"""MOSS-Transcribe-Diarize 推理引擎: 加载、音频适配、生成、段落解析、字幕生成.

关键点:
  * 模型是 HF remote-code 模型, 必须 trust_remote_code=True
  * processor 直接吃 16 kHz 单声道 float32 波形数组, 内部自己切 30s 窗口
    → 所以完全不需要 ffmpeg / soundfile / librosa / 临时 wav 文件
  * eager attention 在长音频下显存平方增长会 OOM, 默认用 sdpa
"""

from __future__ import annotations

import copy
import re
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import numpy as np
import torch

from . import moss_download

TARGET_SAMPLE_RATE = 16000  # WhisperFeatureExtractor 固定要 16 kHz

DEFAULT_PROMPT = (
    "请将音频转写为文本，每一段需以起始时间戳和说话人编号"
    "（[S01]、[S02]、[S03]…）开头，正文为对应的语音内容，"
    "并在段末标注结束时间戳，以清晰标明该段语音范围。"
)

# 输出格式: [0.48][S01]正文[1.66][12.26][S02]正文[13.81]
_TS_RE = re.compile(r"\[(\d+(?:\.\d+)?)\]")
_SPK_RE = re.compile(r"\[(S\d+)\]")

_ATTN_ORDER = ("flash_attention_2", "sdpa", "eager")


def log(message: str) -> None:
    print(f"[MOSS本地] {message}", flush=True)


# --------------------------------------------------------------------------- 音频适配


def _resample(waveform: torch.Tensor, source_rate: int, target_rate: int) -> torch.Tensor:
    if source_rate == target_rate or waveform.numel() == 0:
        return waveform
    try:
        import torchaudio.functional as audio_functional

        return audio_functional.resample(waveform, orig_freq=source_rate, new_freq=target_rate)
    except Exception:  # torchaudio 缺失或后端异常时退化为线性插值
        import torch.nn.functional as functional

        length = max(1, int(round(waveform.numel() * target_rate / source_rate)))
        squeezed = waveform.reshape(1, 1, -1)
        out = functional.interpolate(squeezed, size=length, mode="linear", align_corners=False)
        return out.reshape(-1)


def comfy_audio_to_numpy(audio: dict[str, Any]) -> np.ndarray:
    """ComfyUI 的 AUDIO (44.1kHz 立体声 float32 张量) → 16kHz 单声道 numpy."""
    if not isinstance(audio, dict) or "waveform" not in audio:
        raise ValueError("audio 输入必须是 ComfyUI 的 AUDIO 类型 (含 waveform 与 sample_rate)")

    waveform = audio["waveform"]
    sample_rate = int(audio.get("sample_rate") or TARGET_SAMPLE_RATE)

    if not torch.is_tensor(waveform):
        waveform = torch.as_tensor(waveform)
    waveform = waveform.detach().to("cpu", torch.float32)

    if waveform.ndim == 3:  # [B, C, T] → 取第一条
        waveform = waveform[0]
    if waveform.ndim == 2:  # [C, T] → 下混单声道
        waveform = waveform.mean(dim=0) if waveform.shape[0] > 1 else waveform[0]
    if waveform.ndim != 1:
        waveform = waveform.reshape(-1)

    waveform = torch.nan_to_num(waveform, nan=0.0, posinf=0.0, neginf=0.0)
    waveform = _resample(waveform, sample_rate, TARGET_SAMPLE_RATE)

    array = waveform.contiguous().numpy().astype(np.float32, copy=False)
    if array.size == 0:
        raise ValueError("音频为空, 没有可转写的采样点")
    return array


def duration_of(audio: dict[str, Any]) -> float:
    waveform = audio.get("waveform")
    sample_rate = int(audio.get("sample_rate") or TARGET_SAMPLE_RATE)
    if waveform is None or sample_rate <= 0:
        return 0.0
    return float(waveform.shape[-1]) / float(sample_rate)


# --------------------------------------------------------------------------- 模型加载


@dataclass
class LoadedModel:
    model: Any
    processor: Any
    device: torch.device
    dtype: torch.dtype
    attention: str
    path: Path


_CACHE: dict[tuple[str, str, str], LoadedModel] = {}
_CACHE_LOCK = threading.Lock()


def _resolve_device(name: str) -> torch.device:
    if name == "auto":
        try:
            import comfy.model_management as model_management

            return model_management.get_torch_device()
        except Exception:
            return torch.device("cuda:0" if torch.cuda.is_available() else "cpu")
    device = torch.device(name)
    if device.type == "cuda" and not torch.cuda.is_available():
        return torch.device("cpu")
    return device


def _resolve_dtype(name: str, device: torch.device) -> torch.dtype:
    if device.type != "cuda":
        return torch.float32
    if name == "auto":
        try:
            supports_bf16 = torch.cuda.is_bf16_supported()
        except Exception:
            supports_bf16 = False
        return torch.bfloat16 if supports_bf16 else torch.float16
    table = {
        "bf16": torch.bfloat16,
        "bfloat16": torch.bfloat16,
        "fp16": torch.float16,
        "float16": torch.float16,
        "fp32": torch.float32,
        "float32": torch.float32,
    }
    return table.get(name.lower(), torch.bfloat16)


def _attention_candidates(requested: str, device: torch.device) -> tuple[str, ...]:
    if requested == "auto":
        candidates = list(_ATTN_ORDER)
        # 没装 flash-attn 时不必浪费一次加载尝试
        import importlib.util

        if importlib.util.find_spec("flash_attn") is None:
            candidates.remove("flash_attention_2")
        return tuple(candidates)
    return (requested,)


def _free_vram(device: torch.device) -> None:
    """加载前尽量腾出显存, 但绝不卸载别人的模型."""
    if device.type != "cuda":
        return
    try:
        import comfy.model_management as model_management

        model_management.free_memory(4 * 1024**3, device)
    except Exception:
        pass


def load_model(device_name: str = "auto", dtype_name: str = "auto", attention: str = "sdpa") -> LoadedModel:
    global _CACHE

    model_dir = moss_download.wait_ready()
    device = _resolve_device(device_name)
    dtype = _resolve_dtype(dtype_name, device)
    key = (str(device), str(dtype), attention)

    with _CACHE_LOCK:
        cached = _CACHE.get(key)
        if cached is not None:
            return cached

        from transformers import AutoModelForCausalLM, AutoProcessor

        _free_vram(device)
        log(f"加载模型 {model_dir} (device={device}, dtype={dtype}, attention={attention})")

        candidates = _attention_candidates(attention, device)
        model = None
        used_attention = ""
        last_error: BaseException | None = None
        for candidate in candidates:
            try:
                model = AutoModelForCausalLM.from_pretrained(
                    str(model_dir),
                    trust_remote_code=True,
                    dtype=dtype,
                    attn_implementation=candidate,
                    low_cpu_mem_usage=True,
                )
                used_attention = candidate
                break
            except Exception as exc:  # 显式指定的后端失败也应该退回, 但要把原因打出来
                last_error = exc
                log(f"attention={candidate} 加载失败: {type(exc).__name__}: {exc}")
        if model is None:
            raise RuntimeError(f"MOSS 模型加载失败: {last_error}") from last_error

        if used_attention == "eager":
            log("警告: 正在使用 eager attention, 长音频显存会平方增长, 建议安装 flash-attn 或改用 sdpa")

        try:
            processor = AutoProcessor.from_pretrained(
                str(model_dir), trust_remote_code=True, fix_mistral_regex=True
            )
        except TypeError:
            processor = AutoProcessor.from_pretrained(str(model_dir), trust_remote_code=True)

        model = model.to(device=device, dtype=dtype).eval()

        loaded = LoadedModel(
            model=model,
            processor=processor,
            device=device,
            dtype=dtype,
            attention=used_attention,
            path=model_dir,
        )
        _CACHE = {key: loaded}  # 只保留一份, 避免多份权重同时占显存
        log(f"模型就绪, 实际 attention={used_attention}")
        return loaded


def unload_models() -> int:
    """释放本插件缓存的模型, 返回释放的份数."""
    global _CACHE
    with _CACHE_LOCK:
        count = len(_CACHE)
        _CACHE = {}
    if count:
        import gc

        gc.collect()
        if torch.cuda.is_available():
            try:
                import comfy.model_management as model_management

                model_management.soft_empty_cache()
            except Exception:
                torch.cuda.empty_cache()
    return count


# --------------------------------------------------------------------------- 生成


class _ProgressStreamer:
    """统计已生成 token 数, 顺带响应 ComfyUI 的中断按钮."""

    def __init__(self, on_token: Callable[[int], None] | None, on_check: Callable[[], None] | None):
        self.on_token = on_token
        self.on_check = on_check
        self.count = 0
        self._seen_prompt = False

    def put(self, value) -> None:
        if self.on_check is not None:
            self.on_check()
        if not self._seen_prompt:
            self._seen_prompt = True  # generate 第一次回传的是 prompt
            return
        self.count += int(value.numel()) if hasattr(value, "numel") else 1
        if self.on_token is not None:
            self.on_token(self.count)

    def end(self) -> None:
        return None


def build_prompt(prompt: str, hotwords: str) -> str:
    text = (prompt or "").strip() or DEFAULT_PROMPT
    words = (hotwords or "").strip()
    if words:
        text = f"{text}热词提示：{words}"
    return text


def transcribe(
    audio: dict[str, Any],
    prompt: str = DEFAULT_PROMPT,
    hotwords: str = "",
    max_new_tokens: int = 4096,
    max_length: int = 131072,
    device_name: str = "auto",
    dtype_name: str = "auto",
    attention: str = "sdpa",
    do_sample: bool = False,
    temperature: float = 0.0,
    top_p: float = 1.0,
    on_token: Callable[[int], None] | None = None,
    on_check: Callable[[], None] | None = None,
) -> dict[str, Any]:
    loaded = load_model(device_name, dtype_name, attention)
    waveform = comfy_audio_to_numpy(audio)
    seconds = waveform.shape[0] / TARGET_SAMPLE_RATE
    log(f"音频 {seconds:.1f}s / {waveform.shape[0]} 采样点, 提示词 {len(build_prompt(prompt, hotwords))} 字")

    processor = loaded.processor
    device = loaded.device

    messages = [
        {
            "role": "user",
            "content": [
                # 这里的 audio 只是占位, 真正的波形在下面直接传给 processor
                {"type": "audio", "audio": "waveform"},
                {"type": "text", "text": build_prompt(prompt, hotwords)},
            ],
        }
    ]

    text = processor.apply_chat_template(messages, tokenize=False, add_generation_prompt=True)
    if not isinstance(text, str):
        text = str(text)

    # 音频在 GPU 上做特征提取可以省一次 Host→Device 搬运, 失败就退回 CPU
    kwargs: dict[str, Any] = {"max_length": max_length, "return_tensors": "pt"}
    if device.type == "cuda":
        try:
            inputs = processor(text=text, audio=[waveform], audio_kwargs={"device": str(device)}, **kwargs)
        except Exception as exc:
            log(f"GPU 特征提取不可用 ({exc}), 改用 CPU")
            inputs = processor(text=text, audio=[waveform], **kwargs)
    else:
        inputs = processor(text=text, audio=[waveform], **kwargs)

    inputs = inputs.to(device)
    prompt_len = int(inputs["attention_mask"][0].sum().item())

    generation_config = copy.deepcopy(loaded.model.generation_config)
    generation_config.max_new_tokens = int(max_new_tokens)
    generation_config.do_sample = bool(do_sample)
    if do_sample:
        generation_config.temperature = float(temperature) if temperature > 0 else 0.7
        generation_config.top_p = float(top_p) if 0 < top_p <= 1 else 1.0

    streamer = _ProgressStreamer(on_token, on_check) if (on_token or on_check) else None
    generate_kwargs: dict[str, Any] = {
        "input_ids": inputs["input_ids"],
        "attention_mask": inputs["attention_mask"],
        "input_features": inputs["input_features"],
        "audio_feature_lengths": inputs["audio_feature_lengths"],
        "audio_chunk_mapping": inputs["audio_chunk_mapping"],
        "generation_config": generation_config,
    }
    if streamer is not None:
        generate_kwargs["streamer"] = streamer

    autocast = (
        torch.autocast("cuda", dtype=loaded.dtype)
        if device.type == "cuda" and loaded.dtype in (torch.float16, torch.bfloat16)
        else torch.no_grad()
    )
    with torch.inference_mode(), autocast:
        outputs = loaded.model.generate(**generate_kwargs)

    generated = outputs[0][prompt_len:]
    raw_text = processor.tokenizer.decode(generated, skip_special_tokens=True).strip()

    segments = parse_segments(raw_text)
    return {
        "text": raw_text,
        "segments": segments,
        "generated_tokens": int(generated.numel()),
        "duration": seconds,
        "prompt_len": prompt_len,
        "attention": loaded.attention,
    }


# --------------------------------------------------------------------------- 输出解析


@dataclass
class Segment:
    start: float
    end: float
    speaker: str
    text: str
    extra: dict[str, Any] = field(default_factory=dict)


def parse_segments(raw_text: str, clamp_to: float | None = None) -> list[Segment]:
    """把 [start][Sxx]正文[end] 解析成段落.

    说话人标记 [Sxx] 是**可选**的: 用「带时间戳、不带说话人」预设时, 模型只给
    [start]正文[end], 此时 speaker 留空, 生成的字幕里也就不会出现 [Sxx] 前缀。

    模型偶尔会漏标结尾时间戳或输出乱序时间, 这里做保守处理:
    结尾时间戳缺失的段落丢弃, 时间倒退的段落夹到上一段结束点.
    """
    seconds_marks = [(m.start(), m.end(), float(m.group(1))) for m in _TS_RE.finditer(raw_text)]

    segments: list[Segment] = []
    index = 0
    cursor = 0.0
    while index < len(seconds_marks):
        _, mark_end, start = seconds_marks[index]
        if index + 1 >= len(seconds_marks):
            break  # 没有结尾时间戳

        # 段首时间戳后面可能跟一个说话人标记, 也可能直接就是正文
        speaker_match = _SPK_RE.match(raw_text, mark_end)
        if speaker_match is not None:
            body_start = speaker_match.end()
            speaker = speaker_match.group(1)
        else:
            body_start = mark_end
            speaker = ""

        end_mark_start, _, end = seconds_marks[index + 1]
        body = raw_text[body_start:end_mark_start].strip()
        if end < start:
            end = start
        if end < cursor:
            start, end = cursor, max(cursor, end)
        if body:
            segments.append(Segment(start=max(start, cursor), end=end, speaker=speaker, text=body))
            cursor = end
        index += 1

    if clamp_to is not None and segments:
        last = segments[-1]
        if last.end > clamp_to:
            last.end = clamp_to
    return segments


def parse_speaker_map(spec: str) -> dict[str, str]:
    """解析 "S01=张三,S02=李四" 形式的改名字段."""
    mapping: dict[str, str] = {}
    for chunk in re.split(r"[,;\n]", spec or ""):
        chunk = chunk.strip()
        if not chunk or "=" not in chunk:
            continue
        key, value = chunk.split("=", 1)
        key = key.strip().upper()
        value = value.strip()
        if key.startswith("S") and key[1:].isdigit() and value:
            mapping[key] = value
    return mapping


def speaker_label(speaker: str, mapping: dict[str, str]) -> str:
    return mapping.get(speaker, speaker)


# --------------------------------------------------------------------------- 字幕格式


def _clock(seconds: float, comma: bool) -> str:
    seconds = max(0.0, float(seconds))
    hours, rest = divmod(int(seconds), 3600)
    minutes, whole = divmod(rest, 60)
    millis = int(round((seconds - int(seconds)) * 1000))
    if millis >= 1000:
        millis = 999
    separator = "," if comma else "."
    return f"{hours:02d}:{minutes:02d}:{whole:02d}{separator}{millis:03d}"


def to_srt(segments: list[Segment], mapping: dict[str, str] | None = None, show_speaker: bool = True) -> str:
    mapping = mapping or {}
    lines: list[str] = []
    for number, segment in enumerate(segments, start=1):
        label = speaker_label(segment.speaker, mapping)
        prefix = f"[{label}] " if (show_speaker and label) else ""
        lines.append(str(number))
        lines.append(f"{_clock(segment.start, True)} --> {_clock(segment.end, True)}")
        lines.append(f"{prefix}{segment.text}")
        lines.append("")
    return "\n".join(lines).strip() + ("\n" if lines else "")


def to_vtt(segments: list[Segment], mapping: dict[str, str] | None = None, show_speaker: bool = True) -> str:
    mapping = mapping or {}
    lines = ["WEBVTT", ""]
    for segment in segments:
        label = speaker_label(segment.speaker, mapping)
        prefix = f"<v {label}>" if (show_speaker and label) else ""
        lines.append(f"{_clock(segment.start, False)} --> {_clock(segment.end, False)}")
        lines.append(f"{prefix}{segment.text}")
        lines.append("")
    return "\n".join(lines)

def to_plain(segments: list[Segment], mapping: dict[str, str] | None = None) -> str:
    mapping = mapping or {}
    lines: list[str] = []
    for segment in segments:
        head = _clock(segment.start, False)
        label = speaker_label(segment.speaker, mapping)
        lines.append(f"{head} [{label}] {segment.text}" if label else f"{head} {segment.text}")
    return "\n".join(lines)


def to_segments_json(segments: list[Segment], mapping: dict[str, str] | None = None) -> str:
    import json

    mapping = mapping or {}
    payload = [
        {
            "start": round(s.start, 3),
            "end": round(s.end, 3),
            "speaker": speaker_label(s.speaker, mapping),
            "speaker_id": s.speaker,
            "text": s.text,
        }
        for s in segments
    ]
    return json.dumps(payload, ensure_ascii=False, indent=2)
