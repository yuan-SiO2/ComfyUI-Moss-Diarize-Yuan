"""字幕转录：把编辑器的字幕转写接到**插件内置**的 MOSS-Transcribe-Diarize 模型上。

编辑器原来的 Whisper（Small / Large v3 Turbo / Distil Small）已在浏览器里下线。
前端把 16 kHz 单声道 float32 波形 POST 到 `/yuancut/transcribe`，这里在本机进程里
调用 MOSS 推理，一次返回带说话人的句段（含起止时间），前端只负责画字幕。

引擎和权重下载器都在本插件的 `yuancut_asr/` 里，**不再依赖 custom_nodes 下的任何
外部节点包**：

  * `yuancut_asr/moss_engine.py`   —— 推理
  * `yuancut_asr/moss_download.py` —— 权重供给

权重默认放在 `ComfyUI/models/Yuan_Cut/MOSS-Transcribe-Diarize/`（约 1.8 GB），
**检测不到就自动下载**（ModelScope 优先，失败回退 HuggingFace，断点续传），
ComfyUI 启动时后台开始下，不影响启动速度。

环境变量：

  * `YUANCUT_MOSS_AUTODOWNLOAD=0` 关闭启动时自动下载（第一次转写时再下）
  * `YUANCUT_MOSS_MODEL_DIR=/abs/path` 换权重目录
  * `YUANCUT_MOSS_DOWNLOAD_SOURCE=modelscope|huggingface` 指定下载源
"""

from __future__ import annotations

import asyncio
import base64
import importlib
import logging
import threading
from typing import Any

from aiohttp import web

from .yuancut_asr import moss_download as downloader

logger = logging.getLogger(__name__)

MODEL_NAME = "MOSS-Transcribe-Diarize"
TRANSCRIBE_PATH = "/yuancut/transcribe"
SAMPLE_RATE = 16000

# 单个请求最多接受的音频长度（秒），避免浏览器一次塞进几小时素材
MAX_AUDIO_SECONDS = 3600.0

# 首次转写时如果模型还没下完，最多等这么久（下载在后台线程里跑）
MODEL_WAIT_SECONDS = 900.0

_LANGUAGE_HINT = {
    "zh": "中文",
    "en": "English",
    "ja": "日本語",
    "es": "Español",
    "it": "Italiano",
    "fr": "Français",
    "de": "Deutsch",
    "pt": "Português",
    "ru": "Русский",
}

_engine_lock = threading.Lock()
_engine: Any | None = None
_engine_error: str | None = None
_run_lock = threading.Lock()


# --------------------------------------------------------------------- 内置引擎


def get_engine() -> Any:
    """导入内置引擎模块（首次调用时才 import torch / transformers）。"""
    global _engine, _engine_error
    with _engine_lock:
        if _engine is not None:
            return _engine
        if _engine_error is not None:
            raise RuntimeError(_engine_error)
        try:
            module = importlib.import_module(f"{__package__}.yuancut_asr.moss_engine")
        except Exception as exc:  # pragma: no cover - 依赖缺失等
            _engine_error = (
                f"内置 MOSS 引擎加载失败：{exc}（需要 torch / transformers / numpy）"
            )
            raise RuntimeError(_engine_error) from exc
        _engine = module
        logger.info(
            "[YuanCut] 字幕转录引擎：内置 yuancut_asr（权重目录：%s）",
            downloader.MODEL_DIR,
        )
        return _engine


def engine_status() -> dict[str, Any]:
    state = downloader.progress()
    return {
        "model": MODEL_NAME,
        "engine": "内置 yuancut_asr",
        "model_dir": state["directory"],
        "model_ready": state["ready"],
        "downloading": state["downloading"],
        "download_progress": round(float(state["progress"]), 4),
        "downloaded_bytes": state["downloaded_bytes"],
        "total_bytes": state["total_bytes"],
        "missing_files": state["missing"],
        "download_source": state["source"],
        "download_error": state["error"],
        "loaded": _engine is not None,
        "error": _engine_error,
        "sample_rate": SAMPLE_RATE,
    }


async def _ensure_model() -> str | None:
    """确保权重就绪；返回错误说明，就绪返回 None。"""
    if downloader.is_ready():
        return None

    loop = asyncio.get_running_loop()
    logger.info("[YuanCut] 字幕转录：权重缺失，开始下载到 %s", downloader.MODEL_DIR)
    try:
        await asyncio.wait_for(
            loop.run_in_executor(None, downloader.ensure, True),
            timeout=MODEL_WAIT_SECONDS,
        )
    except asyncio.TimeoutError:
        state = downloader.progress()
        return (
            f"MOSS 权重还在下载（{state['progress'] * 100:.0f}%，"
            f"{state['downloaded_bytes'] / 1024 ** 3:.2f}/{state['total_bytes'] / 1024 ** 3:.2f} GiB）。"
            "先关掉这个提示，等 ComfyUI 控制台打完「下载完成」再点一次「开始转写」。"
        )
    except Exception as exc:
        return f"MOSS 权重下载失败：{exc}（可设置 YUANCUT_MOSS_MODEL_DIR 手动放权重）"
    return None


# --------------------------------------------------------------------- 推理


def _decode_audio(payload: dict[str, Any]) -> Any:
    import numpy as np

    encoded = payload.get("audio")
    if not isinstance(encoded, str) or not encoded:
        raise ValueError("缺少 audio（base64 编码的 16 kHz float32 波形）。")
    raw = base64.b64decode(encoded)
    if len(raw) % 4:
        raise ValueError("audio 不是完整的 float32 数据。")
    samples = np.frombuffer(raw, dtype=np.float32)
    if samples.size == 0:
        raise ValueError("音频为空。")
    seconds = samples.size / SAMPLE_RATE
    if seconds > MAX_AUDIO_SECONDS:
        raise ValueError(f"单次转写最多 {int(MAX_AUDIO_SECONDS)} 秒，当前 {seconds:.0f} 秒。")
    return np.ascontiguousarray(samples, dtype=np.float32)


def _build_prompt(engine: Any, language: str) -> str:
    prompt = engine.DEFAULT_PROMPT
    hint = _LANGUAGE_HINT.get(str(language or "").strip().lower())
    if hint:
        prompt = f"{prompt.rstrip()}。音频的主要语言是：{hint}。"
    return prompt


def _transcribe_sync(samples: Any, language: str) -> dict[str, Any]:
    import torch

    engine = get_engine()
    waveform = torch.from_numpy(samples).reshape(1, 1, -1)
    with _run_lock:
        result = engine.transcribe(
            {"waveform": waveform, "sample_rate": SAMPLE_RATE},
            prompt=_build_prompt(engine, language),
            device_name="auto",
            attention="sdpa",
        )

    duration = float(result.get("duration") or samples.size / SAMPLE_RATE)
    segments = []
    speakers: list[str] = []
    for item in engine.parse_segments(result.get("text") or "", clamp_to=duration):
        speaker = str(getattr(item, "speaker", "") or "")
        text = str(getattr(item, "text", "") or "").strip()
        if not text:
            continue
        if speaker and speaker not in speakers:
            speakers.append(speaker)
        segments.append(
            {
                "start": round(float(getattr(item, "start", 0.0)), 3),
                "end": round(float(getattr(item, "end", 0.0)), 3),
                "speaker": speaker,
                "text": text,
            }
        )
    if not segments and (result.get("text") or "").strip():
        # 模型没按格式输出时至少把整段文字当一条字幕
        segments.append(
            {
                "start": 0.0,
                "end": round(duration, 3),
                "speaker": "",
                "text": result["text"].strip(),
            }
        )

    return {
        "model": MODEL_NAME,
        "text": " ".join(item["text"] for item in segments),
        "segments": segments,
        "speakers": speakers,
        "duration": round(duration, 3),
        "generated_tokens": int(result.get("generated_tokens") or 0),
        "attention": result.get("attention"),
    }


async def _handle_transcribe(request: web.Request) -> web.StreamResponse:
    try:
        payload = await request.json()
    except Exception:
        return web.json_response({"error": "请求体必须是 JSON。"}, status=400)

    try:
        samples = _decode_audio(payload)
    except Exception as exc:
        return web.json_response({"error": str(exc)}, status=400)

    language = str(payload.get("language") or "auto")

    problem = await _ensure_model()
    if problem is not None:
        return web.json_response(
            {"error": problem, **engine_status()},
            status=503,
        )

    try:
        loop = asyncio.get_running_loop()
        result = await loop.run_in_executor(None, _transcribe_sync, samples, language)
    except Exception as exc:  # pragma: no cover - 推理失败
        logger.exception("[YuanCut] MOSS 字幕转录失败")
        return web.json_response({"error": str(exc)}, status=500)
    return web.json_response(result)


async def _handle_status(request: web.Request) -> web.StreamResponse:
    return web.json_response(engine_status())


def register_routes(routes: Any) -> None:
    routes.post(TRANSCRIBE_PATH)(_handle_transcribe)
    routes.get(TRANSCRIBE_PATH)(_handle_status)
    logger.info(
        "[YuanCut] 字幕转录使用内置 %s（权重：%s）",
        MODEL_NAME,
        downloader.MODEL_DIR,
    )
    # 模型缺失就在后台开始下载：启动不阻塞，用户点转写时通常已经下好
    try:
        downloader.start_background_provision()
    except Exception as exc:  # pragma: no cover - 只是预取，失败不影响启动
        logger.warning("[YuanCut] 字幕模型预取失败：%s", exc)


__all__ = ["MODEL_NAME", "TRANSCRIBE_PATH", "register_routes", "engine_status", "get_engine"]
