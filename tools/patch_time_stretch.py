#!/usr/bin/env python3
"""变速不变调的补丁（把 Sonic 引擎接进编辑器）。

Sonic（Apache-2.0）编译成 wasm 后由 sonic-engine 在主线程分块渲染、
纯播放器 worklet 负责播出；导出仍由服务端 yuancut_render.py 用 rubberband 处理。

调用方式（build_ui.sh 已包含）：
  YUANCUT_WORK=/tmp/xxx python3 tools/patch_time_stretch.py
"""

from __future__ import annotations

import base64
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent

if len(sys.argv) > 1:
    ROOT = Path(sys.argv[1]).expanduser().resolve()
else:
    ROOT = Path(os.environ["YUANCUT_WORK"]).expanduser().resolve()

WEB = ROOT / "apps" / "web"
SRC = WEB / "src"
SONIC_WASM = HERE / "sonic" / "sonic.wasm"


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


def build_wasm_module() -> str | None:
    """把 tools/sonic/sonic.wasm 内联成 base64 模块。"""
    if not SONIC_WASM.is_file():
        print(f"  warn    {SONIC_WASM} 不存在，跳过预览变速（先跑 tools/sonic/build_sonic_wasm.sh）")
        return None
    data = base64.b64encode(SONIC_WASM.read_bytes()).decode("ascii")
    print(f"  embed   sonic.wasm {SONIC_WASM.stat().st_size} bytes -> base64 {len(data)}")
    return f'export const SONIC_WASM_B64 =\n\t"{data}";\n'


print(f"Patching time-stretch (Sonic) into {ROOT}")

wasm_module = build_wasm_module()
if wasm_module:
    write(SRC / "lib" / "audio" / "sonic-wasm.ts", wasm_module)

# ------------------------------------------------- 导出：请求里带上倍速
EXPORT_BUTTON = SRC / "components" / "editor" / "export-button.tsx"
patch(
    EXPORT_BUTTON,
    "\t\t\t\t\tconst visual = element as unknown as {\n"
    "\t\t\t\t\t\thidden?: boolean;\n"
    "\t\t\t\t\t\topacity?: number;\n"
    "\t\t\t\t\t\ttrimStart?: number;\n"
    "\t\t\t\t\t\ttransform?: { position?: { x: number; y: number }; scale?: number };\n"
    "\t\t\t\t\t};",
    "\t\t\t\t\tconst visual = element as unknown as {\n"
    "\t\t\t\t\t\thidden?: boolean;\n"
    "\t\t\t\t\t\topacity?: number;\n"
    "\t\t\t\t\t\ttrimStart?: number;\n"
    "\t\t\t\t\t\tplaybackRate?: number;\n"
    "\t\t\t\t\t\ttransform?: { position?: { x: number; y: number }; scale?: number };\n"
    "\t\t\t\t\t};",
)
patch(
    EXPORT_BUTTON,
    "\t\t\t\t\t\topacity: visual.opacity ?? 1,\n\t\t\t\t\t});",
    "\t\t\t\t\t\topacity: visual.opacity ?? 1,\n"
    "\t\t\t\t\t\tplaybackRate: visual.playbackRate ?? 1,\n"
    "\t\t\t\t\t});",
)
patch(
    EXPORT_BUTTON,
    "\t\t\t\t\tconst audio = element as unknown as {\n"
    "\t\t\t\t\t\thidden?: boolean;\n"
    "\t\t\t\t\t\tmuted?: boolean;\n"
    "\t\t\t\t\t\tvolume?: number;\n"
    "\t\t\t\t\t\ttrimStart?: number;\n"
    "\t\t\t\t\t};",
    "\t\t\t\t\tconst audio = element as unknown as {\n"
    "\t\t\t\t\t\thidden?: boolean;\n"
    "\t\t\t\t\t\tmuted?: boolean;\n"
    "\t\t\t\t\t\tvolume?: number;\n"
    "\t\t\t\t\t\ttrimStart?: number;\n"
    "\t\t\t\t\t\tplaybackRate?: number;\n"
    "\t\t\t\t\t};",
)
patch(
    EXPORT_BUTTON,
    "\t\t\t\t\t\ttrimStart: audio.trimStart ?? 0,\n"
    "\t\t\t\t\t\tvolume: audio.volume ?? 1,\n"
    "\t\t\t\t\t});",
    "\t\t\t\t\t\ttrimStart: audio.trimStart ?? 0,\n"
    "\t\t\t\t\t\tvolume: audio.volume ?? 1,\n"
    "\t\t\t\t\t\tplaybackRate: audio.playbackRate ?? 1,\n"
    "\t\t\t\t\t});",
)

if not wasm_module:
    print("Done. time-stretch skipped (no wasm).")
    raise SystemExit(0)

# ------------------------------------------------- 预览：Sonic 流式变速
AUDIO_MANAGER = SRC / "core" / "managers" / "audio-manager.ts"

patch(
    AUDIO_MANAGER,
    '''import { createAudioContext, collectAudioClips } from "@/lib/media/audio";''',
    '''import { createAudioContext, collectAudioClips } from "@/lib/media/audio";
import {
	createSonicNode,
	getClipChannels,
	loadClip,
	setSpeed,
	shouldPreservePitch,
	startClip,
	type StretchSource,
} from "@/lib/audio/sonic-stretch";''',
)
patch(
    AUDIO_MANAGER,
    "\tprivate queuedSources = new Set<AudioBufferSourceNode>();",
    "\tprivate queuedSources = new Set<StretchSource>();",
)
patch(
    AUDIO_MANAGER,
    "\t\t\t\tthis.scheduleClipNode({ clip, buffer, time });",
    "\t\t\t\tawait this.scheduleClipNode({ clip, buffer, time, sessionId });",
)

# 调度器：rate != 1 时走 Sonic 流式变速，其余保持上游行为
patch(
    AUDIO_MANAGER,
    '''	private scheduleClipNode({
		clip,
		buffer,
		time,
	}: {
		clip: AudioClipSource;
		buffer: AudioBuffer;
		time: number;
	}): void {''',
    '''	/**
	 * 变速不变调：rate != 1 时用 Sonic 流式时间伸缩（改速度只是改参数，
	 * 不重建、不预渲染、不丢开头）。失败才退回上游的 playbackRate 方式。
	 */
	private async scheduleClipNode({
		clip,
		buffer,
		time,
		sessionId,
	}: {
		clip: AudioClipSource;
		buffer: AudioBuffer;
		time: number;
		sessionId: number;
	}): Promise<void> {
		const targetRate = clip.playbackRate;
		const isStopped = () =>
			sessionId !== this.playbackSessionId ||
			!this.editor.playback.getIsPlaying();

		if (shouldPreservePitch({ rate: targetRate })) {
			const scheduled = await this.scheduleSonicClip({
				clip,
				buffer,
				time,
				sessionId,
			});
			if (scheduled) return;
			// 已经暂停/换会话：直接放弃，不能回退播放
			if (isStopped()) return;
		}

		this.scheduleBufferNode({ clip, buffer, time });
	}

	/** Sonic 路径：把片段 PCM 交给 worklet，按目标速度流式伸缩。 */
	/** 伸缩节点 -> 它的 gain，用于停止时做淡出。 */
	private readonly fadeOutGains = new Map<StretchSource, GainNode>();

	private async scheduleSonicClip({
		clip,
		buffer,
		time,
		sessionId,
	}: {
		clip: AudioClipSource;
		buffer: AudioBuffer;
		time: number;
		sessionId: number;
	}): Promise<boolean> {
		const audioContext = this.audioContext;
		const masterGain = this.masterGain;
		if (!audioContext || !masterGain) return false;

		const rate = clip.playbackRate;
		const elapsed = Math.max(0, time - clip.startTime);
		const remainingDuration = clip.duration - elapsed;
		if (remainingDuration <= 0) return true;

		const node = await createSonicNode({ audioContext });
		if (!node) return false;

		if (
			sessionId !== this.playbackSessionId ||
			!this.editor.playback.getIsPlaying()
		) {
			try {
				node.disconnect();
			} catch {}
			return false;
		}

		const [left, right] = getClipChannels({
			buffer,
			startTime: clip.trimStart,
			duration: clip.duration * rate,
		});
		loadClip({ node, left, right });

		const timelineStart = Math.max(clip.startTime, time);
		const scheduleTime =
			this.playbackStartContextTime +
			(timelineStart - this.playbackStartTime);
		const startAt = Math.max(scheduleTime, audioContext.currentTime + 0.01);
		const late = Math.max(0, startAt - scheduleTime);
		const playDuration = remainingDuration - late;
		if (playDuration <= 0) {
			try {
				node.disconnect();
			} catch {}
			return true;
		}

		const clipGain = audioContext.createGain();
		// 起播淡入，避免从波形中间硬切造成的爆点（听起来就是"破"）
		clipGain.gain.setValueAtTime(0, startAt);
		clipGain.gain.linearRampToValueAtTime(clip.volume, startAt + 0.012);
		node.connect(clipGain);
		clipGain.connect(masterGain);
		this.fadeOutGains.set(node, clipGain);

		setSpeed({ node, speed: rate });
		startClip({ node, at: startAt, offset: elapsed + late });

		this.queuedSources.add(node);
		const cleanupMs =
			Math.max(0, startAt - audioContext.currentTime + playDuration) * 1000 + 300;
		window.setTimeout(() => {
			try {
				node.stop();
				node.disconnect();
			} catch {}
			this.queuedSources.delete(node);
		}, cleanupMs);
		return true;
	}

	/** 上游路径：直接改 playbackRate（音高会跟着速度变）。 */
	private scheduleBufferNode({
		clip,
		buffer,
		time,
	}: {
		clip: AudioClipSource;
		buffer: AudioBuffer;
		time: number;
	}): void {''',
)

patch(
    AUDIO_MANAGER,
    '''	private stopPlayback(): void {
		for (const source of this.queuedSources) {
			try {
				source.stop();
			} catch {}
			source.disconnect();
		}
		this.queuedSources.clear();
	}''',
    '''	private stopPlayback(): void {
		// 让在途的异步排程作废，避免暂停后被迟到地播出来
		this.playbackSessionId++;
		const audioContext = this.audioContext;
		const now = audioContext ? audioContext.currentTime : 0;
		const pending: StretchSource[] = [];
		for (const source of this.queuedSources) {
			const gain = this.fadeOutGains.get(source);
			if (gain && audioContext) {
				// 8ms 淡出，避免硬切爆点；淡完再断开
				try {
					gain.gain.cancelScheduledValues(now);
					gain.gain.setValueAtTime(gain.gain.value, now);
					gain.gain.linearRampToValueAtTime(0, now + 0.008);
				} catch {}
				pending.push(source);
				this.fadeOutGains.delete(source);
			} else {
				try {
					source.stop(0);
				} catch {}
				source.disconnect();
			}
		}
		if (pending.length > 0) {
			window.setTimeout(() => {
				for (const source of pending) {
					try {
						source.stop(0);
					} catch {}
					try {
						source.disconnect();
					} catch {}
					const gain = this.fadeOutGains.get(source);
					if (gain) {
						try {
							gain.disconnect();
						} catch {}
					}
				}
			}, 30);
		}
		this.queuedSources.clear();
	}''',
)

# --- 拖动进度条：把密集 seek 合并成一次重排，避免每个 seek 都重建音频节点 ---
patch(
    AUDIO_MANAGER,
    "\tprivate timelineChangeTimer: number | null = null;",
    "\tprivate timelineChangeTimer: number | null = null;\n"
    "\tprivate seekRestartTimer: number | null = null;",
)
patch(
    AUDIO_MANAGER,
    '''		if (this.editor.playback.getIsPlaying()) {
			void this.startPlayback({ time: detail.time });
			return;
		}''',
    '''		if (this.editor.playback.getIsPlaying()) {
			// 拖动进度条会连发很多 seek：合并成一次重排，
			// 否则每个 seek 都重建音频节点会把音频线程压垮（听感就是爆音）
			if (this.seekRestartTimer !== null) {
				window.clearTimeout(this.seekRestartTimer);
			}
			this.seekRestartTimer = window.setTimeout(() => {
				this.seekRestartTimer = null;
				if (this.editor.playback.getIsPlaying()) {
					void this.startPlayback({ time: detail.time });
				}
			}, 140);
			return;
		}''',
)
patch(
    AUDIO_MANAGER,
    '''	dispose(): void {
		this.stopPlayback();''',
    '''	dispose(): void {
		this.stopPlayback();
		if (this.seekRestartTimer !== null) {
			window.clearTimeout(this.seekRestartTimer);
			this.seekRestartTimer = null;
		}''',
)

# --- 后处理：把 stopPlayback 换成"只停流"版本（老的淡出版本引用了已删除的 fadeOutGains） ---
_am_text = AUDIO_MANAGER.read_text(encoding="utf-8") if AUDIO_MANAGER.exists() else ""
_start = _am_text.find("\tprivate stopPlayback(): void {")
_end = _am_text.rfind("\n}\n")
if _start != -1 and _end > _start:
    _new_stop = (
        "\tprivate stopPlayback(): void {\n"
        "\t\t// 让在途的异步排程作废\n"
        "\t\tthis.playbackSessionId++;\n"
        "\t\tfor (const source of this.queuedSources) {\n"
        "\t\t\ttry {\n"
        "\t\t\t\tsource.stop(0);\n"
        "\t\t\t} catch {}\n"
        "\t\t\ttry {\n"
        "\t\t\t\tsource.disconnect();\n"
        "\t\t\t} catch {}\n"
        "\t\t}\n"
        "\t\tthis.queuedSources.clear();\n"
        "\t}\n\n"
    )
    AUDIO_MANAGER.write_text(_am_text[:_start] + _new_stop + _am_text[_end:], encoding="utf-8")
    print("  patched audio-manager.ts (stopPlayback -> 只停流)")

print("Done. time-stretch (Sonic) patch applied.")
