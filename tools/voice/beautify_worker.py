#!/usr/bin/env python3
"""人声美化 worker：神经网络降噪/去混响 + DSP 增强链（流式，内存有上限）。

在**独立 venv**里运行（clearvoice 依赖 numpy<2，与 ComfyUI 主环境冲突）：

    ~/.local/share/yuancut/voice/venv/bin/python beautify_worker.py \
        --in in.wav --out out.wav --strength 0.8 --progress progress.json

处理链：
1. ffmpeg 解码成 48 kHz 单声道临时 wav（不整段进内存）
2. 分块（默认 20 s，重叠 1 s）跑 MossFormer2_SE_48K 语音增强
3. 每块串 DSP 增强链：80 Hz 高通、250 Hz 去浑浊、3 kHz 存在感、
   4.5–8 kHz 动态去齿音、软扩张门限、压缩、峰值限制
4. 块间重叠区线性交叉淡化，写盘时逐块落，内存占用与音频长度无关
5. 干湿混合：strength 越小越接近原声

为什么要分块：整段送进 MossFormer2 时，注意力的中间张量随长度爆炸，
一段几分钟的素材就能吃掉几十 GB（实测 240 s 就超过 12 GB 虚拟内存），
进而把整个 ComfyUI 进程 cgroup 拖到被内核 OOM 杀掉。

模型权重放在 $VOICE_ROOT/checkpoints/MossFormer2_SE_48K/（由 setup_env.sh 下载）。
"""

from __future__ import annotations

import argparse
import json
import os
import resource
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import numpy as np

SR = 48000
MODEL_NAME = "MossFormer2_SE_48K"
DEFAULT_ROOT = os.path.expanduser("~/.local/share/yuancut/voice")

#: 分块长度（秒）：越大越快、越稳，但峰值内存越高
CHUNK_SECONDS = 20.0
#: 块间重叠（秒）：用于交叉淡化，避免接缝
OVERLAP_SECONDS = 1.0
#: 单进程地址空间上限：宁可干净报错，也不要把整机拖到 OOM
MEMORY_LIMIT_GB = 6.0


def log(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


def report(path: str | None, stage: str, progress: float) -> None:
    if not path:
        return
    try:
        with open(path, "w", encoding="utf-8") as handle:
            json.dump({"stage": stage, "progress": round(float(progress), 4)}, handle)
    except OSError:
        pass


def limit_memory(limit_gb: float) -> None:
    """限制本进程地址空间，超了抛 MemoryError 而不是被内核 OOM 杀。"""
    try:
        limit = int(limit_gb * 1024**3)
        soft, hard = resource.getrlimit(resource.RLIMIT_AS)
        if hard != resource.RLIM_INFINITY:
            limit = min(limit, hard)
        resource.setrlimit(resource.RLIMIT_AS, (limit, hard))
    except (ValueError, OSError) as exc:  # pragma: no cover - 平台差异
        log(f"[voice] 内存上限设置失败（忽略）：{exc}")


def peak_rss_mb() -> float:
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0


# ----------------------------------------------------------------------- 解码


def decode_to_wav(source: str, target: Path) -> float:
    """用系统 ffmpeg 解码成 48 kHz 单声道临时 wav，返回时长（秒）。"""
    command = [
        "ffmpeg",
        "-v",
        "error",
        "-nostdin",
        "-y",
        "-i",
        source,
        "-ac",
        "1",
        "-ar",
        str(SR),
        "-c:a",
        "pcm_s16le",
        str(target),
    ]
    result = subprocess.run(command, capture_output=True, check=False)
    if not target.is_file() or target.stat().st_size <= 44:
        detail = result.stderr.decode("utf-8", "ignore")[:400]
        raise SystemExit(f"ffmpeg 解码失败：{detail}")

    import soundfile as sf

    info = sf.info(str(target))
    return float(info.frames) / float(info.samplerate or SR)


# ----------------------------------------------------------------------- 模型


class Enhancer:
    """常驻的 MossFormer2_SE_48K，按块推理。"""

    def __init__(self, root: str) -> None:
        checkpoint_dir = Path(root) / "checkpoints" / MODEL_NAME
        if not (checkpoint_dir / "last_best_checkpoint.pt").is_file():
            raise SystemExit(
                f"缺少模型权重：{checkpoint_dir}\n请先运行 tools/voice/setup_env.sh 下载。"
            )

        import torch

        # 线程数直接影响内存与稳定性，限制一下更可控
        torch.set_num_threads(min(8, os.cpu_count() or 4))

        from clearvoice import ClearVoice

        previous = os.getcwd()
        os.chdir(root)  # ClearVoice 从相对 CWD 的 checkpoints/<model>/ 找权重
        try:
            self._model = ClearVoice(task="speech_enhancement", model_names=[MODEL_NAME])
        finally:
            os.chdir(previous)

    def enhance(self, chunk: np.ndarray) -> np.ndarray:
        import torch

        with torch.inference_mode():
            out = self._model.models[0].decode_data(chunk[None, :].astype(np.float32))
        if isinstance(out, (list, tuple)):
            out = out[0]
        if hasattr(out, "detach"):
            out = out.detach().cpu().numpy()
        out = np.asarray(out, dtype=np.float32).reshape(-1)
        if out.size < chunk.size:
            out = np.pad(out, (0, chunk.size - out.size))
        return out[: chunk.size]


# ------------------------------------------------------------------------- DSP


def _biquad_peaking(x: np.ndarray, freq: float, gain_db: float, q: float) -> np.ndarray:
    """RBJ peaking EQ。"""
    from scipy.signal import lfilter

    a = 10 ** (gain_db / 40.0)
    w0 = 2 * np.pi * freq / SR
    alpha = np.sin(w0) / (2 * q)
    cos_w0 = np.cos(w0)

    b0 = 1 + alpha * a
    b1 = -2 * cos_w0
    b2 = 1 - alpha * a
    a0 = 1 + alpha / a
    a1 = -2 * cos_w0
    a2 = 1 - alpha / a
    return lfilter(np.array([b0, b1, b2]) / a0, np.array([1.0, a1 / a0, a2 / a0]), x)


def _smooth_asym(mag: np.ndarray, attack_ms: float, release_ms: float) -> np.ndarray:
    """向量化的非对称包络：快起（攻击）、慢落（释放）。

    取「快平滑」与「慢平滑」的逐点最小值即可近似，完全不用 Python 循环。
    """
    from scipy.signal import lfilter

    a_atk = float(np.exp(-1.0 / (SR * attack_ms / 1000.0)))
    a_rel = float(np.exp(-1.0 / (SR * release_ms / 1000.0)))
    fast = lfilter([1 - a_atk], [1, -a_atk], mag)
    slow = lfilter([1 - a_rel], [1, -a_rel], mag)
    return np.minimum(fast, slow)


def dsp_enhance(x: np.ndarray, strength: float, makeup: float | None = None) -> np.ndarray:
    """确定性的增强链：EQ + 去齿音 + 软扩张门 + 压缩 + 限幅（float32 为主）。"""
    from scipy.signal import butter, lfilter

    s = float(np.clip(strength, 0.0, 1.0))
    y = x.astype(np.float32, copy=True)

    # 1) 80 Hz 高通：去掉隆隆声与喷麦的低频冲击
    b, a = butter(2, 80.0 / (SR / 2), btype="highpass")
    y = lfilter(b, a, y).astype(np.float32, copy=False)

    # 2) 浑浊削减 / 存在感提升（强度线性缩放）
    y = _biquad_peaking(y, 250.0, -2.0 * s, 0.9).astype(np.float32, copy=False)
    y = _biquad_peaking(y, 3000.0, 2.5 * s, 1.0).astype(np.float32, copy=False)

    # 3) de-esser：只对 4.5–8 kHz 频段做动态衰减（齿音/口水音主要能量区）
    bh, ah = butter(2, [4500.0 / (SR / 2), 8000.0 / (SR / 2)], btype="bandpass")
    band = lfilter(bh, ah, y).astype(np.float32, copy=False)
    env = _smooth_asym(np.abs(band), attack_ms=1.0, release_ms=60.0)
    floor = 10 ** (-34.0 / 20.0)
    ratio = 1.0 + 2.5 * s
    gain = np.ones_like(env)
    over = env > floor
    if np.any(over):
        gain[over] = (floor * (env[over] / floor) ** (1.0 / ratio)) / env[over]
    gain = _smooth_asym(gain, attack_ms=1.0, release_ms=40.0)
    y = y - band * (1.0 - gain).astype(np.float32)
    del band, env, gain, over

    # 4) 软扩张门：模型之后仍有残留底噪，句间把它压下去（不起「抽气」感）
    level = _smooth_asym(np.abs(y), attack_ms=5.0, release_ms=180.0)
    level_db = 20.0 * np.log10(level + 1e-12)
    amount = np.clip((-42.0 + 8.0 - level_db) / 8.0, 0.0, 1.0)
    gate = _smooth_asym(10 ** ((-14.0 * s * amount) / 20.0), attack_ms=8.0, release_ms=160.0)
    y = y * gate.astype(np.float32)
    del level, level_db, amount, gate

    # 5) 软压缩：阈值 -22 dBFS，比率随强度上升
    level = _smooth_asym(np.abs(y), attack_ms=8.0, release_ms=140.0)
    threshold = 10 ** (-22.0 / 20.0)
    comp_ratio = 1.0 + 1.8 * s
    cg = np.ones_like(level)
    loud = level > threshold
    if np.any(loud):
        cg[loud] = (threshold * (level[loud] / threshold) ** (1.0 / comp_ratio)) / level[loud]
    cg = _smooth_asym(cg, attack_ms=6.0, release_ms=120.0)
    y = y * cg.astype(np.float32)
    del level, cg, loud

    # 6) 补偿增益：优先用全局值（分块时每块各算会造成 20 s 一次的响度跳变）
    if makeup is None:
        frame = int(0.05 * SR)
        count = max(1, y.size // frame)
        blocked = y[: count * frame].reshape(count, frame)
        speech_rms = float(np.percentile(np.sqrt((blocked**2).mean(axis=1) + 1e-20), 95))
        makeup = float(np.clip((10 ** (-20.0 / 20.0)) / (speech_rms + 1e-12), 0.5, 6.0))
    y = y * (1.0 + (makeup - 1.0) * s)

    # 7) 软限幅 + 峰值 -1 dBFS
    y = np.tanh(y * 1.02) / 1.02
    peak = float(np.max(np.abs(y))) if y.size else 0.0
    if peak > 0:
        y = y * min(1.0, 10 ** (-1.0 / 20.0) / peak)
    return y.astype(np.float32, copy=False)


# ---------------------------------------------------------------------- 主流程

def normalize_output(path: str, progress_path: str | None) -> None:
    """把整段语音响度校正到 95 分位帧 RMS ≈ -20 dBFS，并保证峰值不超过 -1 dBFS。

    流式分块时补偿增益只能用源音频估算，块与块之间的整体电平会与目标有偏差，
    这里再扫一遍成品统一校正（顺序读写，内存不随长度增长）。
    """
    import soundfile as sf

    report(progress_path, "normalizing", 0.97)
    frame = int(0.05 * SR)
    levels: list[np.ndarray] = []
    with sf.SoundFile(path, mode="r") as source:
        while True:
            block = source.read(frame * 200, dtype="float32", always_2d=False)
            if block.size == 0:
                break
            count = block.size // frame
            if count:
                chunked = block[: count * frame].reshape(count, frame)
                levels.append(np.sqrt((chunked**2).mean(axis=1) + 1e-20))
    if not levels:
        return

    speech_rms = float(np.percentile(np.concatenate(levels), 95))
    gain = float(np.clip((10 ** (-20.0 / 20.0)) / (speech_rms + 1e-12), 0.25, 8.0))
    if abs(gain - 1.0) < 0.01:
        return

    temporary = f"{path}.norm"
    with sf.SoundFile(path, mode="r") as source, sf.SoundFile(
        temporary, mode="w", samplerate=SR, channels=1, subtype="PCM_24", format="WAV"
    ) as target:
        peak = 0.0
        while True:
            block = source.read(1 << 18, dtype="float32", always_2d=False)
            if block.size == 0:
                break
            scaled = (block * gain).astype(np.float32)
            peak = max(peak, float(np.max(np.abs(scaled))))
            target.write(scaled)
    # 归一化后可能顶到 0 dBFS，超过 -1 dBFS 再整体压一下
    if peak > 10 ** (-1.0 / 20.0) and peak > 0:
        scale = 10 ** (-1.0 / 20.0) / peak
        with sf.SoundFile(temporary, mode="r") as source, sf.SoundFile(
            f"{temporary}.lim",
            mode="w",
            samplerate=SR,
            channels=1,
            subtype="PCM_24",
            format="WAV",
        ) as target:
            while True:
                block = source.read(1 << 18, dtype="float32", always_2d=False)
                if block.size == 0:
                    break
                target.write((block * scale).astype(np.float32))
        os.replace(f"{temporary}.lim", temporary)
    os.replace(temporary, path)
    log(f"[voice] 输出响度校正 x{gain:.2f}")
def run(
    *,
    input_path: str,
    output_path: str,
    strength: float,
    root: str,
    progress_path: str | None,
    no_neural: bool,
) -> None:
    import soundfile as sf

    report(progress_path, "decoding", 0.03)
    with tempfile.TemporaryDirectory(prefix="yuancut-voice-") as workdir:
        decoded = Path(workdir) / "source.wav"
        duration = decode_to_wav(input_path, decoded)
        log(f"[voice] 解码 {input_path} → {duration:.2f}s @ {SR}Hz")

        enhancer = None if no_neural else Enhancer(root)
        if enhancer is not None:
            log(f"[voice] 模型就绪，峰值内存 {peak_rss_mb():.0f} MB")
        report(progress_path, "enhancing", 0.12)

        chunk_frames = int(CHUNK_SECONDS * SR)
        overlap_frames = int(OVERLAP_SECONDS * SR)
        hop_frames = chunk_frames - overlap_frames


        # 全局补偿增益：扫一遍源音频算 95 分位帧 RMS，避免逐块各算造成响度跳变
        global_makeup = None
        if strength > 0:
            frame = int(0.05 * SR)
            frame_rms: list[np.ndarray] = []
            scan = sf.SoundFile(str(decoded), mode="r")
            try:
                scan.seek(0)
                while True:
                    block = scan.read(frame * 200, dtype="float32", always_2d=False)
                    if block.size == 0:
                        break
                    count = block.size // frame
                    if count:
                        chunked = block[: count * frame].reshape(count, frame)
                        frame_rms.append(np.sqrt((chunked**2).mean(axis=1) + 1e-20))
            finally:
                scan.close()
            if frame_rms:
                speech_rms = float(np.percentile(np.concatenate(frame_rms), 95))
                global_makeup = float(
                    np.clip((10 ** (-20.0 / 20.0)) / (speech_rms + 1e-12), 0.5, 6.0)
                )
                log(f"[voice] 全局增益 x{global_makeup:.2f}")
        source = sf.SoundFile(str(decoded), mode="r")
        total = len(source)
        tail: np.ndarray | None = None
        written = 0

        target = sf.SoundFile(
            output_path, mode="w", samplerate=SR, channels=1, subtype="PCM_24"
        )
        try:
            position = 0
            while position < total:
                source.seek(position)
                chunk = source.read(chunk_frames, dtype="float32", always_2d=False)
                if chunk.ndim > 1:
                    chunk = chunk.mean(axis=1)
                if chunk.size == 0:
                    break
                dry = chunk.copy()

                wet_source = dry if enhancer is None else enhancer.enhance(dry)
                wet = dsp_enhance(wet_source, strength, global_makeup)
                mixed = ((1.0 - strength) * dry + strength * wet).astype(np.float32)
                del dry, wet_source, wet

                # 与上一块的尾巴交叉淡化，消掉接缝
                if tail is not None and tail.size:
                    n = min(tail.size, mixed.size)
                    ramp = np.linspace(0.0, 1.0, n, dtype=np.float32)
                    mixed[:n] = tail[:n] * (1.0 - ramp) + mixed[:n] * ramp

                is_last = position + chunk.size >= total
                if is_last:
                    block = mixed
                    tail = None
                    position = total
                else:
                    block = mixed[:hop_frames]
                    tail = mixed[hop_frames : hop_frames + overlap_frames].copy()
                    position += hop_frames

                if block.size:
                    target.write(block)
                    written += block.size

                del mixed
                progress = 0.12 + 0.83 * min(1.0, written / max(1, total))
                report(progress_path, "enhancing", progress)
        finally:
            source.close()
            target.close()

    # 输出侧响度归一化：分块时补偿增益按整段源音频估算，最后统一校正一次
    normalize_output(output_path, progress_path)

    report(progress_path, "done", 1.0)
    log(
        f"[voice] 完成 → {output_path}，{written / SR:.2f}s，峰值内存 {peak_rss_mb():.0f} MB"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="人声美化：神经网络 + DSP 增强链（流式）")
    parser.add_argument("--in", dest="input_path", required=True)
    parser.add_argument("--out", dest="output_path", required=True)
    parser.add_argument("--strength", type=float, default=1.0)
    parser.add_argument("--progress", default=None)
    parser.add_argument("--root", default=os.environ.get("VOICE_ROOT", DEFAULT_ROOT))
    parser.add_argument("--memory-limit-gb", type=float, default=MEMORY_LIMIT_GB)
    parser.add_argument("--no-neural", action="store_true", help="只跑 DSP 链（调试用）")
    args = parser.parse_args()

    limit_memory(args.memory_limit_gb)
    started = time.time()
    run(
        input_path=args.input_path,
        output_path=args.output_path,
        strength=float(np.clip(args.strength, 0.0, 1.0)),
        root=args.root,
        progress_path=args.progress,
        no_neural=args.no_neural,
    )
    log(f"[voice] 总用时 {time.time() - started:.1f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
