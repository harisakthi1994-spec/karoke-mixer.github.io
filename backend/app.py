from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
from collections import defaultdict, deque
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse

log = logging.getLogger("uvicorn.error")

APP_ROOT = Path(__file__).resolve().parent
RESULTS_ROOT = Path(os.getenv("RESULTS_ROOT", APP_ROOT / "results"))
RESULTS_ROOT.mkdir(parents=True, exist_ok=True)

# Comma-separated browser origins. Use * only for local testing.
ALLOWED_ORIGINS = [
    origin.strip()
    for origin in os.getenv(
        "ALLOWED_ORIGINS", "https://harisakthi1994-spec.github.io,http://localhost:8000"
    ).split(",")
    if origin.strip()
]

MAX_UPLOAD_MB = int(os.getenv("MAX_UPLOAD_MB", "150"))
MAX_UPLOAD_BYTES = MAX_UPLOAD_MB * 1024 * 1024

DEMUCS_MODEL = os.getenv("DEMUCS_MODEL", "htdemucs")
DEMUCS_TIMEOUT_SECONDS = int(os.getenv("DEMUCS_TIMEOUT_SECONDS", "1800"))

# mp3 is ~8x smaller than wav, which matters for the browser download.
OUTPUT_FORMAT = os.getenv("OUTPUT_FORMAT", "mp3").lower()
if OUTPUT_FORMAT not in {"mp3", "wav"}:
    OUTPUT_FORMAT = "mp3"
MP3_BITRATE = os.getenv("MP3_BITRATE", "192")

# Demucs is CPU/RAM heavy: keep concurrency low and the waiting line short.
MAX_CONCURRENT_JOBS = max(1, int(os.getenv("MAX_CONCURRENT_JOBS", "1")))
MAX_QUEUED_JOBS = max(0, int(os.getenv("MAX_QUEUED_JOBS", "5")))
RATE_LIMIT_PER_HOUR = int(os.getenv("RATE_LIMIT_PER_HOUR", "10"))  # 0 disables
RESULTS_TTL_MINUTES = int(os.getenv("RESULTS_TTL_MINUTES", "120"))

ALLOWED_SUFFIXES = {".mp3", ".wav", ".ogg", ".flac", ".m4a", ".aac"}
JOB_ID_RE = re.compile(r"^[0-9a-f]{32}$")
STEM_RE = re.compile(r"^(vocals|instrumental)\.(mp3|wav)$")
PROGRESS_RE = re.compile(r"(\d{1,3})%\|")
MEDIA_TYPES = {".mp3": "audio/mpeg", ".wav": "audio/wav"}

# In-memory job table (single uvicorn worker). Finished stems live on disk, so
# a finished job can still be recovered after a restart.
JOBS: dict[str, dict[str, Any]] = {}
JOBS_LOCK = threading.Lock()
EXECUTOR = ThreadPoolExecutor(max_workers=MAX_CONCURRENT_JOBS, thread_name_prefix="demucs")
_RATE_HITS: dict[str, deque] = defaultdict(deque)


def set_job(job_id: str, **fields: Any) -> None:
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if job is not None:
            job.update(fields)
            job["updated_at"] = time.time()


def active_job_count() -> int:
    with JOBS_LOCK:
        return sum(1 for j in JOBS.values() if j["status"] in {"queued", "processing"})


def queue_position(job_id: str) -> int:
    with JOBS_LOCK:
        queued = sorted(
            (j["created_at"], jid) for jid, j in JOBS.items() if j["status"] == "queued"
        )
    for index, (_, jid) in enumerate(queued, start=1):
        if jid == job_id:
            return index
    return 0


def find_stems(job_dir: Path) -> dict[str, Path]:
    found: dict[str, Path] = {}
    for stem in ("vocals", "instrumental"):
        for ext in (".mp3", ".wav"):
            candidate = job_dir / f"{stem}{ext}"
            if candidate.exists():
                found[stem] = candidate
                break
    return found


def run_demucs(job_id: str, input_path: Path, output_dir: Path) -> tuple[Path, Path]:
    """Run Demucs in two-stem mode, streaming its progress into the job record."""
    command = [
        sys.executable,
        "-m",
        "demucs",
        "--two-stems=vocals",
        "-n",
        DEMUCS_MODEL,
        "-o",
        str(output_dir),
    ]
    if OUTPUT_FORMAT == "mp3":
        command += ["--mp3", f"--mp3-bitrate={MP3_BITRATE}"]
    command.append(str(input_path))

    proc = subprocess.Popen(
        command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, bufsize=0
    )
    timed_out = threading.Event()

    def _kill() -> None:
        timed_out.set()
        proc.kill()

    watchdog = threading.Timer(DEMUCS_TIMEOUT_SECONDS, _kill)
    watchdog.start()

    tail: deque[str] = deque(maxlen=30)
    buffer = b""
    try:
        assert proc.stdout is not None
        while True:
            chunk = proc.stdout.read(256)
            if not chunk:
                break
            buffer += chunk
            parts = re.split(rb"[\r\n]", buffer)
            buffer = parts.pop()
            if len(buffer) > 4096:
                buffer = buffer[-1024:]
            for raw in parts:
                line = raw.decode("utf-8", "replace").strip()
                if not line:
                    continue
                tail.append(line)
                match = PROGRESS_RE.search(line)
                if match:
                    pct = min(100, int(match.group(1)))
                    # 10..95 while Demucs runs; the last 5% is copying/cleanup.
                    set_job(job_id, progress=10 + int(pct * 0.85))
        proc.wait()
    finally:
        watchdog.cancel()

    if timed_out.is_set():
        raise RuntimeError(f"Separation timed out after {DEMUCS_TIMEOUT_SECONDS}s")
    if proc.returncode != 0:
        log.error("Demucs failed for job %s:\n%s", job_id, "\n".join(tail))
        raise RuntimeError(tail[-1][:200] if tail else "Demucs failed")

    # Demucs writes: <output>/<model>/<track-name>/{vocals,no_vocals}.<ext>
    vocals = next(
        (p for ext in (".mp3", ".wav") for p in output_dir.rglob(f"vocals{ext}")), None
    )
    if vocals is None:
        raise RuntimeError("Demucs completed but the vocals stem was not produced")
    instrumental = next(
        (
            vocals.parent / name
            for name in (f"no_vocals{vocals.suffix}", f"accompaniment{vocals.suffix}")
            if (vocals.parent / name).exists()
        ),
        None,
    )
    if instrumental is None:
        raise RuntimeError("Demucs completed but the instrumental stem was not produced")
    return vocals, instrumental


def process_job(job_id: str, job_dir: Path, input_path: Path) -> None:
    work_dir = job_dir / "work"
    try:
        set_job(job_id, status="processing", stage="separating", progress=10)
        vocals, instrumental = run_demucs(job_id, input_path, work_dir / "separated")
        ext = vocals.suffix
        shutil.copy2(vocals, job_dir / f"vocals{ext}")
        shutil.copy2(instrumental, job_dir / f"instrumental{ext}")
        shutil.rmtree(work_dir, ignore_errors=True)
        set_job(job_id, status="done", stage="done", progress=100, format=ext.lstrip("."))
    except Exception as exc:  # noqa: BLE001 - report any failure to the client
        log.exception("Job %s failed", job_id)
        shutil.rmtree(job_dir, ignore_errors=True)
        set_job(job_id, status="error", stage="error", error=str(exc)[:300])


def client_ip(request: Request) -> str:
    # Behind a single reverse proxy (Coolify/Traefik) the real client is the
    # last X-Forwarded-For entry; earlier entries can be forged by the caller.
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[-1].strip()
    return request.client.host if request.client else "unknown"


def check_rate_limit(ip: str) -> None:
    if RATE_LIMIT_PER_HOUR <= 0:
        return
    now = time.time()
    hits = _RATE_HITS[ip]
    while hits and now - hits[0] > 3600:
        hits.popleft()
    if len(hits) >= RATE_LIMIT_PER_HOUR:
        raise HTTPException(
            status_code=429,
            detail=f"Rate limit reached ({RATE_LIMIT_PER_HOUR} separations per hour). Try again later.",
        )
    hits.append(now)


def cleanup_stale_results() -> None:
    cutoff = time.time() - RESULTS_TTL_MINUTES * 60
    with JOBS_LOCK:
        busy = {jid for jid, j in JOBS.items() if j["status"] in {"queued", "processing"}}
    for entry in RESULTS_ROOT.iterdir():
        if not entry.is_dir() or entry.name in busy:
            continue
        try:
            if entry.stat().st_mtime < cutoff:
                shutil.rmtree(entry, ignore_errors=True)
                with JOBS_LOCK:
                    JOBS.pop(entry.name, None)
        except OSError:
            continue


async def cleanup_loop() -> None:
    while True:
        try:
            await asyncio.to_thread(cleanup_stale_results)
        except Exception:  # noqa: BLE001
            log.exception("Result cleanup failed")
        await asyncio.sleep(600)


@asynccontextmanager
async def lifespan(_: FastAPI):
    # Anything left without final stems is debris from an interrupted job.
    for entry in RESULTS_ROOT.iterdir():
        if entry.is_dir() and not find_stems(entry):
            shutil.rmtree(entry, ignore_errors=True)
    task = asyncio.create_task(cleanup_loop())
    try:
        yield
    finally:
        task.cancel()
        EXECUTOR.shutdown(wait=False, cancel_futures=True)


app = FastAPI(title="Karaoke Mixer Separation API", version="2.0.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=False,
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)


def build_url(path: str) -> str:
    base_url = str(os.getenv("PUBLIC_BASE_URL", "")).rstrip("/")
    return f"{base_url}{path}" if base_url else path


def public_job(job_id: str, job: dict[str, Any]) -> dict[str, Any]:
    data: dict[str, Any] = {
        "job_id": job_id,
        "status": job["status"],
        "stage": job.get("stage", job["status"]),
        "progress": job.get("progress", 0),
    }
    if job["status"] == "queued":
        data["queue_position"] = queue_position(job_id)
    if job["status"] == "error":
        data["error"] = job.get("error", "Separation failed")
    if job["status"] == "done":
        ext = job.get("format", OUTPUT_FORMAT)
        data["format"] = ext
        data["vocals"] = build_url(f"/api/results/{job_id}/vocals.{ext}")
        data["instrumental"] = build_url(f"/api/results/{job_id}/instrumental.{ext}")
    return data


@app.get("/health")
def health() -> dict[str, Any]:
    return {
        "ok": True,
        "service": "karaoke-mixer-separation",
        "demucs_model": DEMUCS_MODEL,
        "output_format": OUTPUT_FORMAT,
        "active_jobs": active_job_count(),
    }


@app.post("/api/separate", status_code=202)
async def separate(
    request: Request,
    audio: UploadFile | None = File(None),
    file: UploadFile | None = File(None),  # accepted for older clients/docs
) -> dict[str, Any]:
    upload = audio or file
    if upload is None or not upload.filename:
        raise HTTPException(status_code=400, detail="Missing audio file (form field 'audio')")

    suffix = Path(upload.filename).suffix.lower()
    if suffix not in ALLOWED_SUFFIXES:
        await upload.close()
        raise HTTPException(status_code=400, detail=f"Unsupported audio format: {suffix}")

    if active_job_count() >= MAX_CONCURRENT_JOBS + MAX_QUEUED_JOBS:
        await upload.close()
        raise HTTPException(
            status_code=503, detail="Separation queue is full. Please try again in a few minutes."
        )
    check_rate_limit(client_ip(request))

    job_id = uuid.uuid4().hex
    job_dir = RESULTS_ROOT / job_id
    work_dir = job_dir / "work"
    work_dir.mkdir(parents=True, exist_ok=True)
    input_path = work_dir / f"input{suffix}"

    try:
        size = 0
        with input_path.open("wb") as out:
            while True:
                chunk = await upload.read(1024 * 1024)
                if not chunk:
                    break
                size += len(chunk)
                if size > MAX_UPLOAD_BYTES:
                    raise HTTPException(
                        status_code=413, detail=f"File is larger than {MAX_UPLOAD_MB} MB"
                    )
                out.write(chunk)
    except HTTPException:
        shutil.rmtree(job_dir, ignore_errors=True)
        raise
    except Exception as exc:  # noqa: BLE001
        shutil.rmtree(job_dir, ignore_errors=True)
        raise HTTPException(status_code=500, detail=f"Upload failed: {exc}") from exc
    finally:
        await upload.close()

    now = time.time()
    with JOBS_LOCK:
        JOBS[job_id] = {
            "status": "queued",
            "stage": "queued",
            "progress": 0,
            "created_at": now,
            "updated_at": now,
        }
    EXECUTOR.submit(process_job, job_id, job_dir, input_path)

    return {
        "job_id": job_id,
        "status": "queued",
        "status_url": build_url(f"/api/jobs/{job_id}"),
    }


@app.get("/api/jobs/{job_id}")
def job_status(job_id: str) -> dict[str, Any]:
    if not JOB_ID_RE.match(job_id):
        raise HTTPException(status_code=404, detail="Unknown job")

    with JOBS_LOCK:
        job = dict(JOBS[job_id]) if job_id in JOBS else None
    if job is not None:
        return public_job(job_id, job)

    # Server restarted since the job ran: recover from the files on disk.
    stems = find_stems(RESULTS_ROOT / job_id)
    if "vocals" in stems and "instrumental" in stems:
        recovered = {
            "status": "done",
            "stage": "done",
            "progress": 100,
            "format": stems["vocals"].suffix.lstrip("."),
        }
        return public_job(job_id, recovered)
    raise HTTPException(status_code=404, detail="Job not found (it may have expired)")


@app.get("/api/results/{job_id}/{stem_name}")
def result_file(job_id: str, stem_name: str):
    if not JOB_ID_RE.match(job_id) or not STEM_RE.match(stem_name):
        raise HTTPException(status_code=404, detail="Unknown stem")

    path = RESULTS_ROOT / job_id / stem_name
    if not path.exists():
        raise HTTPException(status_code=404, detail="Stem not found")

    return FileResponse(
        path,
        media_type=MEDIA_TYPES[path.suffix],
        filename=stem_name,
        headers={"Cache-Control": "private, max-age=3600"},
    )
