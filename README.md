# ComfyUI-Moss-Diarize-Yuan

**自包含的 MOSS-Transcribe-Diarize ComfyUI 节点。模型权重就在本插件目录里，装进去就能用。**

不写 `ComfyUI/models`，不配 `extra_model_paths.yaml`，不需要另外跑下载脚本，也不额外安装 Python 依赖（除非你的 ComfyUI 环境里连 `transformers` / `torch` 都没有）。

- 模型：[OpenMOSS-Team/MOSS-Transcribe-Diarize](https://huggingface.co/OpenMOSS-Team/MOSS-Transcribe-Diarize)（0.9B，Apache-2.0）
- 一次前向同时给出**转写文本 + 句级时间戳 + 说话人编号**，50+ 语言
- 固定 revision `e8681d68e7042738ffca8ac8212bc8fcb1131ab8`

---

## 目录结构

```
ComfyUI-Moss-Diarize-Yuan/
├── __init__.py          节点注册 + 启动时检查插件内模型
├── moss_nodes.py        2 个节点（音频转文本 / 保存字幕）
├── moss_engine.py       加载 / 音频适配 / 生成 / 解析 / 字幕
├── moss_download.py     插件内模型目录的自愈下载
└── models/
    └── MOSS-Transcribe-Diarize/   ← 权重就在这里（约 1.71 GiB，16 个文件）
        ├── model-00000-of-00001.safetensors
        ├── config.json  modeling_*.py  processing_*.py  ...
        └── tokenizer.json  vocab.json  merges.txt  ...
```

模型路径是相对插件自身计算的（`Path(__file__).parent / "models" / "MOSS-Transcribe-Diarize"`），**整个插件目录可以整体搬走、改名字、放到任何 custom_nodes 路径下**，路径都不会错。

## 安装

```bash
cd ComfyUI/custom_nodes
git clone <本仓库> ComfyUI-Moss-Diarize-Yuan
# 重启 ComfyUI
```

就这样。首次启动时插件会检查 `models/MOSS-Transcribe-Diarize/`：

| 情况 | 行为 |
|---|---|
| 目录已完整（权重随插件一起带了） | 直接可用，不联网 |
| 目录缺失或不完整 | **ComfyUI 启动时后台自动下载到该目录**，不阻塞界面；节点执行时若还没下完会等它下完 |
| 想自己手动放 | 把 16 个文件塞进 `models/MOSS-Transcribe-Diarize/` 即可 |

### 开关

| 环境变量 | 默认 | 含义 |
|---|---|---|
| `MOSS_AUTODOWNLOAD` | `1` | 设 `0` 关闭启动时自动下载（纯离线部署） |
| `MOSS_DOWNLOAD_SOURCE` | 自动 | 强制下载源：`modelscope`（默认优先）／`huggingface` |
| `HF_ENDPOINT` | 自动探测 | 只在走 HuggingFace 时生效，强制指定 HF 镜像地址 |

### 下载源说明

优先级：**ModelScope 国内源 → huggingface.co → hf-mirror.com**。

- **默认 ModelScope**：`openmoss/MOSS-Transcribe-Diarize`，国内直连，实测 **25 MiB/s**，1.7 GiB 约 70 秒。
- **为什么不再默认 HF 镜像**：hf-mirror 会把下载重定向到 HuggingFace 的 **Xet CAS 服务器**（`cas-server.xethub.hf.co`），
  该端点需要鉴权，国内直连会报 `401 Unauthorized`。插件已设置 `HF_HUB_DISABLE_XET=1` 强制走经典 HTTP，
  但 ModelScope 仍然更快更稳，所以作为首选。
- **逐文件下载**：只补缺失/大小不符的文件，支持断点续传（HTTP Range）、`Content-Length` 校验、失败重试 3 次。
- **纯 HF 环境**：`MOSS_DOWNLOAD_SOURCE=huggingface` 可强制走 HF。

## 节点

### MOSS 音频转文本

界面全中文：节点标题、输入接口名、输出接口名、控件选项、提示说明、控制台日志都是中文。接口键名直接用中文，
所以在任何 UI 语言下显示的都是中文，**不依赖 locales 翻译是否命中**。

| 输入 | 说明 |
|---|---|
| `音频` | 标准 ComfyUI `AUDIO`（接「加载音频」） |
| `提示词预设` | 4 种：带时间戳与说话人（默认）／**带时间戳，不带说话人**／只要纯文本／英文转写（含说话人） |
| `最大生成长度` | 默认 4096。1 分钟语音约 300–600 token，长音频要调大，否则截断 |
| `跑完卸载模型` | **开关**：跑完就卸载／跑完留显存。默认「跑完留显存」，连续转写更快 |
| `自定义提示词` | 可选，填了就以它为准，覆盖预设 |
| `热词` | 可选，专有名词/人名/品牌名，逗号分隔，追加到提示词末尾 |
| `说话人改名` | 可选，`S01=张三,S02=李四`，留空保持原编号 |
| `精度` | 自动／bf16／fp16／fp32 |
| `注意力后端` | sdpa（推荐）／自动／flash_attention_2（需另装）／eager（慢，长音频爆显存） |
| `推理设备` | 自动／显卡 CUDA／处理器 CPU（很慢） |
| `最大序列长度` | 默认 131072，约可容纳 2.9 小时音频的音频 token |

输出：`文本`（模型原始输出）、`SRT 字幕`、`VTT 字幕`、`分段 JSON`。

### MOSS 保存字幕

| 输入 | 说明 |
|---|---|
| `字幕内容` | 接上一个节点的任一字符串输出 |
| `格式` | SRT 字幕／VTT 字幕／纯文本／分段 JSON |
| `文件名前缀` | 默认 `moss_diarize`，生成 `moss_diarize_20250101-120000.srt` |
| `加时间戳` | **开关**：带时间戳／只用前缀。关掉后重名会自动加 `_1`、`_2` |

输出：`文件路径`。文件写入 `ComfyUI/output/`。

> 整个插件只有这 **2 个节点**。释放显存不需要单独挂节点——转写节点上的 `跑完卸载模型` 开关就是干这个的。
> 模型路径与完整性在启动时打印到控制台（`[MOSS本地] 模型已就绪 ...`），也不需要单独的「模型状态」节点。

## 实现要点（也是踩过的坑）

1. **零额外依赖**。processor 的 `__call__(text=..., audio=[waveform])` 直接吃 16 kHz 单声道 `np.float32` 数组，内部自己切 30 秒窗口。所以**不需要** ffmpeg / soundfile / librosa / soxr，也不需要写临时 wav 文件。重采样用 `torchaudio.functional.resample`，没有 torchaudio 时退化为线性插值。
2. **音频适配**。ComfyUI 的 `AUDIO` 是 `[B, C, T]` 的 44.1 kHz float32 → 取 batch 0、声道求均值下混、重采样到 16 kHz。
3. **attention 必须显式管**。`eager` 在长音频下显存**平方级**增长会直接 OOM。默认 `sdpa`，加载失败会按 `flash_attention_2 → sdpa → eager` 依次回退并把每次失败原因打到控制台。
4. **输出解析**。模型输出是紧凑格式 `[0.48][S01]正文[1.66][12.26][S02]正文[13.81]`，时间戳单位是**秒**。其中 `[Sxx]` 是**可选**的：用「带时间戳，不带说话人」预设时模型只给 `[0.48]正文[1.66]`，解析器照样能切段，此时字幕里不会出现 `[Sxx]` 前缀（VTT 也不会有 `<v ...>`）。解析器对漏结尾时间戳、时间倒退做保守处理（丢弃/夹到上一段终点），不会生成非法 SRT。
5. **进度与中断**。用 `streamer` 统计生成 token 驱动 ComfyUI 进度条，并在每次回调检查中断标志，点「取消」能立刻停。

## 实测（本机 RTX 3090 24 GB，`sdpa`，bf16）

| 项 | 结果 |
|---|---|
| 中文语音 13.1 s（44.1 kHz 立体声→16 kHz 单声道） | 5 段、说话人正确、耗时 **3.9 s** |
| 峰值显存（仅权重） | **1.74 GiB** |
| 输出 | 合法 SRT（`00:00:00,410 --> 00:00:02,210`）与 JSON |

已在 ComfyUI 0.42.5 / Python 3.13.12 / torch 2.12.1+cu130 / transformers 5.20.2 上以
`LoadAudio → MOSS 转写 → 保存字幕` 工作流端到端跑通。

## 分发注意

权重有 1.7 GiB，**不适合直接 `git push`**（GitHub 单文件上限 100 MB）。三种做法：

1. 权重不进仓库，靠插件启动时自动下载 —— 开箱即用，推荐；
2. 用 Git LFS 管理 `models/`；
3. 打包成 zip 连同权重一起分发。

另外 `models/MOSS-Transcribe-Diarize/` 里有 Hugging Face 的 `modeling_*.py` 等远端代码，
加载时需要 `trust_remote_code=True`（插件已设好）。只放官方固定 revision 的文件。

## 限制

- **单次前向**，没有做 VAD 切片与分块拼接。24 GB 显存上 10 分钟音频约需 11–12 GB 显存（`sdpa`），可用；超过 30 分钟请先切片。
- 时间戳是**句/段级**，不是词级。词级需要另外接 Whisper 对齐。
- 静音、音乐、噪声上仍可能出现幻觉、提前结束或重复段落，重要结果需人工复核。
- CPU 可以跑（fp32），但很慢。

## 许可证

节点代码 Apache-2.0。模型权重版权归 OpenMOSS Team，Apache-2.0。本项目不是 OpenMOSS 官方发行物。
