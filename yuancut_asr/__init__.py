"""YuanCut 内置的 MOSS-Transcribe-Diarize 引擎与权重下载器。

原来这些代码放在独立的 ComfyUI 节点包（`custom_nodes/ComfyUI-Moss-Diarize-Yuan`）里，
YuanCut 只是借用它的模块。现在直接内置进本插件：

  * `moss_engine`   —— 推理（加载权重、生成、解析带说话人的句段）
  * `moss_download` —— 权重供给（ModelScope 优先，失败回退 HuggingFace；断点续传）

权重默认落在 `ComfyUI/models/Yuan_Cut/MOSS-Transcribe-Diarize/`，缺失时自动下载，
所以删掉外部节点包也不影响字幕转录。
"""

from . import moss_download

__all__ = ["moss_download"]
