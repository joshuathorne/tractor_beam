"""tractor-beam — a local, self-hosted replacement for paste-a-URL downloader sites.

Engine is yt-dlp; ffmpeg does the muxing. Nothing leaves this machine except the
request yt-dlp makes to the site you asked for.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import subprocess
import sys
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from pathlib import Path
from urllib.parse import urlparse

import yt_dlp
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel, field_validator

HERE = Path(__file__).resolve().parent
MAX_CONCURRENT = int(os.environ.get("TRACTOR_BEAM_JOBS", "3"))


WINDOWS = sys.platform == "win32"


def default_output_dir() -> Path:
    """Land files outside the WSL VHDX by default so the virtual disk never swells.

    Running natively there is nothing to escape, and Path.home() is already the
    Windows profile, so the plain fallback is the right answer.
    """
    if env := os.environ.get("TRACTOR_BEAM_OUT"):
        return Path(env)
    if not WINDOWS:
        skip = {"public", "default", "default user", "all users"}
        for downloads in sorted(Path("/mnt/c/Users").glob("*/Downloads")):
            if downloads.parent.name.lower() in skip:
                continue
            if downloads.is_dir() and os.access(downloads, os.W_OK):
                return downloads / "tractor_beam"
    return Path.home() / "Downloads" / "tractor_beam"


OUT_DIR = default_output_dir()
OUT_DIR.mkdir(parents=True, exist_ok=True)


def find_file_manager() -> str | None:
    """Whichever file manager this host offers. explorer.exe first: under WSL both exist."""
    if WINDOWS:
        return "explorer.exe"
    for cmd in ("explorer.exe", "xdg-open", "open"):
        if shutil.which(cmd):
            return cmd
    return None


FILE_MANAGER = find_file_manager()


def has_cookie_db(browser: str, profile: Path) -> bool:
    """Firefox keeps one file; chromium-family hides it a level or two down."""
    if browser == "firefox":
        return (profile / "cookies.sqlite").is_file()
    return any((profile / rel).is_file() for rel in ("Default/Cookies", "Default/Network/Cookies"))


def windows_cookie_sources() -> list[tuple[str, str, str]]:
    """Running natively, DPAPI unseals normally, so the chromium family is usable too."""
    found: list[tuple[str, str, str]] = []
    appdata = Path(os.environ.get("APPDATA", ""))
    local = Path(os.environ.get("LOCALAPPDATA", ""))

    for prof in sorted((appdata / "Mozilla/Firefox/Profiles").glob("*.default*")):
        if has_cookie_db("firefox", prof):
            found.append(("firefox", f"Firefox · {prof.name.split('.', 1)[-1]}", str(prof)))

    for browser, base, rel in (("chrome", local, "Google/Chrome/User Data"),
                               ("edge", local, "Microsoft/Edge/User Data"),
                               ("brave", local, "BraveSoftware/Brave-Browser/User Data"),
                               ("vivaldi", local, "Vivaldi/User Data"),
                               ("chromium", local, "Chromium/User Data"),
                               ("opera", appdata, "Opera Software/Opera Stable")):
        if has_cookie_db(browser, d := base / rel):
            found.append((browser, browser.capitalize(), str(d)))
    return found


def posix_cookie_sources() -> list[tuple[str, str, str]]:
    """Under WSL the Windows profiles show up through /mnt/c.

    Chromium-family profiles on the Windows side are deliberately left out there:
    their cookies are sealed with DPAPI, which cannot be unlocked from inside WSL.
    """
    found: list[tuple[str, str, str]] = []

    for base in (Path.home() / ".mozilla/firefox",
                 Path.home() / "snap/firefox/common/.mozilla/firefox"):
        if base.is_dir():
            for prof in sorted(base.glob("*.default*")):
                if has_cookie_db("firefox", prof):
                    found.append(("firefox", f"Firefox · {prof.name.split('.', 1)[-1]}", str(prof)))

    for prof in sorted(Path("/mnt/c/Users").glob("*/AppData/Roaming/Mozilla/Firefox/Profiles/*.default*")):
        if has_cookie_db("firefox", prof):
            found.append(("firefox", f"Firefox · {prof.name.split('.', 1)[-1]} (Windows)", str(prof)))

    for browser, rel in (("chrome", ".config/google-chrome"),
                         ("chromium", ".config/chromium"),
                         ("brave", ".config/BraveSoftware/Brave-Browser"),
                         ("edge", ".config/microsoft-edge"),
                         ("vivaldi", ".config/vivaldi"),
                         ("opera", ".config/opera")):
        if (d := Path.home() / rel).is_dir() and has_cookie_db(browser, d):
            found.append((browser, browser.capitalize(), str(d)))
    return found


def find_cookie_sources() -> dict[str, dict]:
    """Browser profiles this host can read, whichever platform it is."""
    found = windows_cookie_sources() if WINDOWS else posix_cookie_sources()
    return {
        f"{browser}-{i}": {"browser": browser, "label": label, "profile": profile}
        for i, (browser, label, profile) in enumerate(found)
    }


COOKIE_SOURCES = find_cookie_sources()
FFMPEG = shutil.which(os.environ.get("TRACTOR_BEAM_FFMPEG") or "ffmpeg")

# Cloudflare turns away yt-dlp's stock TLS fingerprint with a 403. curl_cffi makes
# the generic extractor's requests look like a real browser's instead.
IMPERSONATE_ARGS = {"generic": {"impersonate": [""]}}


def cookie_opts(key: str) -> dict:
    """yt-dlp options for one detected profile. Unknown keys mean no cookies, never a guess."""
    src = COOKIE_SOURCES.get(key)
    if not src:
        return {}
    return {"cookiesfrombrowser": (src["browser"], src["profile"] or None, None, None)}


def open_in_file_manager(path: Path) -> None:
    """Pop the host's file browser open on `path`, selecting it when it is a file."""
    if WINDOWS:
        # Passing one raw command line skips subprocess's own quoting, so the quotes
        # land where Explorer wants them and /select genuinely selects — the thing
        # WSL cannot do, because its interop layer quotes the argument first.
        target = path if path.exists() else OUT_DIR
        switch = f'/select,"{target}"' if target.is_file() else f'"{target}"'
        subprocess.Popen(f"explorer.exe {switch}")
        return
    if FILE_MANAGER == "explorer.exe":
        # Explorer's "/select,<file>" switch dies as soon as WSL quotes the argument
        # for a path containing spaces, and it silently opens Documents instead. So
        # open the containing folder, which survives quoting.
        target = path if path.is_dir() else path.parent
        win = subprocess.run(
            ["wslpath", "-w", str(target)], capture_output=True, text=True, check=False
        ).stdout.strip()
        if not win:
            return
        args = ["explorer.exe", win]
    elif FILE_MANAGER == "open":
        args = ["open", "-R", str(path)] if path.is_file() else ["open", str(path)]
    else:
        args = ["xdg-open", str(path if path.is_dir() else path.parent)]
    # explorer.exe exits 1 even on success, so there is no status worth waiting for.
    subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


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
EXTRACTOR_CACHE: list[dict] | None = None
EXECUTOR = ThreadPoolExecutor(max_workers=MAX_CONCURRENT, thread_name_prefix="grab")


class Cancelled(Exception):
    """Raised out of a progress hook to abort a running download."""


def clean_error(exc: Exception) -> str:
    return re.sub(r"\x1b\[[0-9;]*m", "", str(exc)).strip()


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


def build_opts(job: Job, quality: str, audio_format: str, cookies: str = "") -> dict:
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
        "windowsfilenames": True,      # output lands on NTFS either way
        "concurrent_fragment_downloads": 4,
        "retries": 10,
        "fragment_retries": 10,
        "continuedl": True,
        "overwrites": False,
        "postprocessors": [],
        "extractor_args": IMPERSONATE_ARGS,
        **({"ffmpeg_location": FFMPEG} if FFMPEG else {}),
        **cookie_opts(cookies),
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


def run_job(job_id: str, quality: str, audio_format: str, cookies: str = "") -> None:
    job = JOBS[job_id]
    job.status = "running"
    job.stage = "resolving"
    try:
        with yt_dlp.YoutubeDL(build_opts(job, quality, audio_format, cookies)) as ydl:
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
        job.error = clean_error(exc)
    except Exception as exc:  # surfaced to the UI, never swallowed
        job.status = "error"
        job.stage = ""
        job.error = f"{type(exc).__name__}: {exc}"


# ---------------------------------------------------------------- api


app = FastAPI(title="tractor-beam", docs_url=None, redoc_url=None)


class UrlPayload(BaseModel):
    url: str
    cookies: str = ""

    @field_validator("url")
    @classmethod
    def http_only(cls, v: str) -> str:
        v = v.strip()
        if urlparse(v).scheme not in {"http", "https"}:
            raise ValueError("URL must start with http:// or https://")
        return v

    @field_validator("cookies")
    @classmethod
    def known_source(cls, v: str) -> str:
        """Only ids this server itself advertised, so no caller can name an arbitrary path."""
        if v and v not in COOKIE_SOURCES:
            raise ValueError("unknown cookie source")
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
    return {
        "output_dir": str(OUT_DIR),
        "yt_dlp": yt_dlp.version.__version__,
        "can_reveal": FILE_MANAGER is not None,
        "ffmpeg": bool(FFMPEG),
        "browsers": [{"id": k, "label": v["label"]} for k, v in COOKIE_SOURCES.items()],
    }


def site_home(ie) -> str:
    """Extractor names say little, but their own test URLs name the site they belong to."""
    tests = list(getattr(ie, "_TESTS", None) or [])
    if (single := getattr(ie, "_TEST", None)):
        tests.append(single)
    for t in tests:
        if isinstance(t, dict) and (url := t.get("url")):
            if netloc := urlparse(url).netloc:
                return f"https://{netloc}/"
    return ""


@app.get("/api/extractors")
async def extractors() -> dict:
    """Built on first request: reading _TESTS pulls in every extractor module."""
    global EXTRACTOR_CACHE
    if EXTRACTOR_CACHE is None:
        from yt_dlp.extractor import gen_extractor_classes

        EXTRACTOR_CACHE = sorted(
            ({"name": ie.IE_NAME,
              "ok": ie.working(),
              "url": site_home(ie),
              "desc": ie.IE_DESC if isinstance(ie.IE_DESC, str) else ""}
             for ie in gen_extractor_classes()
             if ie.IE_NAME and ie.IE_NAME.lower() != "generic"),
            key=lambda e: e["name"].lower(),
        )
    return {"sites": EXTRACTOR_CACHE}


@app.post("/api/inspect")
async def inspect(payload: UrlPayload):
    def probe(cookies: str) -> dict:
        opts = {"quiet": True, "no_warnings": True, "noplaylist": True, "skip_download": True,
                "extractor_args": IMPERSONATE_ARGS, **cookie_opts(cookies)}
        with yt_dlp.YoutubeDL(opts) as ydl:
            return ydl.extract_info(payload.url, download=False)

    loop = asyncio.get_running_loop()
    used = payload.cookies
    try:
        info = await loop.run_in_executor(EXECUTOR, probe, payload.cookies)
    except yt_dlp.utils.DownloadError as exc:
        # A borrowed session can make a public URL fail that works fine anonymously —
        # YouTube in particular rejects rotated cookies. Worth one plain retry.
        first = clean_error(exc)
        if not payload.cookies:
            return JSONResponse({"error": first}, status_code=422)
        try:
            info = await loop.run_in_executor(EXECUTOR, probe, "")
            used = ""
        except yt_dlp.utils.DownloadError:
            return JSONResponse({"error": first}, status_code=422)

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
        "cookies_used": used,
        "cookies_dropped": bool(payload.cookies) and not used,
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
        EXECUTOR, run_job, job.id, payload.quality, payload.audio_format, payload.cookies
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


def reveal_response(request: Request, path: Path):
    """Opening a window only helps whoever is sitting at this machine, so keep it local."""
    if not request.client or request.client.host not in {"127.0.0.1", "::1"}:
        return JSONResponse({"error": "Only available on the machine running tractor beam"}, status_code=403)
    if not FILE_MANAGER:
        return JSONResponse({"error": "No file manager found on this host"}, status_code=501)
    open_in_file_manager(path)
    return {"ok": True}


@app.post("/api/reveal")
async def reveal_dir(request: Request):
    return reveal_response(request, OUT_DIR)


@app.post("/api/reveal/{job_id}")
async def reveal_job(job_id: str, request: Request):
    """Falls back to the output directory when the file has been moved or renamed since."""
    job = JOBS.get(job_id)
    target = OUT_DIR
    if job and job.filename:
        path = (OUT_DIR / job.filename).resolve()
        if path.is_file() and OUT_DIR.resolve() in path.parents:
            target = path
    return reveal_response(request, target)


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
