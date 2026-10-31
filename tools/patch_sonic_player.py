#!/usr/bin/env python3
"""第五版：把 Sonic 从 AudioWorklet 里搬出来。

上一版的问题：Sonic 没有 reset()，在 worklet 里 seek 只能销毁重建流，
重建瞬间内部相位/缓冲错位 → 输出出现空洞和半幅度跳变（实测 RMS 0.136、跳变 0.4996）。

这一版：
  * 引擎在**主线程**按需分块渲染（无实时约束，测过 20 秒音频仅 11 ms）
  * AudioWorklet 退化成**纯播放器**（只吃已渲染好的 PCM，双缓冲 FIFO）
  * seek / 改速 = 从新位置重新渲染一小块再喂进去，播放器状态不变

用法（build_ui.sh 已在 patch_time_stretch.py 之后调用）：
  YUANCUT_WORK=/tmp/xxx python3 tools/patch_sonic_player.py
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

if len(sys.argv) > 1:
    ROOT = Path(sys.argv[1]).expanduser().resolve()
else:
    ROOT = Path(os.environ["YUANCUT_WORK"]).expanduser().resolve()

WEB = ROOT / "apps" / "web"
SRC = WEB / "src"


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    print(f"  wrote   {path.relative_to(ROOT)}")


def patch(path: Path, old: str, new: str, *, required: bool = True) -> None:
    if not path.exists():
        print(f"  skip    {path.name} (不存在)")
        return
    text = path.read_text(encoding="utf-8")
    if new in text:
        print(f"  keep    {path.relative_to(ROOT)}")
        return
    if old not in text:
        if not required:
            print(f"  skip    {path.relative_to(ROOT)}")
            return
        raise SystemExit(f"anchor not found in {path}:\n{old[:200]}")
    path.write_text(text.replace(old, new, 1), encoding="utf-8")
    print(f"  patched {path.relative_to(ROOT)}")


# --------------------------------------------------------------- 播放器 worklet
PLAYER_SRC = r'''// 纯播放器：只吃已渲染好的 PCM，不跑任何 DSP。
/**
 * 软限幅：变速 + 音量叠加后峰值可能超过 1.0，直接交给声卡会硬削波（破音）。
 * 0.95 以下原样通过，以上用 tanh 平滑压到 1.0 以内。
 */
function softClip(v) {
	const a = Math.abs(v);
	if (a <= 0.95) return v;
	const limited = 0.95 + 0.05 * Math.tanh((a - 0.95) / 0.05);
	return v < 0 ? -limited : limited;
}

/**
 * 所有播放状态都必须是**实例字段**。
 *
 * AudioWorklet 的模块脚本在一个全局作用域里只求值一次，模块级变量由同一个
 * 处理器的所有实例共享。之前把 fifo / playing / startAt 写在模块级，多个片段
 * 各开一个播放器时它们会抢同一个 FIFO：谁先跑到 process 谁把 PCM 读走，
 * 其余的只输出静音 —— 表现就是「多片段变速后只有一个有声，其余无声」。
 */
class SonicPlayer extends AudioWorkletProcessor {
	constructor() {
		super();
		this.fifo = [];
		this.fifoLen = 0;
		this.playing = false;
		this.startAt = 0;
		this.port.onmessage = (event) => {
			const msg = event.data;
			if (msg.type === "pcm") {
				this.fifo.push({ l: new Float32Array(msg.left), r: new Float32Array(msg.right), off: 0 });
				this.fifoLen += msg.left.byteLength / 4;
			} else if (msg.type === "start") {
				this.startAt = msg.at || 0;
				this.playing = true;
			} else if (msg.type === "clear") {
				this.fifo = [];
				this.fifoLen = 0;
				this.playing = false;
			}
		};
	}

	process(_inputs, outputs) {
		const out = outputs[0];
		if (!out || out.length === 0) return true;
		const c0 = out[0];
		const c1 = out[1] || out[0];
		const frames = c0.length;
		c0.fill(0);
		c1.fill(0);
		if (!this.playing) return true;
		if (currentTime + frames / sampleRate <= this.startAt) return true;

		let written = 0;
		while (written < frames && this.fifo.length > 0) {
			const head = this.fifo[0];
			const n = Math.min(frames - written, head.l.length - head.off);
			for (let i = 0; i < n; i++) {
				c0[written + i] = softClip(head.l[head.off + i]);
				c1[written + i] = softClip(head.r[head.off + i]);
			}
			head.off += n;
			written += n;
			this.fifoLen -= n;
			if (head.off >= head.l.length) this.fifo.shift();
		}
		return true;
	}
}

registerProcessor("sonic-player", SonicPlayer);
'''

# ------------------------------------------------------- 引擎（主线程分块渲染）
ENGINE_TS = '''import { SONIC_PLAYER_SRC } from "./sonic-player-source";
import { SONIC_WASM_B64 } from "./sonic-wasm";

/**
 * 变速不变调 · 第五版
 *
 * 引擎（Sonic）在**主线程**按需渲染分块 PCM，AudioWorklet 只负责播放：
 * 这样 seek / 改速不会打断引擎，也不存在"销毁重建流"造成的错位。
 *
 * 实测：Sonic 渲染 20 秒音频约 11 ms → 每次渲染 1~4 秒只需 1~3 ms，延迟不可感知。
 *
 * 关闭：localStorage["yuancut:disable-time-stretch"] = "1"
 */

export type PlayerNode = AudioWorkletNode & { stop: () => void; disconnect: () => void };
export type StretchSource = { stop: (when?: number) => void; disconnect: () => void };

const DISABLE_KEY = "yuancut:disable-time-stretch";
const MIN_RATE_DELTA = 1e-3;
const PROCESSOR = "sonic-player";
/** 一次渲染多少秒（越大 seek 后填充越慢，越小消息越频繁） */
const CHUNK_SECONDS = 4;

let wasmPromise: Promise<Record<string, any> | null> | null = null;
let unavailable = false;

export function isTimeStretchDisabled(): boolean {
	if (typeof window === "undefined") return true;
	try {
		return window.localStorage.getItem(DISABLE_KEY) === "1";
	} catch {
		return false;
	}
}

export function shouldPreservePitch({
	rate,
	preservesPitch,
}: {
	rate: number;
	preservesPitch?: boolean;
}): boolean {
	if (preservesPitch === false) return false;
	if (unavailable) return false;
	if (!Number.isFinite(rate) || rate <= 0) return false;
	if (Math.abs(rate - 1) < MIN_RATE_DELTA) return false;
	return !isTimeStretchDisabled();
}

async function getWasm(): Promise<Record<string, any> | null> {
	if (unavailable) return null;
	if (!wasmPromise) {
		wasmPromise = (async () => {
			try {
				const bin = Uint8Array.from(atob(SONIC_WASM_B64), (c) => c.charCodeAt(0));
				const { instance } = await WebAssembly.instantiate(bin.buffer, {});
				return instance.exports as Record<string, any>;
			} catch (error) {
				unavailable = true;
				console.warn("[time-stretch] wasm 载入失败", error);
				return null;
			}
		})();
	}
	return wasmPromise;
}

/**
 * 从 buffer 的 startTime 处连续取 sourceFrames 帧，按 rate 变速（保音高），
 * 返回渲染好的 PCM（已交错拆成两声道）。
 */
export async function renderChunk({
	buffer,
	startTime,
	sourceSeconds,
	rate,
}: {
	buffer: AudioBuffer;
	startTime: number;
	sourceSeconds: number;
	rate: number;
}): Promise<{ left: Float32Array; right: Float32Array } | null> {
	const wasm = await getWasm();
	if (!wasm) return null;

	const sampleRate = buffer.sampleRate;
	const startSample = Math.max(0, Math.floor(startTime * sampleRate));
	const endSample = Math.min(buffer.length, Math.ceil((startTime + sourceSeconds) * sampleRate));
	const inFrames = endSample - startSample;
	if (inFrames <= 1) return null;

	const stream = wasm.sonicCreateStream(sampleRate, 2);
	if (!stream) return null;
	wasm.sonicSetSpeed(stream, rate);
	wasm.sonicSetPitch(stream, 1.0);

	const leftSrc = buffer.getChannelData(0);
	const rightSrc = buffer.numberOfChannels > 1 ? buffer.getChannelData(1) : leftSrc;
	const outCap = Math.ceil(inFrames / rate) + sampleRate;
	const outPtr = wasm.malloc(outCap * 2 * 4);
	const inPtr = wasm.malloc(4096 * 2 * 4);
	const chunks: Float32Array[] = [];
	let total = 0;

	const pull = () => {
		let guard = 0;
		while (guard++ < 32) {
			const avail = wasm.sonicSamplesAvailable(stream);
			if (avail <= 0) break;
			const want = Math.min(avail, outCap);
			const got = wasm.sonicReadFloatFromStream(stream, outPtr, want);
			if (got <= 0) break;
			chunks.push(new Float32Array(new Float32Array(wasm.memory.buffer, outPtr, got * 2)));
			total += got;
		}
	};

	let pos = startSample;
	while (pos < endSample) {
		const n = Math.min(4096, endSample - pos);
		const heap = new Float32Array(wasm.memory.buffer, inPtr, n * 2);
		for (let i = 0; i < n; i++) {
			heap[i * 2] = leftSrc[pos + i];
			heap[i * 2 + 1] = rightSrc[pos + i];
		}
		wasm.sonicWriteFloatToStream(stream, inPtr, n);
		pos += n;
		pull();
	}
	wasm.sonicFlushStream(stream);
	pull();

	wasm.free(outPtr);
	wasm.free(inPtr);
	wasm.sonicDestroyStream(stream);

	const left = new Float32Array(total);
	const right = new Float32Array(total);
	let offset = 0;
	for (const chunk of chunks) {
		for (let i = 0; i < chunk.length / 2; i++) {
			left[offset + i] = chunk[i * 2];
			right[offset + i] = chunk[i * 2 + 1];
		}
		offset += chunk.length / 2;
	}
	return { left, right };
}

/** 每次渲染覆盖的时间轴时长 */
export function chunkTimelineSeconds({ rate }: { rate: number }): number {
	// 分块长度与速度无关：变速只改变源消耗量，不改变一块覆盖的时间轴秒数。
	// （rate 留在签名里是为了调用处可读，这里显式忽略。）
	void rate;
	return CHUNK_SECONDS;
}


/** 播放器 worklet 每个 AudioContext 只注册一次。 */
const workletReady = new WeakMap<BaseAudioContext, Promise<boolean>>();

function ensureWorklet(audioContext: BaseAudioContext): Promise<boolean> {
	const ready = workletReady.get(audioContext);
	if (ready) return ready;
	const blob = new Blob([SONIC_PLAYER_SRC], { type: "text/javascript" });
	const url = URL.createObjectURL(blob);
	const pending = audioContext.audioWorklet
		.addModule(url)
		.then(() => true)
		.catch((error: unknown) => {
			// 注册失败就退回上游的 playbackRate 方式，至少还有声
			console.warn("[time-stretch] 播放器 worklet 注册失败", error);
			return false;
		})
		.finally(() => URL.revokeObjectURL(url));
	workletReady.set(audioContext, pending);
	return pending;
}

/** 创建一个纯播放器节点。 */
export async function createPlayerNode({
	audioContext,
}: {
	audioContext: BaseAudioContext;
}): Promise<PlayerNode | null> {
	if (!(await ensureWorklet(audioContext))) return null;

	let node: AudioWorkletNode;
	try {
		node = new AudioWorkletNode(audioContext, PROCESSOR, {
			numberOfInputs: 0,
			numberOfOutputs: 1,
			outputChannelCount: [2],
		});
	} catch (error) {
		console.warn("[time-stretch] 播放器节点创建失败", error);
		return null;
	}

	const player = node as PlayerNode;
	player.stop = () => {
		try {
			node.port.postMessage({ type: "clear" });
		} catch {}
	};
	return player;
}

export function pushPcm({
	node,
	left,
	right,
}: {
	node: PlayerNode;
	left: Float32Array;
	right: Float32Array;
}): void {
	node.port.postMessage({ type: "pcm", left, right }, [left.buffer, right.buffer]);
}

export function playFrom({ node, at }: { node: PlayerNode; at: number }): void {
	node.port.postMessage({ type: "start", at });
}
'''

PLAYER_SRC_TS = (
    "export const SONIC_PLAYER_SRC = " + json.dumps(PLAYER_SRC, ensure_ascii=False) + ";\n"
)

_am_path = SRC / "core" / "managers" / "audio-manager.ts"
_am_txt = _am_path.read_text(encoding="utf-8") if _am_path.exists() else ""
if _am_txt:
    _fs = _am_txt.find("\t/** 每个片段一条常驻伸缩节点")
    _fe = _am_txt.find("\t>();", _fs)
    if _fs != -1 and _fe > _fs:
        _am_txt = _am_txt[:_fs] + _am_txt[_fe + len("\t>();\n"):]
        _am_path.write_text(_am_txt, encoding="utf-8")
        print("  patched audio-manager.ts (移除遗留 sonicVoices 字段)")

print(f"Patching Sonic player into {ROOT}")
write(SRC / "lib" / "audio" / "sonic-engine.ts", ENGINE_TS)
write(SRC / "lib" / "audio" / "sonic-player-source.ts", PLAYER_SRC_TS)

AUDIO_MANAGER = SRC / "core" / "managers" / "audio-manager.ts"

# 换成新的引擎/播放器模块：整段 import 重写，不依赖上游/前序补丁的具体写法
_am0 = AUDIO_MANAGER.read_text(encoding="utf-8") if AUDIO_MANAGER.exists() else ""
if _am0:
    _new_import = (
        "import {\n"
        "\tchunkTimelineSeconds,\n"
        "\tcreatePlayerNode,\n"
        "\tplayFrom,\n"
        "\tpushPcm,\n"
        "\trenderChunk,\n"
        "\tshouldPreservePitch,\n"
        "\ttype PlayerNode,\n"
        "\ttype StretchSource,\n"
        '} from "@/lib/audio/sonic-engine";'
    )
    _am0, _count = re.subn(
        r'import \{[^}]*\} from "@/lib/audio/sonic-(?:stretch|engine)";',
        _new_import,
        _am0,
        count=1,
    )
    if _count == 0:
        raise SystemExit(f"audio-manager import block not found in {AUDIO_MANAGER}")
    # 旧实现留下的字段（新的 stopPlayback 不再用它）
    _am0 = re.sub(
        r"\t/\*\* 伸缩节点 -> 它的 gain，用于停止时做淡出。 \*/\n"
        r"\tprivate readonly fadeOutGains = new Map<StretchSource, GainNode>\(\);\n",
        "",
        _am0,
    )
    AUDIO_MANAGER.write_text(_am0, encoding="utf-8")
    print("  patched audio-manager.ts (imports -> sonic-engine)")

# --- 接线：scheduleSonicClip 换成「主线程渲染 + 纯播放器 worklet + 续渲染泵」 ---
_am = AUDIO_MANAGER.read_text(encoding="utf-8") if AUDIO_MANAGER.exists() else ""
if _am:
    _start = _am.find("\tprivate async scheduleSonicClip({")
    _end = _am.find("\t/** 上游路径：直接改 playbackRate")
    if _start != -1 and _end > _start:
        _feeder = """\t/** 每个片段的续渲染状态 */
\tprivate readonly sonicFeeders = new Map<
\t\tstring,
\t\t{
\t\t\tnode: PlayerNode;
\t\t\tbuffer: AudioBuffer;
\t\t\trate: number;
\t\t\tstartAtCtx: number;
\t\t\tstartTimeline: number;
\t\t\tnextSourceTime: number;
\t\t\trenderedEndTimeline: number;
\t\t\tremainingTimeline: number;
\t\t\t/** 正在渲染中：防止定时器和立即补渲染同时跑同一个 feeder */
\t\t\tbusy: boolean;
\t\t}
\t>();
\tprivate readonly sonicPlayers = new Map<string, PlayerNode>();
\tprivate sonicFeedTimer: number | null = null;

\tprivate ensureSonicFeedTimer(): void {
\t\tif (this.sonicFeedTimer !== null || typeof window === "undefined") return;
\t\tthis.sonicFeedTimer = window.setInterval(() => {
\t\t\tvoid this.pumpSonicFeeders();
\t\t}, 250);
\t}

\t/**
\t * 续渲染泵：只用 AudioContext 时钟推算「已经播掉多少秒」，
\t * 不依赖时间轴 getCurrentTime()（两者刻度不一致时泵会失效，
\t * 表现为播几秒就断、重新排程再响，即"一下有声音一下没声音"）。
\t */
\tprivate async pumpSonicFeeders(): Promise<void> {
\t\tconst ctx = this.audioContext;
\t\tif (!ctx || !this.editor.playback.getIsPlaying()) return;
\t\tfor (const [key, feeder] of [...this.sonicFeeders]) {
\t\t\tif (feeder.busy) continue;
\t\t\tif (feeder.remainingTimeline <= 0.01) continue;
\t\t\tconst played = ctx.currentTime - feeder.startAtCtx;
\t\t\tif (played < 0) continue;
\t\t\tconst buffered = () =>
\t\t\t\tfeeder.renderedEndTimeline -
\t\t\t\tfeeder.startTimeline -
\t\t\t\t(ctx.currentTime - feeder.startAtCtx);
\t\t\tif (buffered() > 2) continue;
\t\t\tconst rate = feeder.rate;
\t\t\tfeeder.busy = true;
\t\t\ttry {
\t\t\t\tlet guard = 0;
\t\t\t\twhile (feeder.remainingTimeline > 0.01 && buffered() < 6 && guard++ < 4) {
\t\t\t\t\tconst seconds = Math.min(
\t\t\t\t\t\tchunkTimelineSeconds({ rate }),
\t\t\t\t\t\tfeeder.remainingTimeline,
\t\t\t\t\t);
\t\t\t\t\tconst chunk = await renderChunk({
\t\t\t\t\t\tbuffer: feeder.buffer,
\t\t\t\t\t\tstartTime: feeder.nextSourceTime,
\t\t\t\t\t\tsourceSeconds: seconds * rate,
\t\t\t\t\t\trate,
\t\t\t\t\t});
\t\t\t\t\tif (!chunk) break;
\t\t\t\t\t// 渲染期间可能被 seek / 改速 / 换素材重新排程：
\t\t\t\t\t// 只有「同一个 feeder 对象、同一个速率」才允许把 PCM 推进播放器，
\t\t\t\t\t// 否则这块数据会把播放器的 FIFO 顶乱（听感就是忽大忽小、破音、断音）
\t\t\t\t\tif (this.sonicFeeders.get(key) !== feeder || feeder.rate !== rate) break;
\t\t\t\t\tpushPcm({ node: feeder.node, left: chunk.left, right: chunk.right });
\t\t\t\t\tfeeder.nextSourceTime += seconds * rate;
\t\t\t\t\tfeeder.renderedEndTimeline += seconds;
\t\t\t\t\tfeeder.remainingTimeline -= seconds;
\t\t\t\t}
\t\t\t} finally {
\t\t\t\tfeeder.busy = false;
\t\t\t}
\t\t}
\t}

\tprivate async scheduleSonicClip({
\t\tclip,
\t\tbuffer,
\t\ttime,
\t\tsessionId,
\t}: {
\t\tclip: AudioClipSource;
\t\tbuffer: AudioBuffer;
\t\ttime: number;
\t\tsessionId: number;
\t}): Promise<boolean> {
\t\tconst audioContext = this.audioContext;
\t\tconst masterGain = this.masterGain;
\t\tif (!audioContext || !masterGain) return false;

\t\tconst rate = clip.playbackRate;
\t\tconst elapsed = Math.max(0, time - clip.startTime);
\t\tconst remainingDuration = clip.duration - elapsed;
\t\tif (remainingDuration <= 0) return true;

\t\tconst key = clip.id;
\t\tlet player = this.sonicPlayers.get(key);
\t\tif (!player) {
\t\t\tconst created = await createPlayerNode({ audioContext });
\t\t\tif (!created) return false;
\t\t\tplayer = created;
\t\t\tthis.sonicPlayers.set(key, player);
\t\t}

\t\tconst timelineStart = Math.max(clip.startTime, time);
\t\tconst scheduleTime =
\t\t\tthis.playbackStartContextTime + (timelineStart - this.playbackStartTime);
\t\tconst startAt = Math.max(scheduleTime, audioContext.currentTime + 0.02);

\t\tconst firstSeconds = Math.min(
\t\t\tchunkTimelineSeconds({ rate }),
\t\t\tremainingDuration,
\t\t);
\t\tconst sourceStart = clip.trimStart + elapsed * rate;
\t\tconst chunk = await renderChunk({
\t\t\tbuffer,
\t\t\tstartTime: sourceStart,
\t\t\tsourceSeconds: firstSeconds * rate,
\t\t\trate,
\t\t});
\t\tif (!chunk) return false;

\t\tif (
\t\t\tsessionId !== this.playbackSessionId ||
\t\t\t!this.editor.playback.getIsPlaying()
\t\t) {
\t\t\treturn false;
\t\t}

\t\tplayer.stop();
\t\tconst volume = audioContext.createGain();
\t\tvolume.gain.setValueAtTime(0, startAt);
\t\tvolume.gain.linearRampToValueAtTime(clip.volume, startAt + 0.012);
\t\tpushPcm({ node: player, left: chunk.left, right: chunk.right });
\t\ttry {
\t\t\tplayer.disconnect();
\t\t} catch {}
\t\tplayer.connect(volume);
\t\tvolume.connect(masterGain);
\t\tplayFrom({ node: player, at: startAt });

\t\t// 同片段可能因为 seek/改速被重新排程：旧 feeder 必须先摘掉，\n\t\t// 否则它在飞的渲染块会写进同一个播放器（PCM 乱序 = 破音/断音）\n\t\tthis.sonicFeeders.delete(key);\n\t\tthis.sonicFeeders.set(key, {
\t\t\tnode: player,
\t\t\tbuffer,
\t\t\trate,
\t\t\tstartAtCtx: startAt,
\t\t\tstartTimeline: timelineStart,
\t\t\tnextSourceTime: sourceStart + firstSeconds * rate,
\t\t\trenderedEndTimeline: timelineStart + firstSeconds,
\t\t\tremainingTimeline: remainingDuration - firstSeconds,\n\t\t\tbusy: false,
\t\t});
\t\tthis.ensureSonicFeedTimer();
\t\t// 立刻补一次，不等 250ms 定时器
\t\tvoid this.pumpSonicFeeders();

\t\tthis.queuedSources.add({
\t\t\tstop: () => {
\t\t\t\ttry {
\t\t\t\t\tplayer?.stop();
\t\t\t\t} catch {}
\t\t\t},
\t\t\tdisconnect: () => {
\t\t\t\ttry {
\t\t\t\t\tvolume.gain.cancelScheduledValues(audioContext.currentTime);
\t\t\t\t\tvolume.gain.value = 0;
\t\t\t\t} catch {}
\t\t\t},
\t\t});
\t\treturn true;
\t}

"""
        _am = _am[:_start] + _feeder + _am[_end:]
        # stopPlayback 里清空续渲染队列
        _am = _am.replace(
            "\t\tthis.queuedSources.clear();",
            "\t\tthis.queuedSources.clear();\n\t\tthis.sonicFeeders.clear();",
            1,
        )
        AUDIO_MANAGER.write_text(_am, encoding="utf-8")
        print("  patched audio-manager.ts (scheduleSonicClip -> 渲染 + 续渲染泵 + 纯播放器)")


# ------------------------------------- 多片段变速：解码缓存 + 播放器回收 + 不吞异常
#
# 之前改一个片段的速度就会走一遍「清解码缓存 → 全量重新解码 → 全量重排」，
# 几百秒的 mp4 解码要 1~3 秒，那段时间就是掉音；几个片段一个个改速度，听感
# 就是「先破音、然后彻底没声」。这里把解码缓存按文件对象判定新鲜度，
# 只要素材没换就不重解码，重排成本降到毫秒级。


def _edit_am(marker: str, old: str, new: str) -> None:
    text = AUDIO_MANAGER.read_text(encoding="utf-8")
    if marker in text:
        print("  keep    audio-manager.ts")
        return
    if old not in text:
        raise SystemExit(f"audio-manager anchor not found:\n{old[:200]}")
    AUDIO_MANAGER.write_text(text.replace(old, new, 1), encoding="utf-8")
    print("  patched audio-manager.ts")


_edit_am(
    "cached.file === clip.file",
    "\tprivate decodedBuffers = new Map<string, AudioBuffer>();",
    "\t// 按「素材文件对象」判定新鲜度：改速度 / 改裁剪都不会让缓存失效\n"
    "\tprivate decodedBuffers = new Map<\n"
    "\t\tstring,\n"
    "\t\t{ file: File; buffer: AudioBuffer }\n"
    "\t>();",
)

_edit_am(
    "if (cached && cached.file === clip.file) return cached.buffer;",
    "\t\tconst cached = this.decodedBuffers.get(clip.sourceKey);\n"
    "\t\tif (cached) return cached;",
    "\t\tconst cached = this.decodedBuffers.get(clip.sourceKey);\n"
    "\t\tif (cached && cached.file === clip.file) return cached.buffer;",
)

_edit_am(
    "this.decodedBuffers.set(clip.sourceKey, { file: clip.file, buffer });",
    "\t\t\tthis.decodedBuffers.set(clip.sourceKey, buffer);",
    "\t\t\tthis.decodedBuffers.set(clip.sourceKey, { file: clip.file, buffer });\n"
    "\t\t\t// 只留最近 24 个素材，长时间编辑也不会把内存吃满\n"
    "\t\t\twhile (this.decodedBuffers.size > 24) {\n"
    "\t\t\t\tconst oldest = this.decodedBuffers.keys().next().value;\n"
    "\t\t\t\tif (oldest === undefined) break;\n"
    "\t\t\t\tthis.decodedBuffers.delete(oldest);\n"
    "\t\t\t}",
)

_edit_am(
    "// 不再清解码缓存",
    "\t\tthis.timelineChangeTimer = window.setTimeout(() => {\n"
    "\t\t\tthis.timelineChangeTimer = null;\n"
    "\t\t\tthis.decodedBuffers.clear();\n",
    "\t\tthis.timelineChangeTimer = window.setTimeout(() => {\n"
    "\t\t\tthis.timelineChangeTimer = null;\n"
    "\t\t\t// 不再清解码缓存：改速度/改裁剪用的还是同一批素材，\n"
    "\t\t\t// 重复解码只会让每次重排都卡 1~3 秒（连续改速度时就是连续掉音）\n",
)

_edit_am(
    "private pruneSonicPlayers()",
    "\tprivate async scheduleSonicClip({",
    "\t/** 时间轴上已经没有的片段：把它的播放器丢掉，别一直攒着。 */\n"
    "\tprivate pruneSonicPlayers(): void {\n"
    "\t\tconst alive = new Set(this.clips.map((clip) => clip.id));\n"
    "\t\tfor (const key of [...this.sonicPlayers.keys()]) {\n"
    "\t\t\tif (alive.has(key)) continue;\n"
    "\t\t\tconst player = this.sonicPlayers.get(key);\n"
    "\t\t\tthis.sonicPlayers.delete(key);\n"
    "\t\t\tthis.sonicFeeders.delete(key);\n"
    "\t\t\ttry {\n"
    "\t\t\t\tplayer?.stop();\n"
    "\t\t\t\tplayer?.disconnect();\n"
    "\t\t\t} catch {}\n"
    "\t\t}\n"
    "\t}\n"
    "\n"
    "\tprivate async scheduleSonicClip({",
)

_edit_am(
    "this.pruneSonicPlayers();",
    "\t\t\t} catch (error) {\n"
    '\t\t\t\tconsole.warn("Failed to schedule audio clip:", clip.id, error);\n'
    "\t\t\t}\n"
    "\t\t}\n"
    "\t}",
    "\t\t\t} catch (error) {\n"
    '\t\t\t\tconsole.warn("Failed to schedule audio clip:", clip.id, error);\n'
    "\t\t\t}\n"
    "\t\t}\n"
    "\t\tthis.pruneSonicPlayers();\n"
    "\t}",
)

_edit_am(
    "await this.startPlaybackInner({ time });",
    "\tprivate async startPlayback({ time }: { time: number }): Promise<void> {\n"
    "\t\tconst audioContext = this.ensureAudioContext();\n"
    "\t\tif (!audioContext) return;",
    "\tprivate async startPlayback({ time }: { time: number }): Promise<void> {\n"
    "\t\ttry {\n"
    "\t\t\tawait this.startPlaybackInner({ time });\n"
    "\t\t} catch (error) {\n"
    "\t\t\t// 排程失败不能变成未捕获的 Promise：那样声音彻底没了还查不到原因\n"
    '\t\t\tconsole.warn("[audio] 播放排程失败", error);\n'
    "\t\t}\n"
    "\t}\n"
    "\n"
    "\tprivate async startPlaybackInner({ time }: { time: number }): Promise<void> {\n"
    "\t\tconst audioContext = this.ensureAudioContext();\n"
    "\t\tif (!audioContext) return;",
)

_edit_am(
    "feeder.remainingTimeline = 0;",
    "\t\t\t\t\tif (!chunk) break;",
    "\t\t\t\t\tif (!chunk) {\n"
    "\t\t\t\t\t\t// 源已经读到头（或渲染失败）：标记播完，别再每 250ms 空转\n"
    "\t\t\t\t\t\tfeeder.remainingTimeline = 0;\n"
    "\t\t\t\t\t\tbreak;\n"
    "\t\t\t\t\t}",
)

print("Done. sonic player patch applied.")
