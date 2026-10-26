"""自包含模型供给: 权重只放在本插件目录内, 不碰 ComfyUI/models, 也不用 extra_model_paths.yaml.

下载源:
  * 默认 ModelScope 国内源(openmoss/MOSS-Transcribe-Diarize), 没有 Xet 鉴权跳转
  * 失败自动回退 HuggingFace: huggingface.co → hf-mirror.com
  * 强制指定: MOSS_DOWNLOAD_SOURCE=modelscope|huggingface
  * 关闭启动时自动下载: MOSS_AUTODOWNLOAD=0
"""

from __future__ import annotations

import os
import shutil
import threading
import time
import urllib.request
from pathlib import Path

# hf-mirror 会把下载重定向到 HuggingFace 的 Xet CAS 服务器 (cas-server.xethub.hf.co),
# 该端点需要鉴权, 国内直连会返回 401 Unauthorized。关掉 Xet 强制走经典 HTTP 下载。
# 必须在 huggingface_hub 被导入之前设置, 因此放在模块顶层。
os.environ.setdefault("HF_HUB_DISABLE_XET", "1")

PLUGIN_DIR = Path(__file__).resolve().parent
MODEL_ROOT = PLUGIN_DIR / "models"
MODEL_DIR = MODEL_ROOT / "MOSS-Transcribe-Diarize"

REPO_ID = "OpenMOSS-Team/MOSS-Transcribe-Diarize"
REVISION = "e8681d68e7042738ffca8ac8212bc8fcb1131ab8"

WEIGHT_FILE = "model-00000-of-00001.safetensors"
WEIGHT_SIZE = 1_817_113_576  # 精确字节数, 用来识别被截断的权重

# 模型只需要这些文件; README/.gitattributes/图片一律不下载
REQUIRED_FILES = (
    "added_tokens.json",
    "chat_template.jinja",
    "config.json",
    "configuration_moss_transcribe_diarize.py",
    "generation_config.json",
    "merges.txt",
    WEIGHT_FILE,
    "model.safetensors.index.json",
    "modeling_moss_transcribe_diarize.py",
    "preprocessor_config.json",
    "processing_moss_transcribe_diarize.py",
    "processor_config.json",
    "special_tokens_map.json",
    "tokenizer.json",
    "tokenizer_config.json",
    "vocab.json",
)

DEFAULT_ENDPOINTS = ("https://huggingface.co", "https://hf-mirror.com")
TOTAL_BYTES = 1_833_163_329  # 约 1.71 GiB, 仅用于显示

_lock = threading.Lock()
_thread: threading.Thread | None = None
_finished = threading.Event()
_error: BaseException | None = None
_endpoint_used: str | None = None
_source_used: str | None = None
_downloader_ident: int | None = None  # 正在下载的线程 ident, None 表示无人下载


def log(message: str) -> None:
    print(f"[MOSS本地] {message}", flush=True)


# --------------------------------------------------------------------------- 状态检查


def _file_ok(name: str) -> bool:
    """单个文件是否已就绪(大权重额外做精确字节数校验)."""
    path = MODEL_DIR / name
    if not path.is_file():
        return False
    # 只对大权重做精确大小校验, 其余文件存在即认为完整
    if name == WEIGHT_FILE and path.stat().st_size != WEIGHT_SIZE:
        return False
    return True


def missing_files() -> list[str]:
    """返回缺失或大小不对的文件名列表."""
    return [name for name in REQUIRED_FILES if not _file_ok(name)]


def is_ready() -> bool:
    return not missing_files()


def describe() -> str:
    if is_ready():
        size = sum((MODEL_DIR / n).stat().st_size for n in REQUIRED_FILES)
        return f"模型已就绪 ({size / 1024 ** 3:.2f} GiB): {MODEL_DIR}"
    missing = missing_files()
    return f"模型不完整 ({len(missing)}/{len(REQUIRED_FILES)} 个文件缺失): {MODEL_DIR}"


# --------------------------------------------------------------------------- 下载源选择

# ModelScope 国内源: 没有 Xet/CAS 鉴权跳转, 国内直连速度快, 作为默认下载源
MODELSCOPE_REPO = "openmoss/MOSS-Transcribe-Diarize"
MODELSCOPE_REVISION = "master"
MODELSCOPE_ENDPOINT = "https://www.modelscope.cn"
USER_AGENT = "comfyui-moss-diarize-yuan"


def _probe_modelscope(timeout: float = 8.0) -> bool:
    # 注意: ModelScope 的 API 对 HEAD 请求不响应(会挂到超时), 必须用 GET
    url = f"{MODELSCOPE_ENDPOINT}/api/v1/models/{MODELSCOPE_REPO}"
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return 200 <= response.status < 400
    except Exception:
        return False


def _probe(endpoint: str, timeout: float = 6.0) -> bool:
    url = f"{endpoint.rstrip('/')}/api/models/{REPO_ID}"
    request = urllib.request.Request(url, method="HEAD", headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return 200 <= response.status < 400
    except Exception:
        return False


def pick_endpoint() -> str:
    """选出第一个可达的 HuggingFace 端点."""
    forced = os.environ.get("HF_ENDPOINT", "").strip()
    if forced:
        return forced.rstrip("/")
    for endpoint in DEFAULT_ENDPOINTS:
        if _probe(endpoint):
            return endpoint
    # 都探不通也返回默认值, 让 huggingface_hub 自己报出真实错误
    return DEFAULT_ENDPOINTS[0]


def pick_source() -> str:
    """选下载源: 默认 ModelScope(国内直连), 探不通再走 HuggingFace."""
    forced = os.environ.get("MOSS_DOWNLOAD_SOURCE", "").strip().lower()
    if forced in ("modelscope", "ms"):
        return "modelscope"
    if forced in ("huggingface", "hf"):
        return "huggingface"
    if _probe_modelscope():
        return "modelscope"
    return "huggingface"


# --------------------------------------------------------------------------- 下载


def _human(number: float) -> str:
    for unit in ("B", "KiB", "MiB", "GiB"):
        if number < 1024 or unit == "GiB":
            return f"{number:.1f} {unit}"
        number /= 1024
    return f"{number:.1f} GiB"


def _dir_size() -> int:
    total = 0
    for path in MODEL_ROOT.rglob("*"):
        try:
            if path.is_file():
                total += path.stat().st_size
        except OSError:
            pass
    return total


def _progress_watcher(stop: threading.Event) -> None:
    """每 5 秒打印一次进度, 让用户知道没有卡死."""
    start = time.time()
    while not stop.wait(5.0):
        done = _dir_size()
        pct = min(done / TOTAL_BYTES, 1.0) * 100
        log(f"下载中 {_human(done)} / ~{_human(TOTAL_BYTES)} ({pct:5.1f}%)  已用 {time.time() - start:.0f}s")


def _modelscope_url(name: str) -> str:
    return f"{MODELSCOPE_ENDPOINT}/models/{MODELSCOPE_REPO}/resolve/{MODELSCOPE_REVISION}/{name}"


def _fetch_modelscope_file(name: str) -> None:
    """从 ModelScope 下载单个文件: 断点续传 + 长度校验 + 最多 3 次重试."""
    target = MODEL_DIR / name
    part = MODEL_DIR / f"{name}.part"
    url = _modelscope_url(name)

    for attempt in range(1, 4):
        offset = part.stat().st_size if part.is_file() else 0
        headers = {"User-Agent": USER_AGENT}
        if offset:
            headers["Range"] = f"bytes={offset}-"
        try:
            request = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(request, timeout=60) as response:
                raw_len = response.headers.get("Content-Length")
                expected = int(raw_len) if raw_len and raw_len.isdigit() else None
                if offset and response.status != 206:
                    # 服务端不支持续传, 已下的部分作废, 从头再来
                    offset = 0
                    expected = None
                received = 0
                with open(part, "ab" if offset else "wb") as handle:
                    while True:
                        chunk = response.read(1024 * 1024)
                        if not chunk:
                            break
                        handle.write(chunk)
                        received += len(chunk)
            if expected is not None and received < expected:
                raise IOError(f"字节数不足, 收到 {received} / 期望 {expected}")
            os.replace(part, target)
            return
        except Exception as exc:  # noqa: BLE001 - 重试完再抛出
            log(f"  {name} 第 {attempt}/3 次失败: {type(exc).__name__}: {exc}")
            if attempt == 3:
                raise
            time.sleep(2 * attempt)


def _download_modelscope() -> None:
    """从 ModelScope 国内源逐文件下载, 只补缺失/损坏的文件."""
    MODEL_ROOT.mkdir(parents=True, exist_ok=True)
    pending = [name for name in REQUIRED_FILES if not _file_ok(name)]
    log(f"下载源 ModelScope（国内）→ {MODEL_DIR}")
    log(f"需下载 {len(pending)}/{len(REQUIRED_FILES)} 个文件, 全量约 {_human(TOTAL_BYTES)}")

    stop = threading.Event()
    watcher = threading.Thread(target=_progress_watcher, args=(stop,), daemon=True)
    watcher.start()
    started = time.time()
    try:
        for index, name in enumerate(pending, start=1):
            log(f"[{index}/{len(pending)}] {name}")
            _fetch_modelscope_file(name)
    finally:
        stop.set()

    for stale in MODEL_DIR.glob("*.part"):
        stale.unlink(missing_ok=True)
    log(f"下载完成, 耗时 {time.time() - started:.1f}s")


def _download_hf(endpoint: str) -> None:
    from huggingface_hub import snapshot_download

    MODEL_ROOT.mkdir(parents=True, exist_ok=True)
    log(f"下载源 HuggingFace 端点 {endpoint} → {MODEL_DIR}")
    log(f"共 {len(REQUIRED_FILES)} 个文件, 约 {_human(TOTAL_BYTES)}")

    stop = threading.Event()
    watcher = threading.Thread(target=_progress_watcher, args=(stop,), daemon=True)
    watcher.start()
    started = time.time()
    try:
        snapshot_download(
            repo_id=REPO_ID,
            revision=REVISION,
            local_dir=str(MODEL_DIR),
            allow_patterns=list(REQUIRED_FILES),
            endpoint=endpoint,
            max_workers=4,
        )
    finally:
        stop.set()

    # hub 会在 local_dir 里留下 .cache 元数据, 清掉保持"模型原文件"目录干净
    cache = MODEL_DIR / ".cache"
    if cache.is_dir():
        shutil.rmtree(cache, ignore_errors=True)

    log(f"下载完成, 耗时 {time.time() - started:.1f}s")


def _run_download() -> None:
    """真正执行下载. 调用前必须已把自己登记为 downloader."""
    global _error, _endpoint_used, _source_used

    _finished.clear()
    _error = None
    try:
        _source_used = pick_source()
        if _source_used == "modelscope":
            try:
                _download_modelscope()
            except Exception as exc:  # noqa: BLE001 - ModelScope 不可用就回退 HF
                log(f"ModelScope 下载失败 ({type(exc).__name__}: {exc}), 改用 HuggingFace 重试")
                _source_used = "huggingface"
                _endpoint_used = pick_endpoint()
                _download_hf(_endpoint_used)
        else:
            _endpoint_used = pick_endpoint()
            _download_hf(_endpoint_used)
    except BaseException as exc:  # noqa: BLE001 - 记录后原样抛出
        _error = exc
        raise
    finally:
        _finished.set()


def ensure(block: bool = True) -> Path:
    """确保模型可用.

    * 没人在下载 → 由当前线程下载
    * 别人正在下 → block=True 等它结束, block=False 直接报错
    * 等的人遇到对方失败 → 抛出那次失败的原因; 自己重新发起则视为重试
    """
    global _downloader_ident, _error

    if is_ready():
        return MODEL_DIR

    current = threading.get_ident()
    waited_for_other = False
    while True:
        with _lock:
            if is_ready():
                return MODEL_DIR
            if _error is not None and waited_for_other:
                raise RuntimeError(f"MOSS 模型下载失败: {_error}") from _error
            if _downloader_ident is None:
                _downloader_ident = current  # 认领下载任务
                break
        if not block:
            raise RuntimeError("MOSS 模型正在后台下载中, 请稍候再执行节点")
        waited_for_other = True
        _finished.wait(1.0)  # 隔一秒重查, 对方失败时下一轮就能抛出来

    try:
        _run_download()
    finally:
        with _lock:
            if _downloader_ident == current:
                _downloader_ident = None

    if not is_ready():
        raise RuntimeError(f"MOSS 模型下载后仍不完整: {describe()}")
    return MODEL_DIR


def start_background_provision() -> None:
    """ComfyUI 启动时调用: 模型缺失就在后台开下, 不阻塞启动."""
    global _thread

    if os.environ.get("MOSS_AUTODOWNLOAD", "1").strip() in ("0", "false", "False", "no"):
        log("MOSS_AUTODOWNLOAD=0, 跳过自动下载")
        return

    if is_ready():
        log(describe())
        return

    if _thread is not None and _thread.is_alive():
        return

    log("模型缺失, 后台开始自动下载 (可用 MOSS_AUTODOWNLOAD=0 关闭)")

    def worker() -> None:
        try:
            ensure(block=True)
            log(describe())
        except BaseException as exc:  # noqa: BLE001 - 后台线程不能把异常抛到启动流程
            log(f"自动下载失败: {exc}")

    _thread = threading.Thread(target=worker, name="moss-local-provision", daemon=True)
    _thread.start()


def wait_ready(timeout: float | None = None) -> Path:
    """节点执行时用: 等模型就绪, 缺就补下载."""
    if is_ready():
        return MODEL_DIR
    if timeout is not None:
        _finished.wait(timeout)
        if is_ready():
            return MODEL_DIR
    return ensure(block=True)
