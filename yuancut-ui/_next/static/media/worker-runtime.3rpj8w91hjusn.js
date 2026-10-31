// 字幕转录统一走 ComfyUI 本机的 MOSS-Transcribe-Diarize（见 yuancut_transcribe.py）。
const TRANSCRIBE_ENDPOINT = "/yuancut/transcribe";
const SAMPLE_RATE = 16000;
// 单次请求最多送 5 分钟音频，更长的音频分块发送后按时间偏移拼接
const MAX_WINDOW_SECONDS = 300;
// 同一个路由的 GET 返回引擎 / 权重状态（是否就绪、下载进度）
const STATUS_ENDPOINT = TRANSCRIBE_ENDPOINT;

let cancelled = false;
let activeController = null;

self.onmessage = async (event) => {
	const message = event.data;

	switch (message.type) {
		case "init":
			// 模型常驻 ComfyUI 服务端，前端不需要下载权重
			self.postMessage({ type: "init-progress", progress: 100 });
			self.postMessage({ type: "init-complete" });
			break;
		case "transcribe":
			await handleTranscribe({
				audio: message.audio,
				language: message.language,
			});
			break;
		case "cancel":
			cancelled = true;
			if (activeController) activeController.abort();
			self.postMessage({ type: "cancelled" });
			break;
	}
};

function toBase64(samples) {
	const bytes = new Uint8Array(
		samples.buffer,
		samples.byteOffset,
		samples.byteLength,
	);
	const BLOCK = 0x8000;
	let binary = "";
	for (let index = 0; index < bytes.length; index += BLOCK) {
		const slice = bytes.subarray(index, index + BLOCK);
		binary += String.fromCharCode(...slice);
	}
	return btoa(binary);
}

async function handleTranscribe({ audio, language }) {
	cancelled = false;
	const startedAt = performance.now();
	const windowSamples = Math.floor(MAX_WINDOW_SECONDS * SAMPLE_RATE);
	const windowCount = Math.max(1, Math.ceil(audio.length / windowSamples));

	const segments = [];
	const chunks = [];
	let collectedText = "";
	let tps = 0;

	try {
		// 首次使用时要先等权重就绪（1.8 GB，下载进度会回显到界面）
		await waitForModel();

		for (let windowIndex = 0; windowIndex < windowCount; windowIndex++) {
			if (cancelled) return;

			const startSample = windowIndex * windowSamples;
			const endSample = Math.min(audio.length, startSample + windowSamples);
			if (endSample - startSample < SAMPLE_RATE * 0.2) continue;

			const offsetS = startSample / SAMPLE_RATE;
			const windowAudio = audio.slice(startSample, endSample);

			activeController = new AbortController();
			const response = await fetch(TRANSCRIBE_ENDPOINT, {
				method: "POST",
				headers: { "Content-Type": "application/json" },
				body: JSON.stringify({
					audio: toBase64(windowAudio),
					sample_rate: SAMPLE_RATE,
					language,
				}),
				signal: activeController.signal,
			});
			activeController = null;

			if (!response.ok) {
				let detail = "MOSS 转写失败：HTTP " + response.status;
				try {
					const payload = await response.json();
					if (payload && payload.error) detail = payload.error;
				} catch {
					// 保留 HTTP 状态码
				}
				throw new Error(detail);
			}

			const payload = await response.json();
			for (const item of payload.segments || []) {
				const text = (item.text || "").trim();
				if (!text) continue;
				const start = offsetS + Number(item.start || 0);
				const end = offsetS + Number(item.end || item.start || 0);
				segments.push({ text, start, end });
				chunks.push({
					text,
					timestamp: [start, end],
					finalised: true,
					offset: offsetS,
				});
			}
			if (payload.text) {
				collectedText = [collectedText, payload.text.trim()]
					.filter((part) => part.length > 0)
					.join(" ");
			}

			const elapsedSeconds = (performance.now() - startedAt) / 1000;
			tps = elapsedSeconds > 0 ? collectedText.length / elapsedSeconds : 0;

			self.postMessage({ type: "transcribe-update", chunks, tps });
			self.postMessage({
				type: "transcribe-progress",
				progress: Math.min(
					99,
					Math.round(((windowIndex + 1) / windowCount) * 100),
				),
			});
		}

		self.postMessage({
			type: "transcribe-complete",
			text: collectedText,
			segments,
			tps,
		});
	} catch (error) {
		if (cancelled) return;
		self.postMessage({
			type: "transcribe-error",
			error: error instanceof Error ? error.message : "MOSS 转写失败",
		});
	} finally {
		activeController = null;
	}
}

/**
 * 等服务端权重就绪。
 * 没说就绪就每 2 秒查一次状态，把下载进度当成「转写进度」发回去（带 label），
 * 界面会显示「模型下载中 x%（a/b GiB）」。下完直接返回，继续正常转写。
 */
async function waitForModel() {
	for (let attempt = 0; attempt < 900; attempt++) {
		if (cancelled) return;
		let status = null;
		try {
			const response = await fetch(STATUS_ENDPOINT, { cache: "no-store" });
			if (response.ok) status = await response.json();
		} catch {
			// 状态接口问不到就让 POST 自己去报错，别卡住转写
			return;
		}
		if (!status || status.model_ready) return;
		if (status.download_error) {
			throw new Error("模型下载失败：" + status.download_error);
		}
		const done = Math.max(0, Math.min(1, Number(status.download_progress) || 0));
		const gib = (Number(status.downloaded_bytes) || 0) / 1024 ** 3;
		const total = (Number(status.total_bytes) || 0) / 1024 ** 3;
		self.postMessage({
			type: "transcribe-progress",
			progress: Math.round(done * 100),
			label:
				total > 0
					? "模型下载中 " +
					  Math.round(done * 100) +
					  "%（" +
					  gib.toFixed(2) +
					  "/" +
					  total.toFixed(2) +
					  " GiB）"
					: "模型下载中 " + Math.round(done * 100) + "%",
		});
		await new Promise((resolve) => setTimeout(resolve, 2000));
	}
}
