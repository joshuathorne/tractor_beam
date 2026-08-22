"""tractor-beam — a local, self-hosted replacement for paste-a-URL downloader sites.

Engine is yt-dlp; ffmpeg does the muxing. Nothing leaves this machine except the
request yt-dlp makes to the site you asked for.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from pathlib import Path
from urllib.parse import urlparse

import yt_dlp
from fastapi import FastAPI
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel, field_validator

HERE = Path(__file__).resolve().parent
MAX_CONCURRENT = int(os.environ.get("TRACTOR_BEAM_JOBS", "3"))


def default_output_dir() -> Path:
    """Land files outside the WSL VHDX by default so the virtual disk never swells."""
    if env := os.environ.get("TRACTOR_BEAM_OUT"):
        return Path(env)
    skip = {"public", "default", "default user", "all users"}
    for downloads in sorted(Path("/mnt/c/Users").glob("*/Downloads")):
        if downloads.parent.name.lower() in skip:
            continue
        if downloads.is_dir() and os.access(downloads, os.W_OK):
            return downloads / "tractor_beam"
    return Path.home() / "Downloads" / "tractor_beam"


OUT_DIR = default_output_dir()
OUT_DIR.mkdir(parents=True, exist_ok=True)


# ---------------------------------------------------------------- job state


@dataclass
class Job:
    id: str
    url: str
    mode: str
    label: str
    title: str = ""
    status: str = "queued"          # queued | running | postprocess | done | error | cancelled
    percent: float = 0.0
    speed: str = ""
    eta: str = ""
    size: str = ""
    stage: str = ""
    filename: str = ""
    error: str = ""
    cancel: bool = field(default=False, repr=False)

    def public(self) -> dict:
        d = asdict(self)
        d.pop("cancel", None)
        return d


JOBS: dict[str, Job] = {}
EXECUTOR = ThreadPoolExecutor(max_workers=MAX_CONCURRENT, thread_name_prefix="grab")


class Cancelled(Exception):
    """Raised out of a progress hook to abort a running download."""


def human_bytes(n: float | None) -> str:
    if not n:
        return ""
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.1f} {unit}".replace(".0 ", " ")
        n /= 1024
    return f"{n:.1f} PB"


def human_eta(seconds: float | None) -> str:
    if not seconds or seconds < 0:
        return ""
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m {seconds % 60:02d}s"
    return f"{seconds // 3600}h {(seconds % 3600) // 60:02d}m"


# ---------------------------------------------------------------- yt-dlp glue


def build_opts(job: Job, quality: str, audio_format: str) -> dict:
    def progress_hook(d: dict) -> None:
        if job.cancel:
            raise Cancelled()
        if d["status"] == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate")
            done = d.get("downloaded_bytes") or 0
            job.status = "running"
            job.percent = round(done / total * 100, 1) if total else 0.0
            job.speed = f"{human_bytes(d.get('speed'))}/s" if d.get("speed") else ""
            job.eta = human_eta(d.get("eta"))
            job.size = human_bytes(total)
            frag_i, frag_n = d.get("fragment_index"), d.get("fragment_count")
            job.stage = f"fragment {frag_i}/{frag_n}" if frag_i and frag_n else "downloading"
        elif d["status"] == "finished":
            job.percent = 100.0
            job.speed = job.eta = ""
            job.stage = "merging / converting"
            job.status = "postprocess"

    def postprocessor_hook(d: dict) -> None:
        if job.cancel:
            raise Cancelled()
        if d["status"] == "started":
            job.status = "postprocess"
            job.stage = d.get("postprocessor", "processing")

    opts: dict = {
        "outtmpl": {"default": str(OUT_DIR / "%(title).180B [%(id)s].%(ext)s")},
        "paths": {"home": str(OUT_DIR)},
        "progress_hooks": [progress_hook],
        "postprocessor_hooks": [postprocessor_hook],
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "noplaylist": True,
        "windowsfilenames": True,      # output usually lands on NTFS via /mnt/c
        "concurrent_fragment_downloads": 4,
        "retries": 10,
        "fragment_retries": 10,
        "continuedl": True,
        "overwrites": False,
        "postprocessors": [],
    }

    if job.mode == "audio":
        opts["format"] = "bestaudio/best"
        opts["writethumbnail"] = True
        opts["postprocessors"] = [
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": audio_format,
                "preferredquality": "0",
            },
            {"key": "FFmpegMetadata", "add_metadata": True},
            {"key": "EmbedThumbnail", "already_have_thumbnail": False},
        ]
    else:
        if quality == "best":
            selector = (
                "bestvideo[ext=mp4]+bestaudio[ext=m4a]/bestvideo+bestaudio/best"
            )
        else:
            h = int(quality)
            selector = (
                f"bestvideo[ext=mp4][height<={h}]+bestaudio[ext=m4a]/"
                f"bestvideo[height<={h}]+bestaudio/"
                f"best[height<={h}]/best"
            )
        opts["format"] = selector
        opts["merge_output_format"] = "mp4"
        opts["postprocessors"] = [{"key": "FFmpegMetadata", "add_metadata": True}]

    return opts


def run_job(job_id: str, quality: str, audio_format: str) -> None:
    job = JOBS[job_id]
    job.status = "running"
    job.stage = "resolving"
    try:
        with yt_dlp.YoutubeDL(build_opts(job, quality, audio_format)) as ydl:
            info = ydl.extract_info(job.url, download=True)
            job.title = info.get("title") or job.title
            requested = info.get("requested_downloads") or []
            path = requested[0].get("filepath") if requested else None
            if path:
                job.filename = Path(path).name
                job.size = human_bytes(Path(path).stat().st_size)
        job.status = "done"
        job.stage = ""
        job.percent = 100.0
    except Cancelled:
        job.status = "cancelled"
        job.stage = ""
    except yt_dlp.utils.DownloadError as exc:
        job.status = "error"
        job.stage = ""
        job.error = re.sub(r"\x1b\[[0-9;]*m", "", str(exc)).strip()
    except Exception as exc:  # surfaced to the UI, never swallowed
        job.status = "error"
        job.stage = ""
        job.error = f"{type(exc).__name__}: {exc}"


# ---------------------------------------------------------------- api


app = FastAPI(title="tractor-beam", docs_url=None, redoc_url=None)


class UrlPayload(BaseModel):
    url: str

    @field_validator("url")
    @classmethod
    def http_only(cls, v: str) -> str:
        v = v.strip()
        if urlparse(v).scheme not in {"http", "https"}:
            raise ValueError("URL must start with http:// or https://")
        return v


class GrabPayload(UrlPayload):
    mode: str = "video"
    quality: str = "best"
    audio_format: str = "mp3"

    @field_validator("mode")
    @classmethod
    def known_mode(cls, v: str) -> str:
        if v not in {"video", "audio"}:
            raise ValueError("mode must be 'video' or 'audio'")
        return v

    @field_validator("audio_format")
    @classmethod
    def known_audio(cls, v: str) -> str:
        if v not in {"mp3", "m4a", "opus", "flac", "wav"}:
            raise ValueError("unsupported audio format")
        return v


@app.get("/", response_class=HTMLResponse)
async def index() -> HTMLResponse:
    return HTMLResponse((HERE / "static" / "index.html").read_text(encoding="utf-8"))


@app.get("/api/config")
async def config() -> dict:
    return {"output_dir": str(OUT_DIR), "yt_dlp": yt_dlp.version.__version__}


@app.post("/api/inspect")
async def inspect(payload: UrlPayload):
    def probe() -> dict:
        opts = {"quiet": True, "no_warnings": True, "noplaylist": True, "skip_download": True}
        with yt_dlp.YoutubeDL(opts) as ydl:
            return ydl.extract_info(payload.url, download=False)

    try:
        info = await asyncio.get_running_loop().run_in_executor(EXECUTOR, probe)
    except yt_dlp.utils.DownloadError as exc:
        msg = re.sub(r"\x1b\[[0-9;]*m", "", str(exc)).strip()
        return JSONResponse({"error": msg}, status_code=422)

    heights = sorted(
        {f["height"] for f in (info.get("formats") or []) if f.get("height")},
        reverse=True,
    )
    return {
        "title": info.get("title") or "",
        "uploader": info.get("uploader") or info.get("channel") or "",
        "duration": info.get("duration") or 0,
        "thumbnail": info.get("thumbnail") or "",
        "extractor": info.get("extractor_key") or "",
        "heights": heights,
    }


@app.post("/api/grab")
async def grab(payload: GrabPayload) -> dict:
    job = Job(
        id=uuid.uuid4().hex[:12],
        url=payload.url,
        mode=payload.mode,
        label=payload.audio_format.upper() if payload.mode == "audio" else f"MP4 {payload.quality}",
    )
    JOBS[job.id] = job
    asyncio.get_running_loop().run_in_executor(
        EXECUTOR, run_job, job.id, payload.quality, payload.audio_format
    )
    return job.public()


@app.post("/api/cancel/{job_id}")
async def cancel(job_id: str) -> dict:
    if job := JOBS.get(job_id):
        job.cancel = True
        return {"ok": True}
    return JSONResponse({"error": "no such job"}, status_code=404)


@app.post("/api/clear")
async def clear() -> dict:
    for jid in [j for j, job in JOBS.items() if job.status in {"done", "error", "cancelled"}]:
        JOBS.pop(jid, None)
    return {"ok": True}


@app.get("/api/file/{job_id}")
async def file(job_id: str):
    job = JOBS.get(job_id)
    if not job or not job.filename:
        return JSONResponse({"error": "not ready"}, status_code=404)
    path = (OUT_DIR / job.filename).resolve()
    if not path.is_file() or OUT_DIR.resolve() not in path.parents:
        return JSONResponse({"error": "not found"}, status_code=404)
    return FileResponse(path, filename=path.name)


@app.get("/api/stream")
async def stream() -> StreamingResponse:
    async def events():
        last = ""
        while True:
            snap = json.dumps([j.public() for j in JOBS.values()])
            if snap != last:
                last = snap
                yield f"data: {snap}\n\n"
            await asyncio.sleep(0.4)

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
