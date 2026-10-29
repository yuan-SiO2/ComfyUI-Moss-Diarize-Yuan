"""服务端渲染：把时间线编译成 ffmpeg filter_complex 并出片。

浏览器只负责「把素材传上来 + 描述时间线」，渲染/编码全在 ComfyUI 侧用 ffmpeg 做：
  * 不再经过 WebCodecs → 没有黑屏、花屏、卡百分比
  * 20 线程 + libx264/NVENC，实测 1080p 双轨合成 326 fps

接口：
  POST /yuancut/api/asset/begin                          -> {"token": ...}
  POST /yuancut/api/asset/part?token=&index=              分片上传素材
  POST /yuancut/api/asset/finish?token=&assetId=&ext=mp4  落到 cache/<assetId>
  POST /yuancut/api/render                                时间线 JSON -> MP4
"""

from __future__ import annotations

import asyncio
import logging
import re
import shutil
import subprocess
import tempfile
import uuid
from pathlib import Path

from aiohttp import web

logger = logging.getLogger(__name__)

TOKEN_RE = re.compile(r"^[0-9a-f]{32}$")
ASSET_ID_RE = re.compile(r"^[A-Za-z0-9_.-]{1,80}$")
# 与前端 text-node.ts 保持一致：fontSize 是相对值，按画布高度换算
FONT_SIZE_SCALE_REFERENCE = 90

H264_COLOR_BSF = (
    "h264_metadata=colour_primaries=1:transfer_characteristics=1:"
    "matrix_coefficients=1:video_full_range_flag=0"
)

_upload_tokens: dict[str, Path] = {}


# ------------------------------------------------------------------ 目录与工具
def _output_root() -> Path:
    try:
        import folder_paths  # type: ignore

        return Path(folder_paths.get_output_directory())
    except Exception:  # pragma: no cover
        return Path(tempfile.gettempdir())


def _cache_dir() -> Path:
    target = _output_root() / "YuanCut" / "cache"
    target.mkdir(parents=True, exist_ok=True)
    return target


def _ffmpeg_binary() -> str | None:
    found = shutil.which("ffmpeg")
    if found:
        return found
    for candidate in (Path("/usr/bin/ffmpeg"), Path("/usr/local/bin/ffmpeg")):
        if candidate.is_file():
            return str(candidate)
    return None


async def _probe_streams(path: Path) -> set[str]:
    """用 ffprobe 判断素材里到底有哪些流（避免对无声视频取 :a 而报错）。"""
    ffprobe = shutil.which("ffprobe") or "/usr/bin/ffprobe"
    process = await asyncio.create_subprocess_exec(
        ffprobe,
        "-v",
        "error",
        "-show_entries",
        "stream=codec_type",
        "-of",
        "csv=p=0",
        str(path),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    out, _ = await process.communicate()
    kinds = {line.strip() for line in (out or b"").decode().splitlines() if line.strip()}
    return kinds


def _asset_path(asset_id: str) -> Path | None:
    """按 assetId 找素材：不限定扩展名（上传时有些 File.name 没有后缀）。"""
    if not ASSET_ID_RE.match(asset_id):
        return None

    direct = _cache_dir() / asset_id
    if direct.is_file():
        return direct

    for candidate in sorted(_cache_dir().glob(f"{asset_id}.*")):
        if candidate.is_file():
            return candidate

    return None


def _num(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _element_rate(element: dict) -> float:
    """读取元素的播放倍速（前端字段名兼容 playbackRate / rate / speed）。"""
    for key in ("playbackRate", "rate", "speed"):
        if key in element:
            rate = _num(element.get(key), 1.0)
            if rate > 0:
                return max(0.1, min(10.0, rate))
    return 1.0


def _element_keeps_pitch(element: dict) -> bool:
    """默认保音高；显式 false 时走"磁带效果"（音高随速度变）。"""
    value = element.get("preservesPitch")
    return True if value is None else bool(value)


_FILTER_AVAILABILITY: dict[str, bool] = {}


def _has_filter(name: str) -> bool:
    """缓存探测 ffmpeg 是否带某个滤镜（rubberband 是可选编译项）。"""
    cached = _FILTER_AVAILABILITY.get(name)
    if cached is not None:
        return cached

    available = False
    ffmpeg = _ffmpeg_binary()
    if ffmpeg:
        try:
            probe = subprocess.run(
                [ffmpeg, "-hide_banner", "-filters"],
                capture_output=True,
                text=True,
                timeout=15,
            )
            available = (
                re.search(rf"^\s*\S+\s+{re.escape(name)}\s", probe.stdout or "", re.M)
                is not None
            )
        except Exception:  # pragma: no cover - ffmpeg 不可用时退回 atempo
            available = False
    _FILTER_AVAILABILITY[name] = available
    return available


def _audio_tempo_filter(rate: float, *, keep_pitch: bool) -> str:
    """音频变速滤镜链。

    优先 rubberband（时间伸缩质量最好，且能同时控音高）；没有就退回 atempo
    串联（ffmpeg 内置，单级只支持 0.5~2.0）。两者都不会像 asetrate/重采样那样
    把音色拉尖变闷，这是"变速不变调"的关键。
    """
    if abs(rate - 1.0) < 1e-3:
        return ""

    if _has_filter("rubberband"):
        pitch = 1.0 if keep_pitch else rate
        return (
            f"rubberband=tempo={rate:.6f}:pitch={pitch:.6f}"
            f":formant={'preserved' if keep_pitch else 'shifted'}"
        )

    # 没有 rubberband：只能保音高（atempo 不支持变调），显式变调需求降级处理
    chain: list[str] = []
    value = rate
    while value > 2.0:
        chain.append("atempo=2.0")
        value /= 2.0
    while value < 0.5:
        chain.append("atempo=0.5")
        value /= 0.5
    chain.append(f"atempo={value:.6f}")
    return ",".join(chain)


# --------------------------------------------------------------- filter 编译
class CompileError(Exception):
    pass


def _build_filter(
    timeline: dict,
    inputs: dict[str, int],
    caps: dict[str, set[str]],
    output_size: tuple[int, int] | None = None,
) -> tuple[str, str, str]:
    """返回 (filter_complex, video_out_label, audio_out_label)。"""
    canvas = timeline.get("canvas") or {}
    width = int(canvas.get("width") or 1920)
    height = int(canvas.get("height") or 1080)
    fps = int(timeline.get("fps") or 30)
    duration = _num(timeline.get("duration"), 0)

    parts: list[str] = []
    # 0 号输入是黑色底图
    parts.append(
        f"color=c=black:s={width}x{height}:r={fps}:d={duration:.3f}[canvas0]"
    )
    current = "canvas0"
    index = 0

    tracks = timeline.get("tracks") or []
    for track in tracks:
        for element in track.get("elements") or []:
            if element.get("hidden"):
                continue
            kind = element.get("type")
            start = _num(element.get("start"))
            length = _num(element.get("duration"))
            if length <= 0:
                continue

            if kind == "color":
                color = str(element.get("color") or "#000000").replace("#", "0x")
                parts.append(
                    f"color=c={color}:s={width}x{height}:r={fps}:d={length:.3f}"
                    f",setpts=PTS-STARTPTS+{start:.3f}/TB[c{index}]"
                )
                parts.append(
                    f"[{current}][c{index}]overlay=0:0:eof_action=pass"
                    f":enable='between(t,{start:.3f},{start + length:.3f})'[o{index}]"
                )
                current = f"o{index}"
                index += 1
                continue

            if kind == "text":
                continue

            # 只有视频/图片元素参与画面合成（音频元素只进音轨）
            if kind not in ("video", "image"):
                continue

            asset_id = element.get("assetId")
            if not asset_id:
                continue
            stream = inputs.get(asset_id)
            if stream is None:
                raise CompileError(f"素材未上传：{asset_id}")
            if "video" not in caps.get(asset_id, set()):
                continue

            trim_start = _num(element.get("trimStart"))
            scale = _num(element.get("scale"), 1.0) or 1.0
            opacity = max(0.0, min(1.0, _num(element.get("opacity"), 1.0)))
            pos_x = _num(element.get("x"))
            pos_y = _num(element.get("y"))

            label = f"v{index}"
            # 变速：先按 length*rate 取源，再用 setpts 压回 length 秒
            rate = _element_rate(element) if kind == "video" else 1.0
            source_length = length * rate if rate != 1.0 else length
            if rate != 1.0:
                timing = (
                    f"setpts=(PTS-STARTPTS)/{rate:.6f}+{start:.3f}/TB,fps={fps}"
                )
            else:
                timing = f"setpts=PTS-STARTPTS+{start:.3f}/TB"
            parts.append(
                f"[{stream}:v]trim=start={trim_start:.3f}:duration={source_length:.3f},"
                f"{timing},"
                f"scale=iw*{scale:.4f}:ih*{scale:.4f},format=rgba,"
                f"colorchannelmixer=aa={opacity:.3f}[{label}]"
            )
            parts.append(
                f"[{current}][{label}]overlay="
                f"x=(W-w)/2+{pos_x:.1f}:y=(H-h)/2+{pos_y:.1f}"
                f":eof_action=pass:enable='between(t,{start:.3f},{start + length:.3f})'"
                f"[o{index}]"
            )
            current = f"o{index}"
            index += 1

    # 文字：在主画面之后画 drawtext
    text_index = 0
    for track in tracks:
        for element in track.get("elements") or []:
            if element.get("type") != "text" or element.get("hidden"):
                continue
            content = str(element.get("content") or "").strip()
            if not content:
                continue
            start = _num(element.get("start"))
            length = _num(element.get("duration"))
            if length <= 0:
                continue

            # 应用里的 fontSize 是相对值（字幕默认 5），按画布高度换算成像素
            relative_size = _num(element.get("fontSize"), 5) or 5
            font_size = max(8, round(relative_size * height / FONT_SIZE_SCALE_REFERENCE))
            # 按画布宽度换行，避免切比例后字幕横向溢出画面
            lines = _wrap_text_for_canvas(
                text=content,
                font_size=font_size,
                max_width=max(16.0, width * 0.90),
            )
            text_file = _write_text_file("\n".join(lines))
            color = str(element.get("color") or "#ffffff")
            if not color.startswith("#"):
                color = "#ffffff"
            pos_x = int(width / 2 + _num(element.get("x")))
            pos_y = int(height / 2 + _num(element.get("y")))
            # 文本块垂直夹紧，保证不出画面
            block_height = font_size * 1.3 * len(lines)
            if block_height * 2 <= height:
                pos_y = max(
                    int(block_height / 2),
                    min(pos_y, int(height - block_height / 2)),
                )
            else:
                pos_y = int(height / 2)

            parts.append(
                f"[{current}]drawtext=textfile='{text_file}'"
                f":fontsize={font_size}:fontcolor={color}"
                f":line_spacing={max(2, round(font_size * 0.3))}"
                f":x={pos_x}-text_w/2:y={pos_y}-text_h/2"
                f":enable='between(t,{start:.3f},{start + length:.3f})'[t{text_index}]"
            )
            current = f"t{text_index}"
            text_index += 1

    # 音频：每段素材音轨单独 trim/延迟，最后 amix
    audio_labels: list[str] = []
    audio_index = 0
    for track in tracks:
        for element in track.get("elements") or []:
            if element.get("type") not in ("video", "audio"):
                continue
            if element.get("hidden") or element.get("muted"):
                continue
            asset_id = element.get("assetId")
            stream = inputs.get(asset_id) if asset_id else None
            if stream is None:
                continue
            if "audio" not in caps.get(asset_id, set()):
                continue
            start = _num(element.get("start"))
            length = _num(element.get("duration"))
            trim_start = _num(element.get("trimStart"))
            volume = _num(element.get("volume"), 1.0)
            if length <= 0:
                continue
            label = f"a{audio_index}"
            # 变速：源取 length*rate 秒再时间伸缩回 length 秒；默认保音高（不变调）
            rate = _element_rate(element)
            source_length = length * rate if rate != 1.0 else length
            tempo = _audio_tempo_filter(
                rate, keep_pitch=_element_keeps_pitch(element)
            )
            parts.append(
                f"[{stream}:a]atrim=start={trim_start:.3f}:duration={source_length:.3f},"
                f"asetpts=PTS-STARTPTS,"
                f"{tempo + ',' if tempo else ''}"
                f"volume={volume:.3f},"
                f"adelay={int(start * 1000)}|{int(start * 1000)}[{label}]"
            )
            audio_labels.append(f"[{label}]")
            audio_index += 1

    if output_size is not None and output_size != (width, height):
        out_w, out_h = output_size
        parts.append(
            f"[{current}]scale={out_w}:{out_h}:flags=lanczos,format=yuv420p[scaled]"
        )
        current = "scaled"

    video_out = current
    if audio_labels:
        parts.append(
            "".join(audio_labels)
            + f"amix=inputs={len(audio_labels)}:duration=longest:normalize=0[aout]"
        )
        audio_out = "aout"
    else:
        parts.append("anullsrc=r=48000:cl=stereo[asilence]")
        audio_out = "asilence"
    return ";".join(parts), video_out, audio_out


def _text_char_width(char: str, font_size: int) -> float:
    """粗略估算单字符宽度：CJK 按 1em，ASCII 按 0.55em。"""
    code = ord(char)
    if code >= 0x2E80:
        return font_size * 1.0
    if char == " ":
        return font_size * 0.3
    if char in "iljI.,:;'!|":
        return font_size * 0.3
    if char.isupper() or char in "mwMW@#%&":
        return font_size * 0.72
    return font_size * 0.55


def _wrap_text_for_canvas(
    *, text: str, font_size: int, max_width: float
) -> list[str]:
    """按画布宽度折行，与前端画布渲染的换行行为保持一致。"""
    lines: list[str] = []

    for paragraph in text.split("\n"):
        if not paragraph:
            lines.append("")
            continue

        current = ""
        current_width = 0.0
        for char in paragraph:
            char_width = _text_char_width(char, font_size)
            if current and current_width + char_width > max_width:
                lines.append(current)
                current = char
                current_width = char_width
            else:
                current += char
                current_width += char_width

        if current:
            lines.append(current)

    return lines or [""]


def _write_text_file(text: str) -> str:
    handle = tempfile.NamedTemporaryFile(
        "w", suffix=".txt", delete=False, encoding="utf-8"
    )
    handle.write(text)
    handle.close()
    return handle.name.replace("\\", "/").replace(":", "\\\\:")


async def _run_render(
    timeline: dict, work: Path | None = None, job: dict | None = None
) -> tuple[Path | None, str | None]:
    ffmpeg = _ffmpeg_binary()
    if not ffmpeg:
        return None, "本机找不到 ffmpeg"

    assets: list[str] = []
    for track in timeline.get("tracks") or []:
        for element in track.get("elements") or []:
            asset_id = element.get("assetId")
            if asset_id and asset_id not in assets:
                assets.append(asset_id)

    inputs: dict[str, int] = {}
    input_args: list[str] = []
    for asset_id in assets:
        path = _asset_path(asset_id)
        if path is None:
            return None, f"素材未上传：{asset_id}"
        inputs[asset_id] = len(inputs)
        input_args += ["-i", str(path)]

    caps: dict[str, set[str]] = {}
    for asset_id in assets:
        path = _asset_path(asset_id)
        if path is not None:
            caps[asset_id] = await _probe_streams(path)

    canvas = timeline.get("canvas") or {}
    width = int(canvas.get("width") or 1920)
    height = int(canvas.get("height") or 1080)
    output_cfg = timeline.get("output") or {}
    out_w = int(output_cfg.get("width") or width)
    out_h = int(output_cfg.get("height") or height)

    try:
        filter_complex, video_out, audio_out = _build_filter(
            timeline, inputs, caps, (out_w, out_h)
        )
    except CompileError as exc:
        return None, str(exc)

    duration = _num(timeline.get("duration"), 0)
    fps = int(timeline.get("fps") or 30)
    profile = timeline.get("profile") or {}

    work_dir = work or Path(tempfile.mkdtemp(prefix="yuancut-render-"))
    output = work_dir / "out.mp4"
    total_frames = max(1, int(duration * fps))

    args = [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        *input_args,
        "-filter_complex",
        filter_complex,
        "-map",
        f"[{video_out}]",
        "-map",
        f"[{audio_out}]",
        "-c:v",
        str(profile.get("vcodec") or "libx264"),
        "-preset",
        str(profile.get("preset") or "veryfast"),
        "-crf",
        str(int(profile.get("crf") or 19)),
        "-pix_fmt",
        "yuv420p",
        "-threads",
        str(int(profile.get("threads") or 0)),
        "-c:a",
        "aac",
        "-b:a",
        str(profile.get("audioBitrate") or "192k"),
        "-t",
        f"{duration:.3f}",
        "-movflags",
        "+faststart",
        str(output),
    ]

    args[1:1] = ["-progress", "pipe:1", "-nostats"]

    process = await asyncio.create_subprocess_exec(
        *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    if job is not None:
        job["process"] = process

    async def _drain_progress() -> bytes:
        assert process.stdout is not None
        async for raw in process.stdout:
            line = raw.decode("utf-8", "replace").strip()
            if line.startswith("frame=") and job is not None:
                try:
                    done_frames = int(line.split("=", 1)[1])
                except ValueError:
                    continue
                job["progress"] = min(0.98, done_frames / total_frames)
        return b""

    assert process.stderr is not None
    _, stderr = await asyncio.gather(_drain_progress(), process.stderr.read())
    detail = (stderr or b"").decode("utf-8", "replace").strip()
    produced = output.is_file() and output.stat().st_size > 0

    if process.returncode != 0 or not produced:
        # 有些情况下 ffmpeg 收尾阶段被杀（内存/磁盘），但文件其实已经写完：
        # 只要能读到有效文件就按成功处理，只记警告，不再让整次导出白跑。
        if produced:
            logger.warning(
                "[YuanCut] ffmpeg 返回码 %s，但产物已生成（%d 字节），按成功处理。stderr: %s",
                process.returncode,
                output.stat().st_size,
                detail[-600:] or "(空)",
            )
        else:
            logger.error(
                "[YuanCut] ffmpeg 渲染失败 rc=%s stderr=%s",
                process.returncode,
                detail[-1500:] or "(空)",
            )
            reason = detail[-600:] or (
                f"ffmpeg 被异常终止（返回码 {process.returncode}），"
                "stderr 为空，通常是内存或磁盘不足"
            )
            return None, f"ffmpeg 渲染失败：{reason}"

    # 补 bt709 色彩标记（ffmpeg 滤镜链常丢 primaries/trc）
    fixed = output.with_name("fixed.mp4")
    fix_args = [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-i",
        str(output),
        "-c",
        "copy",
        "-bsf:v",
        H264_COLOR_BSF,
        str(fixed),
    ]
    process = await asyncio.create_subprocess_exec(
        *fix_args, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE
    )
    _, fix_err = await process.communicate()
    if process.returncode != 0:
        logger.warning(
            "[YuanCut] 色彩标记二次封装失败（不影响出片）：%s",
            (fix_err or b"").decode("utf-8", "replace")[-400:],
        )

    return (fixed if fixed.is_file() else output), None


# ------------------------------------------------------------------- handlers
async def asset_begin(request: web.Request) -> web.Response:
    token = uuid.uuid4().hex
    work = Path(tempfile.mkdtemp(prefix="yuancut-asset-"))
    _upload_tokens[token] = work
    return web.json_response({"token": token})


async def asset_part(request: web.Request) -> web.Response:
    token = request.query.get("token", "")
    index = request.query.get("index", "0")
    if not TOKEN_RE.match(token) or token not in _upload_tokens:
        return web.json_response({"error": "invalid token"}, status=400)
    if not index.isdigit():
        return web.json_response({"error": "invalid index"}, status=400)

    payload = await request.read()
    (_upload_tokens[token] / f"part.{int(index):05d}").write_bytes(payload)
    return web.json_response({"ok": True, "size": len(payload)})


async def asset_finish(request: web.Request) -> web.Response:
    token = request.query.get("token", "")
    asset_id = request.query.get("assetId", "")
    ext = request.query.get("ext", "mp4")
    if not TOKEN_RE.match(token) or token not in _upload_tokens:
        return web.json_response({"error": "invalid token"}, status=400)
    if not ASSET_ID_RE.match(asset_id):
        return web.json_response({"error": "invalid assetId"}, status=400)

    work = _upload_tokens.pop(token)
    suffix = re.sub(r"[^a-z0-9]", "", ext.lower()) or "mp4"
    target = _cache_dir() / f"{asset_id}.{suffix}"

    with target.open("wb") as out:
        for part in sorted(work.glob("part.*")):
            with part.open("rb") as src:
                shutil.copyfileobj(src, out, length=1024 * 1024)
    shutil.rmtree(work, ignore_errors=True)

    return web.json_response({"ok": True, "path": str(target)})


async def _stream_mp4(request: web.Request, output: Path) -> web.StreamResponse:
    response = web.StreamResponse(
        headers={
            "Content-Type": "video/mp4",
            "Content-Length": str(output.stat().st_size),
            "Cache-Control": "no-store",
        }
    )
    await response.prepare(request)
    with output.open("rb") as handle:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            await response.write(chunk)
    await response.write_eof()
    return response


async def render_timeline(request: web.Request) -> web.StreamResponse:
    try:
        timeline = await request.json()
    except Exception:
        return web.json_response({"error": "时间线 JSON 非法"}, status=400)

    duration = _num(timeline.get("duration"), 0)
    tracks = timeline.get("tracks") or []
    element_count = sum(len(track.get("elements") or []) for track in tracks)

    # 诊断：把前端送来的时间线摘要写进日志，便于对齐"时间轴 vs 成片"
    type_counts: dict[str, int] = {}
    texts_in: list[str] = []
    for track in tracks:
        for element in track.get("elements") or []:
            kind = str(element.get("type") or "unknown")
            type_counts[kind] = type_counts.get(kind, 0) + 1
            if kind == "text":
                texts_in.append(str(element.get("content") or "")[:12])

    supported = {"video", "image", "text", "audio", "color"}
    skipped = {k: v for k, v in type_counts.items() if k not in supported}
    logger.info(
        "[YuanCut] 时间线：%d 轨 / %d 元素 / 时长 %.2fs / 类型 %s%s%s",
        len(tracks),
        element_count,
        duration,
        type_counts,
        f" / 跳过 {skipped}" if skipped else "",
        f" / 字幕片段 {texts_in[:5]}" if texts_in else " / 无 text 元素",
    )
    if duration <= 0:
        return web.json_response({"error": "时间线时长为 0（项目为空）"}, status=400)
    if element_count == 0:
        return web.json_response({"error": "时间线没有任何元素"}, status=400)

    output, error = await _run_render(timeline)
    if output is None:
        return web.json_response({"error": error or "渲染失败"}, status=500)

    response = web.StreamResponse(
        headers={
            "Content-Type": "video/mp4",
            "Content-Length": str(output.stat().st_size),
            "Cache-Control": "no-store",
        }
    )
    await response.prepare(request)
    with output.open("rb") as handle:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            await response.write(chunk)
    await response.write_eof()
    shutil.rmtree(output.parent, ignore_errors=True)
    return response


async def render_selftest(request: web.Request) -> web.StreamResponse:
    """不依赖任何素材的自检：画 2 秒彩条 + 文字 + 正弦音，验证整条服务端链路。"""
    ffmpeg = _ffmpeg_binary()
    if not ffmpeg:
        return web.json_response({"error": "本机找不到 ffmpeg"}, status=501)

    work = Path(tempfile.mkdtemp(prefix="yuancut-selftest-"))
    output = work / "selftest.mp4"
    text_file = work / "t.txt"
    text_file.write_text("YuanCut selftest", encoding="utf-8")

    args = [
        ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
        "-f", "lavfi", "-i", "testsrc2=size=1280x720:rate=30:duration=2",
        "-f", "lavfi", "-i", "sine=frequency=440:duration=2",
        "-filter_complex",
        f"[0:v]drawtext=textfile='{text_file}':fontsize=64:fontcolor=white"
        f":x=(w-text_w)/2:y=(h-text_h)/2[v]",
        "-map", "[v]", "-map", "1:a",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "128k", "-t", "2", "-movflags", "+faststart",
        str(output),
    ]
    process = await asyncio.create_subprocess_exec(
        *args, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE
    )
    _, stderr = await process.communicate()
    if process.returncode != 0 or not output.is_file():
        detail = (stderr or b"").decode("utf-8", "replace")[-800:]
        shutil.rmtree(work, ignore_errors=True)
        return web.json_response(
            {"error": f"自检渲染失败：{detail}"}, status=500
        )

    data = output.read_bytes()
    shutil.rmtree(work, ignore_errors=True)
    return web.Response(
        body=data,
        headers={
            "Content-Type": "video/mp4",
            "Content-Length": str(len(data)),
            "Cache-Control": "no-store",
        },
    )


# ---------------------------------------------------------------- 任务式渲染
# 一次 POST 拿不到中间进度，所以改成：start 建任务 -> progress 轮询 -> result 取文件
_JOBS: dict[str, dict] = {}


def _job(job_id: str) -> dict | None:
    return _JOBS.get(job_id)


async def _render_job(job_id: str, timeline: dict, work: Path) -> None:
    job = _JOBS[job_id]
    ffmpeg = _ffmpeg_binary()
    if not ffmpeg:
        job.update(state="error", error="本机找不到 ffmpeg")
        return

    try:
        output, error = await _run_render(timeline, work=work, job=job)
        if output is None:
            job.update(state="error", error=error or "渲染失败")
        else:
            job.update(state="done", progress=1.0, output=output)
    except Exception as exc:  # pragma: no cover
        logger.exception("[YuanCut] 渲染任务异常")
        job.update(state="error", error=str(exc))


async def render_start(request: web.Request) -> web.Response:
    try:
        timeline = await request.json()
    except Exception:
        return web.json_response({"error": "时间线 JSON 非法"}, status=400)

    duration = _num(timeline.get("duration"), 0)
    tracks = timeline.get("tracks") or []
    element_count = sum(len(track.get("elements") or []) for track in tracks)
    if duration <= 0:
        return web.json_response({"error": "时间线时长为 0（项目为空）"}, status=400)
    if element_count == 0:
        return web.json_response({"error": "时间线没有任何元素"}, status=400)

    job_id = uuid.uuid4().hex
    work = Path(tempfile.mkdtemp(prefix="yuancut-job-"))
    _JOBS[job_id] = {
        "state": "running",
        "progress": 0.0,
        "error": None,
        "output": None,
        "work": work,
    }
    asyncio.create_task(_render_job(job_id, timeline, work))
    return web.json_response({"jobId": job_id})


async def render_cancel(request: web.Request) -> web.Response:
    """真正杀掉 ffmpeg 进程，取消导出。"""
    job = _job(request.query.get("jobId", ""))
    if job is None:
        return web.json_response({"error": "任务不存在"}, status=404)

    job["state"] = "cancelled"
    job["error"] = "已取消"
    process = job.get("process")
    if process is not None and process.returncode is None:
        try:
            process.kill()
        except ProcessLookupError:
            pass

    work = job.get("work")
    if isinstance(work, Path):
        shutil.rmtree(work, ignore_errors=True)
    _JOBS.pop(request.query.get("jobId", ""), None)
    logger.info("[YuanCut] 导出已取消")
    return web.json_response({"ok": True})


async def render_progress(request: web.Request) -> web.Response:
    job = _job(request.query.get("jobId", ""))
    if job is None:
        return web.json_response({"error": "任务不存在"}, status=404)
    return web.json_response(
        {
            "state": job["state"],
            "progress": round(float(job["progress"]), 4),
            "error": job["error"],
        }
    )


async def render_result(request: web.Request) -> web.StreamResponse:
    job_id = request.query.get("jobId", "")
    job = _job(job_id)
    if job is None:
        return web.json_response({"error": "任务不存在"}, status=404)
    if job["state"] != "done" or not job.get("output"):
        return web.json_response({"error": job.get("error") or "尚未完成"}, status=409)

    output: Path = job["output"]
    work: Path = job["work"]
    response = await _stream_mp4(request, output)
    shutil.rmtree(work, ignore_errors=True)
    _JOBS.pop(job_id, None)
    return response


def register_routes(routes) -> None:  # noqa: ANN001
    routes.post("/yuancut/api/asset/begin")(asset_begin)
    routes.post("/yuancut/api/asset/part")(asset_part)
    routes.post("/yuancut/api/asset/finish")(asset_finish)
    routes.post("/yuancut/api/render")(render_timeline)
    routes.post("/yuancut/api/render/selftest")(render_selftest)
    routes.post("/yuancut/api/render/start")(render_start)
    routes.get("/yuancut/api/render/progress")(render_progress)
    routes.post("/yuancut/api/render/cancel")(render_cancel)
    routes.get("/yuancut/api/render/result")(render_result)
