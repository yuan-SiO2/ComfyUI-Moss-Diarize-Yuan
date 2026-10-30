"""人声美化（Vocal Beautify）后端。

链路：前端把素材 blob 传到 ComfyUI 侧 cache/<assetId>.*（复用 /api/asset/*），
本模块调用**独立 venv** 里的 tools/voice/beautify_worker.py 做
「MossFormer2_SE_48K 神经网络增强 + DSP 增强链」，产出同长度的 48 kHz wav：

    POST /yuancut/api/voice/beautify      {assetId, strength} -> {jobId}
    GET  /yuancut/api/voice/progress      ?jobId=             -> {status, progress, stage, error}
    GET  /yuancut/api/voice/result        ?jobId=             -> wav 字节流
    GET  /yuancut/api/voice/status                            -> {available, reason}

模型和依赖都在 ~/.local/share/yuancut/voice 下，不碰 ComfyUI 主环境。
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import tempfile
import time
import uuid
from pathlib import Path

from aiohttp import web

from .yuancut_render import ASSET_ID_RE, _asset_path, _cache_dir
from .yuancut_render import logger as _log

VOICE_ROOT = Path(
    os.environ.get("YUANCUT_VOICE_ROOT", Path.home() / ".local/share/yuancut/voice")
).expanduser()
MODEL_NAME = "MossFormer2_SE_48K"
WORKER = Path(__file__).resolve().parent / "tools" / "voice" / "beautify_worker.py"

#: jobId -> job dict；同一时刻只跑一个 worker（模型加载很重）
_jobs: dict[str, dict] = {}
_queue: asyncio.Queue[str] | None = None
_worker_task: asyncio.Task | None = None

#: 终态任务留这么久，够客户端回来取结果；再久就回收
_JOB_TTL_SECONDS = 30 * 60


def _sweep_jobs() -> None:
    now = time.time()
    for job_id, job in list(_jobs.items()):
        if job.get("status") not in ("done", "error"):
            continue
        finished = job.get("finishedAt")
        if finished is None:
            job["finishedAt"] = now
            continue
        if now - finished < _JOB_TTL_SECONDS:
            continue
        workdir = job.get("workdir")
        if isinstance(workdir, Path):
            shutil.rmtree(workdir, ignore_errors=True)
        _jobs.pop(job_id, None)


def _venv_python() -> Path:
    return VOICE_ROOT / "venv" / "bin" / "python"


def _model_dir() -> Path:
    return VOICE_ROOT / "checkpoints" / MODEL_NAME


_systemd_run_checked = False
_systemd_run_ok = False


def _systemd_run_available() -> bool:
    """本机是否能用 systemd-run 把 worker 放进独立 scope（失败就退回直接跑）。"""
    global _systemd_run_checked, _systemd_run_ok
    if _systemd_run_checked:
        return _systemd_run_ok
    _systemd_run_checked = True
    binary = shutil.which("systemd-run")
    if not binary:
        return False
    try:
        probe = subprocess.run(
            [binary, "--user", "--scope", "--quiet", "--collect", "--unit", "yuancut-voice-probe", "/bin/true"],
            capture_output=True,
            timeout=10,
            check=False,
        )
        _systemd_run_ok = probe.returncode == 0
    except (OSError, subprocess.SubprocessError):
        _systemd_run_ok = False
    if not _systemd_run_ok:
        _log.info("[voice] systemd-run 不可用，worker 将直接以子进程运行")
    return _systemd_run_ok



def availability() -> tuple[bool, str]:
    """返回（是否可用, 不可用原因）。"""
    python = _venv_python()
    if not python.is_file():
        return False, f"缺少 Python 环境：{python}（运行 tools/voice/setup_env.sh）"
    if not (WORKER).is_file():
        return False, f"缺少 worker：{WORKER}"
    if not (_model_dir() / "last_best_checkpoint.pt").is_file():
        return False, f"缺少模型权重：{_model_dir()}（运行 tools/voice/setup_env.sh）"
    return True, ""


async def _run_job(job_id: str) -> None:
    job = _jobs.get(job_id)
    if job is None:
        return

    job["status"] = "running"
    job["stage"] = "starting"
    job["startedAt"] = time.time()

    command = [
        str(_venv_python()),
        str(WORKER),
        "--in",
        str(job["source"]),
        "--out",
        str(job["output"]),
        "--strength",
        f"{job['strength']:.3f}",
        "--progress",
        str(job["progressFile"]),
        "--root",
        str(VOICE_ROOT),
    ]

    # 放到独立的 systemd scope 里跑：万一它内存失控，内核只杀它，不会连带
    # 把 ComfyUI 所在的 cgroup 一起 OOM 掉（之前就吃过这个亏）。
    if _systemd_run_available():
        command = [
            "systemd-run",
            "--user",
            "--scope",
            "--quiet",
            "--collect",
            "--unit",
            f"yuancut-voice-{job_id}",
            *command,
        ]

    _log.info(f"[voice] 开始处理 {job['source'].name} (strength={job['strength']:.2f})")

    try:
        process = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
    except OSError as exc:
        job.update(status="error", error=f"无法启动 worker：{exc}")
        return

    tail: list[str] = []
    assert process.stdout is not None
    async for raw in process.stdout:
        line = raw.decode("utf-8", "ignore").rstrip()
        if not line:
            continue
        tail.append(line)
        if len(tail) > 40:
            tail.pop(0)
        _refresh_progress(job)

    returncode = await process.wait()
    _refresh_progress(job)

    if returncode != 0 or not Path(job["output"]).is_file():
        job.update(
            status="error",
            error="\n".join(tail[-6:]) or f"处理失败（退出码 {returncode}）",
        )
        _log.info(f"[voice] 处理失败：{job['error'][:300]}")
    else:
        job.update(status="done", stage="done", progress=1.0)
        _log.info(
            f"[voice] 处理完成 {job['output'].name}，用时 {time.time() - job['startedAt']:.1f}s"
        )


def _refresh_progress(job: dict) -> None:
    path: Path = job["progressFile"]
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    stage = payload.get("stage")
    if isinstance(stage, str):
        job["stage"] = stage
    progress = payload.get("progress")
    if isinstance(progress, (int, float)):
        job["progress"] = float(progress)


async def _pump_queue() -> None:
    assert _queue is not None
    while True:
        job_id = await _queue.get()
        try:
            await _run_job(job_id)
        except Exception as exc:  # noqa: BLE001 - 单个任务失败不能拖垮队列
            job = _jobs.get(job_id)
            if job is not None:
                job.update(status="error", error=f"内部错误：{exc}")
            _log.info(f"[voice] 队列异常：{exc}")


def _ensure_queue() -> asyncio.Queue[str]:
    global _queue, _worker_task
    if _queue is None:
        _queue = asyncio.Queue()
    if _worker_task is None or _worker_task.done():
        _worker_task = asyncio.create_task(_pump_queue())
    return _queue


async def voice_status(request: web.Request) -> web.Response:  # noqa: ANN001
    ok, reason = availability()
    return web.json_response({"available": ok, "reason": reason})


async def voice_beautify(request: web.Request) -> web.Response:  # noqa: ANN001
    _sweep_jobs()
    ok, reason = availability()
    if not ok:
        return web.json_response({"error": reason}, status=503)

    try:
        payload = await request.json()
    except Exception:  # noqa: BLE001 - 客户端偶尔发空体
        return web.json_response({"error": "invalid json"}, status=400)

    asset_id = str(payload.get("assetId", ""))
    if not ASSET_ID_RE.match(asset_id):
        return web.json_response({"error": "invalid assetId"}, status=400)

    source = _asset_path(asset_id)
    if source is None or not source.is_file():
        return web.json_response({"error": "素材还没上传到服务端"}, status=404)

    try:
        strength = float(payload.get("strength", 1.0))
    except (TypeError, ValueError):
        strength = 1.0
    strength = min(1.0, max(0.0, strength))

    job_id = uuid.uuid4().hex[:16]
    workdir = Path(tempfile.mkdtemp(prefix=f"yuancut-voice-{job_id}-"))
    output = _cache_dir() / f"{asset_id}-vb-{job_id[:8]}.wav"
    _jobs[job_id] = {
        "status": "queued",
        "stage": "queued",
        "progress": 0.0,
        "strength": strength,
        "source": source,
        "output": output,
        "progressFile": workdir / "progress.json",
        "workdir": workdir,
        "error": "",
        "createdAt": time.time(),
    }
    await _ensure_queue().put(job_id)
    _log.info(f"[voice] 排队 {job_id} ← {source.name} (strength={strength:.2f})")
    return web.json_response({"jobId": job_id})


async def voice_progress(request: web.Request) -> web.Response:  # noqa: ANN001
    _sweep_jobs()
    job_id = request.query.get("jobId", "")
    job = _jobs.get(job_id)
    if job is None:
        return web.json_response({"error": "unknown job"}, status=404)

    _refresh_progress(job)
    return web.json_response(
        {
            "status": job["status"],
            "stage": job.get("stage", ""),
            "progress": job.get("progress", 0.0),
            "error": job.get("error", ""),
        }
    )


async def voice_result(request: web.Request) -> web.Response:  # noqa: ANN001
    job_id = request.query.get("jobId", "")
    job = _jobs.get(job_id)
    if job is None:
        return web.json_response({"error": "unknown job"}, status=404)
    if job["status"] != "done":
        return web.json_response({"error": "job not done"}, status=409)

    output: Path = job["output"]
    if not output.is_file():
        return web.json_response({"error": "结果文件丢失"}, status=410)

    # 结果被取走后清掉 job（缓存文件保留，供导出引用）
    body = output.read_bytes()
    shutil.rmtree(job["workdir"], ignore_errors=True)
    _jobs.pop(job_id, None)
    return web.Response(body=body, content_type="audio/wav")


async def voice_cancel(request: web.Request) -> web.Response:  # noqa: ANN001
    job_id = request.query.get("jobId", "")
    job = _jobs.pop(job_id, None)
    if job is not None:
        shutil.rmtree(job["workdir"], ignore_errors=True)
    return web.json_response({"ok": True})


def register_routes(routes) -> None:  # noqa: ANN001 - aiohttp RouteTableDef
    routes.get("/yuancut/api/voice/status")(voice_status)
    routes.post("/yuancut/api/voice/beautify")(voice_beautify)
    routes.get("/yuancut/api/voice/progress")(voice_progress)
    routes.get("/yuancut/api/voice/result")(voice_result)
    routes.post("/yuancut/api/voice/cancel")(voice_cancel)
