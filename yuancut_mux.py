"""把浏览器导出的无声视频 / WAV 音频合并或转码成本机能播的文件。

背景：Linux 上的 Chromium 系内核没有 AAC / MP3 编码器，WebCodecs 的
剪映（原生客户端）的做法是「音轨独立编译成 `_temp_audio.aac`，再由 ffmpeg 的
muxer 用 av_interleaved_write_frame 把音频包/视频包交织写进同一个容器」，编码
失败还会逐级回退（硬编 -> 软编 -> 换 writer）。

这里用本机 ffmpeg 承担同样的角色。接口（全部为分片上传，绕开 ComfyUI 默认
100MB 的 client_max_size）：

    POST /yuancut/api/mux/begin                        -> {"token": "..."}
    POST /yuancut/api/mux/part?token=&name=&index=     上传一片
    POST /yuancut/api/mux/finish?token=                无声视频 + WAV -> 带 AAC 的 MP4
    POST /yuancut/api/mux/audio?token=&format=         WAV -> mp3 / aac
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import shutil
import tempfile
import time
import uuid
from pathlib import Path

from aiohttp import web

logger = logging.getLogger(__name__)

# 编码格式定义（从 ComfyUI-Yuan-Tool 的 Media/video_formats/h264-mp4.json 搬来）
FORMAT_PROFILE_PATH = Path(__file__).resolve().parent / "video_formats" / "h264-mp4.json"

# 只改 bitstream 里的色彩标记，不重编码：解决播放器把 H.264 颜色读错（发绿/偏色）
H264_COLOR_BSF = (
    "h264_metadata=colour_primaries=1:transfer_characteristics=1"
    ":matrix_coefficients=1:video_full_range_flag=0"
)
ALLOWED_PRESETS = {
    "ultrafast",
    "superfast",
    "veryfast",
    "faster",
    "fast",
    "medium",
    "slow",
    "slower",
    "veryslow",
}

SESSION_TTL_SECONDS = 6 * 3600
PART_NAME_RE = re.compile(r"^[0-9a-f]{32}$")
ALLOWED_PARTS = {"video", "audio", "file"}
SEGMENT_PART_RE = re.compile(r"^video(\d{1,2})$")
MERGED_NAMES = {"video": "video.mp4", "audio": "audio.wav", "file": "file.bin"}


def _export_dir() -> Path:
    """导出落盘目录：优先 ComfyUI 的 output/YuanCut，非 ComfyUI 环境退到临时目录。"""
    base: Path
    try:
        import folder_paths  # type: ignore

        base = Path(folder_paths.get_output_directory())
    except Exception:  # pragma: no cover - 非 ComfyUI 环境
        base = Path(tempfile.gettempdir())

    target = base / "YuanCut"
    try:
        target.mkdir(parents=True, exist_ok=True)
    except OSError:  # pragma: no cover
        target = Path(tempfile.gettempdir())

    return target


def _safe_filename(name: str) -> str:
    cleaned = re.sub(r"[\\/\\\\:\\*\\?\"<>\\|]+", "_", name or "").strip()
    cleaned = cleaned.lstrip(".")
    return cleaned[:180] or "export.bin"

# 音频导出目标格式 -> (ffmpeg 参数, 输出文件名, MIME)
AUDIO_TARGETS: dict[str, tuple[list[str], str, str]] = {
    "mp3": (["-c:a", "libmp3lame", "-b:a", "192k"], "out.mp3", "audio/mpeg"),
    "aac": (["-c:a", "aac", "-b:a", "192k", "-f", "adts"], "out.aac", "audio/aac"),
}


def _session_root() -> Path:
    return Path(tempfile.gettempdir()) / "yuancut-mux"


def _session_dir(token: str) -> Path:
    return _session_root() / token


def _ffmpeg_binary() -> str | None:
    found = shutil.which("ffmpeg")
    if found:
        return found

    for candidate in (
        Path("/usr/bin/ffmpeg"),
        Path("/usr/local/bin/ffmpeg"),
        Path("/opt/homebrew/bin/ffmpeg"),
    ):
        if candidate.is_file():
            return str(candidate)

    return None


def _cleanup_stale_sessions() -> None:
    root = _session_root()
    if not root.is_dir():
        return

    now = time.time()
    for entry in root.iterdir():
        try:
            if now - entry.stat().st_mtime > SESSION_TTL_SECONDS:
                shutil.rmtree(entry, ignore_errors=True)
        except OSError:  # pragma: no cover - 并发删除等
            pass


def _merge_parts(session: Path, name: str) -> Path | None:
    """把 name.xxxxx.part 按序号拼成最终文件。"""
    parts = sorted(session.glob(f"{name}.*.part"))
    if not parts:
        return None

    target = session / MERGED_NAMES.get(name, f"{name}.mp4")
    with target.open("wb") as out:
        for part in parts:
            with part.open("rb") as src:
                shutil.copyfileobj(src, out, length=1024 * 1024)
            part.unlink(missing_ok=True)

    return target


async def _run_ffmpeg(args: list[str]) -> tuple[int, bytes]:
    process = await asyncio.create_subprocess_exec(
        *args,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await process.communicate()
    return process.returncode or 0, stderr or b""


def _load_format_profile() -> dict:
    """读取 video_formats/h264-mp4.json（从 ComfyUI-Yuan-Tool 的格式定义搬来）。"""
    try:
        with FORMAT_PROFILE_PATH.open(encoding="utf-8") as handle:
            return json.load(handle)
    except Exception as exc:  # pragma: no cover - 配置损坏时用内置默认
        logger.warning("[YuanCut] 读取编码格式配置失败，改用内置默认：%s", exc)
        return {
            "main_pass": [
                "-c:v",
                "libx264",
                "-pix_fmt",
                "yuv420p",
                "-crf",
                "19",
                "-vf",
                "scale=out_color_matrix=bt709",
                "-color_range",
                "tv",
                "-colorspace",
                "bt709",
                "-color_primaries",
                "bt709",
                "-color_trc",
                "bt709",
            ],
            "audio_pass": ["-c:a", "aac"],
        }


def _resolve_pass(
    tokens: list,
    *,
    crf: int | None = None,
    pix_fmt: str | None = None,
) -> list[str]:
    """把格式定义里的 ["crf", ...] / ["pix_fmt", ...] 说明符换成真实取值。"""
    resolved: list[str] = []

    for token in tokens:
        if isinstance(token, str):
            # -n / -y 由本模块统一控制（总是覆盖写），配置里的同名字段跳过
            if token in {"-n", "-y"}:
                continue
            resolved.append(token)
            continue


        if not isinstance(token, list) or not token:
            continue

        kind = token[0]
        if kind == "pix_fmt":
            options = token[1] if len(token) > 1 and isinstance(token[1], list) else []
            resolved.append(pix_fmt or (options[0] if options else "yuv420p"))
        elif kind == "crf":
            default = 19
            if len(token) > 2 and isinstance(token[2], dict):
                default = int(token[2].get("default", default))
            resolved.append(str(crf if crf is not None else default))

    return resolved


def _mux_encode_args(
    profile: dict,
    *,
    crf: int | None,
    bitrate: int | None,
) -> list[str]:
    args = _resolve_pass(profile.get("main_pass", []), crf=crf, pix_fmt="yuv420p")

    if bitrate:  # 面板指定了码率：用 -b:v 覆盖 CRF
        cleaned: list[str] = []
        skip_next = False
        for token in args:
            if skip_next:
                skip_next = False
                continue
            if token == "-crf":
                skip_next = True
                continue
            cleaned.append(token)
        args = [*cleaned, "-b:v", str(bitrate)]

    return _append_setparams(args)


COLOR_FLAG_NAMES = {
    "-colorspace": "colorspace",
    "-color_primaries": "color_primaries",
    "-color_trc": "color_trc",
    "-color_range": "range",
}


def _append_setparams(args: list[str]) -> list[str]:
    """-vf 会覆盖帧上的色彩元数据，用 setparams 把 primaries/trc 补回去。"""
    params: list[str] = []
    for index, token in enumerate(args):
        name = COLOR_FLAG_NAMES.get(token)
        if name and index + 1 < len(args):
            params.append(f"{name}={args[index + 1]}")

    if not params or "-vf" not in args:
        return args

    vf_index = args.index("-vf")
    if vf_index + 1 >= len(args):
        return args

    filter_expr = args[vf_index + 1]
    if "setparams" in filter_expr:
        return args

    updated = list(args)
    updated[vf_index + 1] = f"{filter_expr},setparams={':'.join(params)}"
    return updated


def _mux_base_args(
    ffmpeg: str,
    video: Path,
    audio: Path,
    output: Path,
    codec_args: list[str],
    audio_args: list[str],
) -> list[str]:
    return [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-i",
        str(video),
        "-i",
        str(audio),
        "-map",
        "0:v:0",
        "-map",
        "1:a:0",
        *codec_args,
        *audio_args,
        "-b:a",
        "192k",
        "-movflags",
        "+faststart",
        "-shortest",
        str(output),
    ]



def subprocess_run_sync(args: list[str]) -> tuple[int, bytes]:
    import subprocess

    result = subprocess.run(args, capture_output=True, check=False)
    return result.returncode, result.stderr or b""


def _concat_segments(session: Path, clips: list[Path]) -> Path | None:
    """把并行导出的分段视频拼成一个（优先流复制，失败再重编码）。"""
    ffmpeg = _ffmpeg_binary()
    if not ffmpeg or not clips:
        return None

    if len(clips) == 1:
        return clips[0]

    list_file = session / "concat.txt"
    list_file.write_text(
        "\n".join(f"file '{clip.name}'" for clip in clips), encoding="utf-8"
    )
    output = session / "concat.mp4"

    attempts = (
        ["-c:v", "copy", "-bsf:v", H264_COLOR_BSF],
        ["-c:v", "libx264", "-preset", "veryfast", "-crf", "19", "-pix_fmt", "yuv420p"],
    )

    for codec_args in attempts:
        args = [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            str(list_file),
            *codec_args,
            str(output),
        ]
        code, stderr = subprocess_run_sync(args)
        if code == 0 and output.is_file() and output.stat().st_size > 0:
            for clip in clips:
                clip.unlink(missing_ok=True)
            return output

        logger.warning(
            "[YuanCut] 分段拼接失败，换下一种方式：%s",
            stderr.decode("utf-8", "replace")[-300:],
        )
        output.unlink(missing_ok=True)

    return None


async def _mux(
    ffmpeg: str,
    video: Path,
    audio: Path,
    output: Path,
    *,
    crf: int | None = None,
    bitrate: int | None = None,
    preset: str = "veryfast",
) -> bool:
    """按「最快优先」挑第一个能成功的封装方式：

    1. 视频流复制 + h264_metadata 改色彩标记（不重编码，通常 0.1~0.3s）
    2. 按 video_formats 配置重编码 + h264_metadata（Kdenlive 同款「编码速度」档，默认 veryfast）
    3. NVENC 硬件编码兜底（对应 Kdenlive 的 nvenc 预设）
    """
    profile = _load_format_profile()
    audio_args = [
        item for item in profile.get("audio_pass", []) if isinstance(item, str)
    ]
    if not audio_args:
        audio_args = ["-c:a", "aac"]

    attempts: list[list[str]] = []

    # 1) 最快：直接复制视频流，只改色彩标记（播放器不会把颜色读错）
    if not bitrate and not crf:
        attempts.append(["-c:v", "copy", "-bsf:v", H264_COLOR_BSF])

    # 2) 重编码：libx264 + 指定速度档 + 全线程
    encode_args = _mux_encode_args(profile, crf=crf, bitrate=bitrate)
    if "-preset" not in encode_args:
        encode_args[2:2] = ["-preset", preset]
    encode_args += ["-threads", "0", "-bsf:v", H264_COLOR_BSF]
    attempts.append(encode_args)

    # 3) 兜底：NVENC 硬件编码（有卡就用，失败自动跳过）
    attempts.append(
        [
            "-c:v",
            "h264_nvenc",
            "-preset",
            "p4",
            "-rc",
            "vbr",
            "-cq",
            str(crf or 21),
            "-b:v",
            str(bitrate) if bitrate else "0",
            "-pix_fmt",
            "yuv420p",
            "-bsf:v",
            H264_COLOR_BSF,
        ]
    )

    last_error = b""
    for codec_args in attempts:
        args = _mux_base_args(ffmpeg, video, audio, output, codec_args, audio_args)
        code, stderr = await _run_ffmpeg(args)
        if code == 0 and output.is_file() and output.stat().st_size > 0:
            return True

        last_error = stderr
        output.unlink(missing_ok=True)

    logger.warning(
        "[YuanCut] ffmpeg mux failed: %s",
        last_error.decode("utf-8", "replace")[-800:],
    )
    return False


async def _stream_file(request: web.Request, path: Path, mime: str) -> web.StreamResponse:
    response = web.StreamResponse(
        headers={
            "Content-Type": mime,
            "Content-Length": str(path.stat().st_size),
            "Cache-Control": "no-store",
        }
    )
    await response.prepare(request)

    with path.open("rb") as handle:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            await response.write(chunk)

    await response.write_eof()
    return response


async def mux_begin(request: web.Request) -> web.Response:
    _cleanup_stale_sessions()

    token = uuid.uuid4().hex
    _session_dir(token).mkdir(parents=True, exist_ok=True)

    return web.json_response({"token": token})


async def mux_part(request: web.Request) -> web.Response:
    token = request.query.get("token", "")
    name = request.query.get("name", "")
    index = request.query.get("index", "0")

    if not PART_NAME_RE.match(token):
        return web.json_response({"error": "invalid token"}, status=400)
    if name not in ALLOWED_PARTS and not SEGMENT_PART_RE.match(name):
        return web.json_response({"error": "invalid name"}, status=400)
    if not index.isdigit():
        return web.json_response({"error": "invalid index"}, status=400)

    session = _session_dir(token)
    if not session.is_dir():
        return web.json_response({"error": "session not found"}, status=404)

    payload = await request.read()
    (session / f"{name}.{int(index):05d}.part").write_bytes(payload)

    return web.json_response({"ok": True, "size": len(payload)})


async def mux_finish(request: web.Request) -> web.StreamResponse:
    """无声视频 + WAV -> 带 AAC 音轨的 MP4。"""
    token = request.query.get("token", "")
    if not PART_NAME_RE.match(token):
        return web.json_response({"error": "invalid token"}, status=400)

    session = _session_dir(token)
    if not session.is_dir():
        return web.json_response({"error": "session not found"}, status=404)

    ffmpeg = _ffmpeg_binary()
    if not ffmpeg:
        return web.json_response(
            {"error": "本机找不到 ffmpeg，无法把音频合成进 MP4"}, status=501
        )

    try:
        segments_raw = request.query.get("segments", "1")
        segments = int(segments_raw) if segments_raw.isdigit() else 1
        if segments > 1:
            clips = []
            for index in range(segments):
                clip = _merge_parts(session, f"video{index}")
                if clip is None:
                    return web.json_response({"error": "分段视频不完整"}, status=400)
                clips.append(clip)
            video = _concat_segments(session, clips)
            if video is None:
                return web.json_response({"error": "分段拼接失败"}, status=500)
        else:
            video = _merge_parts(session, "video")
        audio = _merge_parts(session, "audio")
        if video is None or audio is None:
            return web.json_response({"error": "缺少 video 或 audio 数据"}, status=400)

        output = session / "out.mp4"
        preset = request.query.get("preset", "veryfast").lower()
        if preset not in ALLOWED_PRESETS:
            preset = "veryfast"

        bitrate_raw = request.query.get("bitrate", "")
        bitrate = int(bitrate_raw) if bitrate_raw.isdigit() else None

        crf_raw = request.query.get("crf", "")
        crf = int(crf_raw) if crf_raw.isdigit() else None

        if not await _mux(
            ffmpeg, video, audio, output, crf=crf, bitrate=bitrate, preset=preset
        ):
            return web.json_response(
                {"error": "ffmpeg 合成失败（详见 ComfyUI 控制台日志）"}, status=500
            )

        return await _stream_file(request, output, "video/mp4")
    finally:
        shutil.rmtree(session, ignore_errors=True)


async def mux_audio(request: web.Request) -> web.StreamResponse:
    """WAV -> mp3 / aac（浏览器没有这些编码器）。"""
    token = request.query.get("token", "")
    target = request.query.get("format", "mp3").lower()

    if not PART_NAME_RE.match(token):
        return web.json_response({"error": "invalid token"}, status=400)
    if target not in AUDIO_TARGETS:
        return web.json_response({"error": "unsupported format"}, status=400)

    session = _session_dir(token)
    if not session.is_dir():
        return web.json_response({"error": "session not found"}, status=404)

    ffmpeg = _ffmpeg_binary()
    if not ffmpeg:
        return web.json_response({"error": "本机找不到 ffmpeg"}, status=501)

    codec_args, out_name, mime = AUDIO_TARGETS[target]

    try:
        audio = _merge_parts(session, "audio")
        if audio is None:
            return web.json_response({"error": "缺少 audio 数据"}, status=400)

        output = session / out_name
        args = [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(audio),
            *codec_args,
            str(output),
        ]
        code, stderr = await _run_ffmpeg(args)
        if code != 0 or not output.is_file() or output.stat().st_size == 0:
            detail = stderr.decode("utf-8", "replace")[-500:]
            logger.warning("[YuanCut] audio convert failed: %s", detail)
            return web.json_response({"error": "音频转码失败"}, status=500)

        return await _stream_file(request, output, mime)
    finally:
        shutil.rmtree(session, ignore_errors=True)


async def export_dir_info(request: web.Request) -> web.Response:
    """给前端显示真实的导出绝对路径。"""
    return web.json_response({"path": str(_export_dir())})


async def save_uploaded_file(request: web.Request) -> web.Response:
    """把分片上传的文件直接落到导出目录，返回绝对路径。"""
    token = request.query.get("token", "")
    filename = _safe_filename(request.query.get("filename", "export.bin"))

    if not PART_NAME_RE.match(token):
        return web.json_response({"error": "invalid token"}, status=400)

    session = _session_dir(token)
    if not session.is_dir():
        return web.json_response({"error": "session not found"}, status=404)

    try:
        uploaded = _merge_parts(session, "file")
        if uploaded is None:
            return web.json_response({"error": "缺少文件数据"}, status=400)

        directory = _export_dir()
        target = directory / filename
        shutil.move(str(uploaded), str(target))
        logger.info("[YuanCut] 导出文件已保存：%s", target)

        return web.json_response({"ok": True, "path": str(target)})
    finally:
        shutil.rmtree(session, ignore_errors=True)


def register_routes(routes) -> None:  # noqa: ANN001 - aiohttp RouteTableDef
    routes.post("/yuancut/api/mux/begin")(mux_begin)
    routes.post("/yuancut/api/mux/part")(mux_part)
    routes.post("/yuancut/api/mux/finish")(mux_finish)
    routes.post("/yuancut/api/mux/audio")(mux_audio)
    routes.get("/yuancut/api/export-dir")(export_dir_info)
    routes.post("/yuancut/api/save/finish")(save_uploaded_file)
