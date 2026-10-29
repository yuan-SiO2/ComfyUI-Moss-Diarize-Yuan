"""字幕转录：把编辑器的字幕转写接到本地 MOSS-Transcribe-Diarize 模型上。

编辑器原本在浏览器里用 transformers.js 跑 Whisper（Small / Large v3 Turbo /
Distil Small）。这里改成单一的「方法」：前端把 16 kHz 单声道 float32 波形 POST 到
`/yuancut/transcribe`，由 ComfyUI 本机进程调用 MOSS-Transcribe-Diarize 推理，
一次返回带说话人的句段（含起止时间），前端只负责画字幕。

引擎来源：custom_nodes 下已安装的 MOSS 节点包（优先带完整权重的
`ComfyUI-Moss-Diarize-Yuan`），只借用它的 `moss_engine` 模块，不注册任何节点。
"""

from __future__ import annotations

import asyncio
import base64
import importlib
import logging
import sys
import threading
import types
from pathlib import Path
from typing import Any, Iterator

from aiohttp import web

logger = logging.getLogger(__name__)

MODEL_NAME = "MOSS-Transcribe-Diarize"
TRANSCRIBE_PATH = "/yuancut/transcribe"
SAMPLE_RATE = 16000

# 单个请求最多接受的音频长度（秒），避免浏览器一次塞进几小时素材
MAX_AUDIO_SECONDS = 3600.0

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


# --------------------------------------------------------------------- 引擎定位


def _custom_node_roots() -> Iterator[Path]:
    seen: set[Path] = set()

    here = Path(__file__).resolve().parent
    for candidate in (here.parent, here.parent.parent / "custom_nodes"):
        if candidate.is_dir() and candidate not in seen:
            seen.add(candidate)
            yield candidate

    try:
        import folder_paths  # type: ignore

        base = Path(folder_paths.base_path) / "custom_nodes"
        if base.is_dir() and base not in seen:
            seen.add(base)
            yield base
    except Exception:  # pragma: no cover - 非 ComfyUI 环境
        return


# 权重默认放在 ComfyUI/models/Yuan_Cut/MOSS-Transcribe-Diarize（与插件其它模型同目录）
MODEL_FILES = ("config.json", "model-00000-of-00001.safetensors")


def _models_root() -> Path | None:
    """ComfyUI 的 models 目录；拿不到时退回 custom_nodes 的上级目录/models。"""
    try:
        import folder_paths  # type: ignore

        root = Path(folder_paths.models_dir)
        if root.is_dir():
            return root
    except Exception:  # pragma: no cover - 非 ComfyUI 环境
        pass
    for custom_nodes in _custom_node_roots():
        candidate = custom_nodes.parent / "models"
        if candidate.is_dir():
            return candidate
    return None


def yuan_cut_model_dir() -> Path | None:
    root = _models_root()
    return None if root is None else root / "Yuan_Cut" / MODEL_NAME


def _dir_ready(path: Path | None) -> bool:
    return bool(path and path.is_dir() and all((path / name).is_file() for name in MODEL_FILES))


def resolve_model_dir(pack: Path | None = None) -> Path | None:
    """权重查找顺序：models/Yuan_Cut → 节点包自带 models 目录。"""
    candidates = [yuan_cut_model_dir()]
    if pack is not None:
        candidates.append(pack / "models" / MODEL_NAME)
    for candidate in candidates:
        if _dir_ready(candidate):
            return candidate
    for candidate in candidates:
        if candidate is not None and candidate.is_dir():
            return candidate
    return next((item for item in candidates if item is not None), None)


def _apply_model_dir(engine: Any, pack: Path) -> Path | None:
    """把引擎的模型目录指到 Yuan_Cut，缺失时下载也会落在那里。"""
    model_dir = resolve_model_dir(pack)
    download = getattr(engine, "moss_download", None)
    if model_dir is not None and download is not None:
        try:
            model_dir.parent.mkdir(parents=True, exist_ok=True)
        except OSError:  # pragma: no cover - 只影响能否自动下载
            pass
        download.MODEL_ROOT = model_dir.parent
        download.MODEL_DIR = model_dir
    return model_dir


def _pack_looks_ready(pack: Path) -> bool:
    return (pack / "moss_engine.py").is_file() and _dir_ready(resolve_model_dir(pack))


def find_engine_pack() -> Path | None:
    """在 custom_nodes 中找带 moss_engine.py 的 MOSS 节点包，优先已就绪的。"""
    ready: Path | None = None
    any_pack: Path | None = None
    for root in _custom_node_roots():
        try:
            entries = sorted(root.iterdir())
        except OSError:
            continue
        for pack in entries:
            if not pack.is_dir() or not (pack / "moss_engine.py").is_file():
                continue
            if not (pack / "moss_download.py").is_file():
                continue
            if _pack_looks_ready(pack):
                return pack
            any_pack = any_pack or pack
    return ready or any_pack


def _import_engine(pack: Path) -> Any:
    """把 MOSS 包当成一个临时命名空间包导入，避开重复注册 ComfyUI 节点。"""
    pkg_name = f"_yuancut_moss_engine_{abs(hash(str(pack))) & 0xFFFFFF:x}"
    module = sys.modules.get(pkg_name)
    if module is None:
        module = types.ModuleType(pkg_name)
        module.__path__ = [str(pack)]
        sys.modules[pkg_name] = module
    return importlib.import_module(f"{pkg_name}.moss_engine")


def get_engine() -> Any:
    global _engine, _engine_error
    with _engine_lock:
        if _engine is not None:
            return _engine
        if _engine_error is not None:
            raise RuntimeError(_engine_error)
        pack = find_engine_pack()
        if pack is None:
            _engine_error = (
                "未找到 MOSS-Transcribe-Diarize 引擎：请把 ComfyUI-Moss-Diarize-Yuan "
                "节点包放进 custom_nodes，权重放到 models/Yuan_Cut/MOSS-Transcribe-Diarize。"
            )
            raise RuntimeError(_engine_error)
        try:
            _engine = _import_engine(pack)
            model_dir = _apply_model_dir(_engine, pack)
            logger.info("[YuanCut] 字幕转录引擎：%s（模型目录：%s）", pack, model_dir)
            return _engine
        except Exception as exc:  # pragma: no cover - 依赖缺失等
            _engine_error = f"MOSS 引擎加载失败（{pack}）：{exc}"
            raise RuntimeError(_engine_error) from exc


def engine_status() -> dict[str, Any]:
    pack = find_engine_pack()
    model_dir = resolve_model_dir(pack)
    return {
        "model": MODEL_NAME,
        "engine": str(pack) if pack else None,
        "model_dir": str(model_dir) if model_dir else None,
        "model_ready": _dir_ready(model_dir),
        "loaded": _engine is not None,
        "error": _engine_error,
        "sample_rate": SAMPLE_RATE,
    }


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
            {"start": 0.0, "end": round(duration, 3), "speaker": "", "text": result["text"].strip()}
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
    logger.info("[YuanCut] 字幕转录使用 %s（POST %s）", MODEL_NAME, TRANSCRIBE_PATH)


__all__ = ["MODEL_NAME", "TRANSCRIBE_PATH", "register_routes", "engine_status", "find_engine_pack"]
