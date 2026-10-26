"""ComfyUI 节点定义：音频转文本 / 保存字幕。

界面全中文：节点标题、输入接口名、输出接口名、控件选项、提示说明、控制台日志。
接口键名直接用中文，因此在「中文/英文」任意 UI 语言下都显示中文，不依赖 locales 翻译是否命中。
"""

from __future__ import annotations

import os
import time

import folder_paths

from . import moss_engine
from .moss_engine import DEFAULT_PROMPT

CATEGORY = "Yuan Tool/Moss 音频"

PROMPT_PRESETS = {
    "带时间戳与说话人（默认）": DEFAULT_PROMPT,
    "带时间戳，不带说话人": (
        "请将音频转写为文本，每一段需以起始时间戳开头，正文为对应的语音内容，"
        "并在段末标注结束时间戳，以清晰标明该段语音范围。"
        "不要输出任何说话人编号或说话人标记。"
    ),
    "只要纯文本": "请将音频转写为纯文本，不要输出时间戳和说话人标记。",
    "英文转写（含说话人）": (
        "Transcribe the audio into text. Start every segment with its start timestamp "
        "and a speaker label ([S01], [S02], ...), then the spoken content, and end the "
        "segment with its end timestamp."
    ),
}

# 界面上显示中文，引擎内部仍用英文键，避免中文值渗透到推理代码里
DTYPE_OPTIONS = {
    "自动": "auto",
    "bf16": "bf16",
    "fp16": "fp16",
    "fp32": "fp32",
}

ATTENTION_OPTIONS = {
    "sdpa（推荐）": "sdpa",
    "自动": "auto",
    "flash_attention_2（需另装）": "flash_attention_2",
    "eager（慢，长音频爆显存）": "eager",
}

DEVICE_OPTIONS = {
    "自动": "auto",
    "显卡 CUDA": "cuda",
    "处理器 CPU（很慢）": "cpu",
}

FORMAT_OPTIONS = {
    "SRT 字幕": "srt",
    "VTT 字幕": "vtt",
    "纯文本": "txt",
    "分段 JSON": "json",
}


def _log(message: str) -> None:
    print(f"[MOSS本地] {message}", flush=True)


class MOSS_TranscribeDiarize:
    """把 ComfyUI 的音频转写成带时间戳与说话人编号的文本和字幕。"""

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "音频": ("AUDIO",),
                "提示词预设": (
                    list(PROMPT_PRESETS.keys()),
                    {
                        "default": "带时间戳与说话人（默认）",
                        "tooltip": "决定输出里是否带时间戳、是否带 [S01] 说话人标记。「只要纯文本」不生成字幕分段；「带时间戳，不带说话人」生成的字幕不含 [S01] 前缀。",
                    },
                ),
                "最大生成长度": (
                    "INT",
                    {
                        "default": 4096,
                        "min": 64,
                        "max": 16384,
                        "step": 64,
                        "tooltip": "最多生成多少 token。1 分钟语音约 300~600，长音频要调大，否则会被截断。",
                    },
                ),
            },
            "optional": {
                "跑完卸载模型": (
                    "BOOLEAN",
                    {
                        "default": False,
                        "label_on": "跑完就卸载",
                        "label_off": "跑完留显存",
                        "tooltip": "开：转写结束立刻释放模型显存，下次运行重新加载(约2秒)。关：模型常驻，连续转写更快。",
                    },
                ),
                "自定义提示词": (
                    "STRING",
                    {
                        "default": "",
                        "multiline": True,
                        "tooltip": "留空则使用上面的「提示词预设」。填了就以这里为准，会覆盖预设。",
                    },
                ),
                "热词": (
                    "STRING",
                    {
                        "default": "",
                        "multiline": False,
                        "tooltip": "专有名词、人名、品牌名，用逗号分隔。会追加到提示词末尾，可提升命中率。",
                    },
                ),
                "说话人改名": (
                    "STRING",
                    {
                        "default": "",
                        "tooltip": "把 S01/S02 换成人名，格式如：S01=张三,S02=李四。留空保持原编号。",
                    },
                ),
                "精度": (
                    list(DTYPE_OPTIONS.keys()),
                    {
                        "default": "自动",
                        "tooltip": "自动＝显卡上优先 bf16(30/40系支持)，否则 fp16；CPU 强制 fp32。显存不够可试 fp16。",
                    },
                ),
                "注意力后端": (
                    list(ATTENTION_OPTIONS.keys()),
                    {
                        "default": "sdpa（推荐）",
                        "tooltip": "sdpa 是 PyTorch 内置，速度快、显存随长度线性增长。eager 在长音频上显存平方增长，会爆。",
                    },
                ),
                "推理设备": (
                    list(DEVICE_OPTIONS.keys()),
                    {
                        "default": "自动",
                        "tooltip": "自动＝有显卡用显卡，否则用处理器。CPU 能跑但非常慢。",
                    },
                ),
                "最大序列长度": (
                    "INT",
                    {
                        "default": 131072,
                        "min": 2048,
                        "max": 131072,
                        "step": 2048,
                        "tooltip": "提示词+音频 token 的总上限。131072 约可容纳 2.9 小时音频；音频更长会报错，请先切段。",
                    },
                ),
            },
        }

    RETURN_TYPES = ("STRING", "STRING", "STRING", "STRING")
    RETURN_NAMES = ("文本", "SRT 字幕", "VTT 字幕", "分段 JSON")
    FUNCTION = "run"
    CATEGORY = CATEGORY
    DESCRIPTION = (
        "MOSS-Transcribe-Diarize 本地转写：一次前向同时给出文本、句级时间戳和说话人编号。"
        "模型就在本插件目录内，无需额外下载。"
    )

    def run(
        self,
        音频,
        提示词预设: str,
        最大生成长度: int,
        跑完卸载模型: bool = False,
        自定义提示词: str = "",
        热词: str = "",
        说话人改名: str = "",
        精度: str = "自动",
        注意力后端: str = "sdpa（推荐）",
        推理设备: str = "自动",
        最大序列长度: int = 131072,
    ):
        import comfy.model_management as 显存管理
        import comfy.utils

        实际提示词 = (自定义提示词 or "").strip() or PROMPT_PRESETS.get(提示词预设, DEFAULT_PROMPT)

        seconds = moss_engine.duration_of(音频)
        if seconds > 1800:
            _log(
                f"警告：音频 {seconds / 60:.1f} 分钟，单次前向可能超出显存。"
                "建议先切成 10 分钟以内的片段再转写。"
            )

        进度条 = comfy.utils.ProgressBar(int(最大生成长度))
        已报数 = 0

        def 收到token(count: int) -> None:
            nonlocal 已报数
            if count > 已报数:
                进度条.update(count - 已报数)
                已报数 = count

        def 检查中断() -> None:
            显存管理.throw_exception_if_processing_interrupted()

        started = time.time()
        try:
            result = moss_engine.transcribe(
                音频,
                prompt=实际提示词,
                hotwords=热词,
                max_new_tokens=int(最大生成长度),
                max_length=int(最大序列长度),
                device_name=DEVICE_OPTIONS.get(推理设备, "auto"),
                dtype_name=DTYPE_OPTIONS.get(精度, "auto"),
                attention=ATTENTION_OPTIONS.get(注意力后端, "sdpa"),
                on_token=收到token,
                on_check=检查中断,
            )
        finally:
            if 跑完卸载模型:
                freed = moss_engine.unload_models()
                _log(f"已释放 {freed} 份模型缓存")

        elapsed = time.time() - started
        segments = result["segments"]
        mapping = moss_engine.parse_speaker_map(说话人改名)

        _log(
            f"完成：{result['duration']:.1f} 秒音频 / 生成 {result['generated_tokens']} token / "
            f"{len(segments)} 段 / 总耗时 {elapsed:.1f}s / 注意力后端={result['attention']}"
        )
        if not segments and result["text"]:
            _log("提示：输出里没有解析到 [起始][Sxx]…[结束] 结构，只有「文本」可用。可换预设或提示词重试。")

        return (
            result["text"],
            moss_engine.to_srt(segments, mapping),
            moss_engine.to_vtt(segments, mapping),
            moss_engine.to_segments_json(segments, mapping),
        )


class MOSS_SaveSubtitles:
    """把字幕字符串写成文件，存到 ComfyUI 的 output 目录。"""

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "字幕内容": ("STRING", {"forceInput": True}),
                "格式": (
                    list(FORMAT_OPTIONS.keys()),
                    {
                        "default": "SRT 字幕",
                        "tooltip": "接哪个输出就选对应格式：SRT 字幕→SRT，VTT 字幕→VTT，分段 JSON→JSON。",
                    },
                ),
                "文件名前缀": (
                    "STRING",
                    {
                        "default": "moss_diarize",
                        "tooltip": "文件名前缀，例如 moss_diarize 会生成 moss_diarize_20250101-120000.srt。",
                    },
                ),
            },
            "optional": {
                "加时间戳": (
                    "BOOLEAN",
                    {
                        "default": True,
                        "label_on": "带时间戳",
                        "label_off": "只用前缀",
                        "tooltip": "开：文件名追加年月日-时分秒，避免覆盖。关：固定文件名，重名时自动加 _1、_2。",
                    },
                ),
            },
        }

    RETURN_TYPES = ("STRING",)
    RETURN_NAMES = ("文件路径",)
    FUNCTION = "run"
    CATEGORY = CATEGORY
    OUTPUT_NODE = True
    DESCRIPTION = "把「SRT 字幕 / VTT 字幕 / 纯文本 / 分段 JSON」写成文件，保存到 ComfyUI 的 output 目录。"

    def run(self, 字幕内容: str, 格式: str, 文件名前缀: str, 加时间戳: bool = True):
        输出目录 = folder_paths.get_output_directory()
        os.makedirs(输出目录, exist_ok=True)

        后缀 = FORMAT_OPTIONS.get(格式, "srt")
        前缀 = (文件名前缀 or "moss_diarize").strip() or "moss_diarize"
        主名 = time.strftime(f"{前缀}_%Y%m%d-%H%M%S") if 加时间戳 else 前缀
        路径 = os.path.join(输出目录, f"{主名}.{后缀}")

        序号 = 1
        while os.path.exists(路径):
            路径 = os.path.join(输出目录, f"{主名}_{序号}.{后缀}")
            序号 += 1

        with open(路径, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(字幕内容 or "")

        _log(f"字幕已保存：{路径}")
        return (路径,)


NODE_CLASS_MAPPINGS = {
    "MOSS_TranscribeDiarize": MOSS_TranscribeDiarize,
    "MOSS_SaveSubtitles": MOSS_SaveSubtitles,
}

NODE_DISPLAY_NAME_MAPPINGS = {
    "MOSS_TranscribeDiarize": "MOSS 音频转文本",
    "MOSS_SaveSubtitles": "MOSS 保存字幕",
}
