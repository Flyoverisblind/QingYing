from __future__ import annotations

import asyncio
import mimetypes
import os
import re
import shutil
import threading
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, File, HTTPException, Query, Request, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import config
from .ai import ai_status, probe_ai
from .ffmpeg import check_binaries, codec_options, detect_encoders, probe_media
from .fs import (
    build_preview,
    list_directory,
    list_roots,
    open_external,
    open_in_file_manager,
    thumbnail,
)
from .jobs import manager, system_snapshot, warm_ai_probe
from .schemas import JobCreate, OpenPathRequest, PreviewRequest, ProbeRequest


def _startup() -> None:
    config.ensure_dirs()
    manager.start()
    threading.Thread(target=warm_ai_probe, name="ai-probe", daemon=True).start()
    threading.Thread(target=detect_encoders, name="encoder-probe", daemon=True).start()


@asynccontextmanager
async def lifespan(app: FastAPI):
    _startup()
    yield
    manager.shutdown()


app = FastAPI(title="清影 QingYing", version=config.VERSION, lifespan=lifespan)


@app.middleware("http")
async def no_cache_static(request: Request, call_next):
    response = await call_next(request)
    if request.url.path == "/" or request.url.path.startswith("/static/"):
        response.headers["Cache-Control"] = "no-cache"
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response


@app.get("/")
async def index() -> FileResponse:
    index_file = config.STATIC_DIR / "index.html"
    if not index_file.exists():
        raise HTTPException(500, "前端资源缺失")
    return FileResponse(index_file, media_type="text/html")


@app.get("/favicon.ico")
async def favicon() -> FileResponse:
    for name in ("favicon.ico", "icon.png"):
        candidate = config.STATIC_DIR / name
        if candidate.exists():
            return FileResponse(candidate)
    raise HTTPException(404, "not found")


@app.get("/api/health")
async def health() -> dict[str, Any]:
    return {"ok": True, "version": config.VERSION, "time": time.time()}


@app.get("/api/system")
async def system() -> dict[str, Any]:
    return await run_in_threadpool(system_snapshot)


@app.post("/api/system/refresh-ai")
async def refresh_ai() -> dict[str, Any]:
    await run_in_threadpool(probe_ai, True)
    return await run_in_threadpool(ai_status)


@app.get("/api/fs")
async def browse(path: str | None = Query(default=None)) -> dict[str, Any]:
    try:
        return await run_in_threadpool(list_directory, path)
    except FileNotFoundError as exc:
        raise HTTPException(404, str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(403, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.get("/api/roots")
async def roots() -> dict[str, Any]:
    return {"roots": list_roots(), "home": str(Path.home())}


@app.post("/api/probe")
async def probe(payload: ProbeRequest) -> dict[str, Any]:
    try:
        info = await run_in_threadpool(probe_media, payload.path)
    except FileNotFoundError as exc:
        raise HTTPException(404, str(exc)) from exc
    except Exception as exc:
        raise HTTPException(400, str(exc)) from exc
    if Path(info["path"]).suffix.lower() not in config.VIDEO_EXTENSIONS:
        raise HTTPException(400, "仅支持视频文件")
    return info


@app.get("/api/thumbnail")
async def thumbnail_endpoint(path: str, duration: float | None = None) -> FileResponse:
    try:
        file_path = await run_in_threadpool(thumbnail, path, duration)
    except FileNotFoundError as exc:
        raise HTTPException(404, str(exc)) from exc
    except Exception as exc:
        raise HTTPException(400, str(exc)) from exc
    return FileResponse(
        file_path,
        media_type="image/jpeg",
        headers={"Cache-Control": "public, max-age=86400"},
    )


@app.post("/api/preview")
async def preview(payload: PreviewRequest) -> dict[str, Any]:
    try:
        return await run_in_threadpool(build_preview, payload.path, payload.time, payload.options)
    except FileNotFoundError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except Exception as exc:
        raise HTTPException(500, f"预览生成失败：{exc}") from exc


@app.post("/api/jobs")
async def create_job(payload: JobCreate) -> dict[str, Any]:
    try:
        job = await run_in_threadpool(manager.create, payload.source, payload.options, payload.preview)
    except FileNotFoundError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except Exception as exc:
        raise HTTPException(500, str(exc)) from exc
    return job.snapshot(include_log=True)


@app.get("/api/jobs")
async def list_jobs() -> dict[str, Any]:
    return {"jobs": manager.list(), "active": manager.active_count()}


@app.get("/api/jobs/{job_id}")
async def get_job(job_id: str) -> dict[str, Any]:
    job = manager.get(job_id)
    if not job:
        raise HTTPException(404, "任务不存在")
    return job.snapshot(include_log=True)


@app.post("/api/jobs/{job_id}/cancel")
async def cancel_job(job_id: str) -> dict[str, Any]:
    if not manager.cancel(job_id):
        raise HTTPException(400, "任务无法取消")
    job = manager.get(job_id)
    return job.snapshot(include_log=True) if job else {"ok": True}


@app.delete("/api/jobs/{job_id}")
async def remove_job(job_id: str) -> dict[str, Any]:
    if not manager.remove(job_id):
        raise HTTPException(400, "运行中的任务不能删除")
    return {"ok": True}


@app.get("/api/jobs/{job_id}/file")
async def job_file(job_id: str):
    job = manager.get(job_id)
    if not job:
        raise HTTPException(404, "任务不存在")
    if job.status != "done" or not job.output:
        raise HTTPException(409, "任务尚未完成")
    path = Path(job.output)
    if not path.exists():
        raise HTTPException(404, "输出文件已被移动或删除")
    media_type = mimetypes.guess_type(path.name)[0] or "video/mp4"
    return FileResponse(
        path,
        media_type=media_type,
        filename=path.name,
        content_disposition_type="inline",
    )


@app.post("/api/upload")
async def upload(file: UploadFile = File(...)) -> dict[str, Any]:
    original = Path(file.filename or "video.mp4").name
    # Browsers send the multipart filename as UTF-8, while some parsers expose
    # those bytes as latin-1.  Recover the readable name when possible.
    try:
        repaired = original.encode("latin-1").decode("utf-8")
        if repaired and repaired != original:
            original = repaired
    except (UnicodeEncodeError, UnicodeDecodeError):
        pass
    suffix = Path(original).suffix.lower()
    if suffix not in config.VIDEO_EXTENSIONS:
        raise HTTPException(400, "不支持的文件格式")
    safe_stem = re.sub(r"[^\w\u4e00-\u9fff.-]+", "_", Path(original).stem)[:80] or "video"
    destination = config.UPLOAD_DIR / f"{safe_stem}{suffix}"
    if destination.exists():
        destination = config.UPLOAD_DIR / f"{safe_stem}_{uuid.uuid4().hex[:6]}{suffix}"
    total = 0
    try:
        with destination.open("wb") as handle:
            while True:
                chunk = await file.read(4 * 1024 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > config.MAX_UPLOAD_BYTES:
                    raise HTTPException(
                        413,
                        f"文件超过上传上限（{config.MAX_UPLOAD_BYTES // 1024**3} GB），"
                        "大文件请使用「浏览本机文件」",
                    )
                handle.write(chunk)
    except HTTPException:
        destination.unlink(missing_ok=True)
        raise
    except Exception as exc:
        destination.unlink(missing_ok=True)
        raise HTTPException(500, f"上传失败：{exc}") from exc
    return {
        "path": str(destination),
        "name": destination.name,
        "size": total,
        "original_name": original,
    }


@app.post("/api/open-folder")
async def open_folder(payload: OpenPathRequest) -> dict[str, Any]:
    try:
        await run_in_threadpool(open_in_file_manager, payload.path)
    except FileNotFoundError as exc:
        raise HTTPException(404, str(exc)) from exc
    except Exception as exc:
        raise HTTPException(500, f"无法打开文件夹：{exc}") from exc
    return {"ok": True}


@app.post("/api/open-file")
async def open_file(payload: OpenPathRequest) -> dict[str, Any]:
    try:
        await run_in_threadpool(open_external, payload.path)
    except FileNotFoundError as exc:
        raise HTTPException(404, str(exc)) from exc
    except Exception as exc:
        raise HTTPException(500, f"无法打开文件：{exc}") from exc
    return {"ok": True}


@app.post("/api/shutdown")
async def shutdown() -> dict[str, Any]:
    def _exit() -> None:
        try:
            (config.DATA_DIR / "runtime.json").unlink(missing_ok=True)
        except Exception:
            pass
        time.sleep(0.35)
        os._exit(0)

    threading.Thread(target=_exit, daemon=True).start()
    return {"ok": True, "message": "应用即将退出"}


@app.exception_handler(Exception)
async def unhandled_exception(request: Request, exc: Exception):
    return JSONResponse(
        status_code=500,
        content={"detail": f"服务器内部错误：{exc}"},
    )


static_dir = config.STATIC_DIR
if static_dir.exists():
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")
