"""ComfyUI-Moss-Diarize-Yuan — 自包含本地 ComfyUI 音频转文本节点.

模型权重就在本插件目录里:
    <插件目录>/models/MOSS-Transcribe-Diarize/

  * 开箱即用: 权重随插件一起放好, 不写 ComfyUI/models, 不需要 extra_model_paths.yaml,
    也不需要另外跑下载脚本。
  * 自愈: 若该目录缺文件, ComfyUI 启动时会在后台自动下载进去, 节点执行时等待下载完成。
    关闭自动下载: 设置环境变量 MOSS_AUTODOWNLOAD=0
  * 国内网络: 默认走 ModelScope 国内源(openmoss/MOSS-Transcribe-Diarize), 不用翻墙也不会有 Xet 鉴权问题;
    失败自动回退 HuggingFace(huggingface.co → hf-mirror.com)。强制指定: MOSS_DOWNLOAD_SOURCE=modelscope|huggingface
"""

from __future__ import annotations

from .moss_nodes import NODE_CLASS_MAPPINGS, NODE_DISPLAY_NAME_MAPPINGS
from . import moss_download

__version__ = "1.0.0"
__all__ = ["NODE_CLASS_MAPPINGS", "NODE_DISPLAY_NAME_MAPPINGS", "__version__"]

# ComfyUI 启动时立刻检查插件内模型目录, 缺文件就在后台线程里补全, 不阻塞启动
try:
    moss_download.start_background_provision()
except Exception as exc:  # 绝不让自动下载把 ComfyUI 的节点加载搞挂
    moss_download.log(f"自动下载初始化失败: {type(exc).__name__}: {exc}")
