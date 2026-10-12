from __future__ import annotations

import asyncio
import json
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

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, Response

log = logging.getLogger("uvicorn.error")

APP_ROOT = Path(__file__).resolve().parent
RESULTS_ROOT = Path(os.getenv("RESULTS_ROOT", APP_ROOT / "results"))
RESULTS_ROOT.mkdir(parents=True, exist_ok=True)
CACHE_ROOT = RESULTS_ROOT / "cache"
CACHE_ROOT.mkdir(parents=True, exist_ok=True)

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
CACHE_TTL_DAYS = int(os.getenv("CACHE_TTL_DAYS", "30"))
CACHE_MAX_GB = float(os.getenv("CACHE_MAX_GB", "5"))

ALLOWED_SUFFIXES = {".mp3", ".wav", ".ogg", ".flac", ".m4a", ".aac"}
JOB_ID_RE = re.compile(r"^[0-9a-f]{32}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
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


def save_to_cache(
    sha256: str,
    vocals_path: Path,
    instrumental_path: Path,
    name: str = "",
    size: int = 0,
) -> None:
    cache_dir = CACHE_ROOT / sha256
    try:
        cache_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(vocals_path, cache_dir / vocals_path.name)
        shutil.copy2(instrumental_path, cache_dir / instrumental_path.name)
        (cache_dir / "meta.json").write_text(
            json.dumps({"name": name, "size": size, "created": time.time()})
        )
        cache_dir.touch()
    except Exception:
        log.exception("Failed to save stems to cache for %s", sha256)
        shutil.rmtree(cache_dir, ignore_errors=True)


def process_job(
    job_id: str,
    job_dir: Path,
    input_path: Path,
    sha256: str | None = None,
    name: str = "",
    size: int = 0,
) -> None:
    work_dir = job_dir / "work"
    try:
        set_job(job_id, status="processing", stage="separating", progress=10)
        vocals, instrumental = run_demucs(job_id, input_path, work_dir / "separated")
        ext = vocals.suffix
        dest_vocals = job_dir / f"vocals{ext}"
        dest_inst = job_dir / f"instrumental{ext}"
        shutil.copy2(vocals, dest_vocals)
        shutil.copy2(instrumental, dest_inst)
        shutil.rmtree(work_dir, ignore_errors=True)
        if sha256:
            save_to_cache(sha256, dest_vocals, dest_inst, name=name, size=size)
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
        if not entry.is_dir() or entry.name in busy or entry.name == "cache":
            continue
        try:
            if entry.stat().st_mtime < cutoff:
                shutil.rmtree(entry, ignore_errors=True)
                with JOBS_LOCK:
                    JOBS.pop(entry.name, None)
        except OSError:
            continue


def cleanup_cache() -> None:
    if not CACHE_ROOT.exists():
        return
    cutoff = time.time() - CACHE_TTL_DAYS * 86400
    max_bytes = int(CACHE_MAX_GB * 1024 ** 3)
    entries = []
    for entry in CACHE_ROOT.iterdir():
        if not entry.is_dir():
            continue
        try:
            stat = entry.stat()
            if stat.st_mtime < cutoff:
                shutil.rmtree(entry, ignore_errors=True)
                continue
            size = sum(f.stat().st_size for f in entry.iterdir() if f.is_file())
            entries.append((stat.st_mtime, size, entry))
        except OSError:
            continue
    # oldest-first eviction to stay under CACHE_MAX_GB
    entries.sort()
    total = sum(s for _, s, _ in entries)
    for _mtime, size, entry in entries:
        if total <= max_bytes:
            break
        shutil.rmtree(entry, ignore_errors=True)
        total -= size


async def cleanup_loop() -> None:
    while True:
        try:
            await asyncio.to_thread(cleanup_stale_results)
            await asyncio.to_thread(cleanup_cache)
        except Exception:  # noqa: BLE001
            log.exception("Result cleanup failed")
        await asyncio.sleep(600)


@asynccontextmanager
async def lifespan(_: FastAPI):
    # Anything left without final stems is debris from an interrupted job.
    for entry in RESULTS_ROOT.iterdir():
        if entry.is_dir() and entry.name != "cache" and not find_stems(entry):
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
    allow_methods=["GET", "POST", "DELETE"],
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


@app.get("/api/cache/{sha256}")
def cache_lookup(sha256: str, name: str = "", size: int = 0) -> dict[str, Any]:
    if not SHA256_RE.match(sha256):
        raise HTTPException(status_code=400, detail="Invalid sha256")
    cache_dir = CACHE_ROOT / sha256
    stems = find_stems(cache_dir)
    if "vocals" not in stems or "instrumental" not in stems:
        return {"hit": False, "job_id": None, "vocals": None, "instrumental": None}
    try:
        cache_dir.touch()  # refresh mtime so LRU eviction keeps hot entries
    except OSError:
        pass
    meta_path = cache_dir / "meta.json"
    clean = re.sub(r"[\x00-\x1f\x7f]", "", name).strip()[:200]
    if clean and size > 0:
        # Backfill meta for entries cached before the library feature, or fix blank ones.
        old: dict[str, Any] = {}
        if meta_path.exists():
            try:
                old = json.loads(meta_path.read_text())
            except Exception:
                old = {}
        if not old.get("name") or not old.get("size"):
            try:
                meta_path.write_text(
                    json.dumps({"name": clean, "size": size, "created": old.get("created") or time.time()})
                )
            except OSError:
                log.exception("Failed to backfill meta for %s", sha256)
    ext = stems["vocals"].suffix.lstrip(".")
    return {
        "hit": True,
        "job_id": sha256,
        "vocals": build_url(f"/api/cache/{sha256}/vocals.{ext}"),
        "instrumental": build_url(f"/api/cache/{sha256}/instrumental.{ext}"),
    }


@app.get("/api/cache/{sha256}/{stem_name}")
def cache_file(sha256: str, stem_name: str):
    if not SHA256_RE.match(sha256) or not STEM_RE.match(stem_name):
        raise HTTPException(status_code=404, detail="Unknown stem")
    path = CACHE_ROOT / sha256 / stem_name
    if not path.exists():
        raise HTTPException(status_code=404, detail="Stem not found")
    try:
        (CACHE_ROOT / sha256).touch()
    except OSError:
        pass
    return FileResponse(
        path,
        media_type=MEDIA_TYPES[path.suffix],
        filename=stem_name,
        headers={"Cache-Control": "private, max-age=86400"},
    )


@app.get("/api/library")
def library_list() -> list[dict]:
    songs = []
    for entry in CACHE_ROOT.iterdir():
        if not entry.is_dir() or not SHA256_RE.match(entry.name):
            continue
        meta_path = entry / "meta.json"
        stems = find_stems(entry)
        if "vocals" not in stems or "instrumental" not in stems:
            continue
        try:
            meta = json.loads(meta_path.read_text()) if meta_path.exists() else {}
            name = meta.get("name") or ""
            size = meta.get("size") or 0
            if not name.strip():
                name = f"Unnamed song ({entry.name[:8]})"
            if not size:
                size = sum(p.stat().st_size for p in stems.values())
            songs.append(
                {
                    "sha256": entry.name,
                    "name": name,
                    "size": size,
                    "created": meta.get("created") or entry.stat().st_mtime,
                }
            )
        except Exception:
            continue
    songs.sort(key=lambda x: x["created"], reverse=True)
    return songs


@app.delete("/api/cache/{sha256}", status_code=204, response_class=Response, response_model=None)
def cache_delete(sha256: str) -> Response:
    if not SHA256_RE.match(sha256):
        raise HTTPException(status_code=400, detail="Invalid sha256")
    shutil.rmtree(CACHE_ROOT / sha256, ignore_errors=True)
    return Response(status_code=204)


@app.post("/api/separate", status_code=202)
async def separate(
    request: Request,
    audio: UploadFile | None = File(None),
    file: UploadFile | None = File(None),  # accepted for older clients/docs
    sha256: str | None = Form(None),
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

    # Silently discard an invalid sha256 rather than failing the upload.
    if sha256 is not None and not SHA256_RE.match(sha256):
        sha256 = None

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
    EXECUTOR.submit(process_job, job_id, job_dir, input_path, sha256, upload.filename or "", size)

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


# ---- Sync rooms (in-memory, polled) ----
ROOMS: dict[str, dict[str, Any]] = {}
ROOMS_LOCK = threading.Lock()
ROOM_TTL_SECONDS = 6 * 3600
ROOM_MAX = 100
DEVICE_WINDOW_SECONDS = 6
MIX_MODES = {"device", "same"}


def now_ms() -> int:
    return int(time.time() * 1000)


def purge_rooms() -> None:
    # Caller holds ROOMS_LOCK.
    cutoff = time.time() - ROOM_TTL_SECONDS
    for code in [c for c, r in ROOMS.items() if r["touched"] < cutoff]:
        del ROOMS[code]


def room_state(room: dict[str, Any]) -> dict[str, Any]:
    return {k: room[k] for k in ("song", "name", "playing", "position", "start_at", "mix_mode", "version")}


@app.get("/api/time")
def server_time() -> dict[str, int]:
    return {"t": now_ms()}


@app.post("/api/rooms")
def room_create() -> dict[str, Any]:
    import random
    import string

    with ROOMS_LOCK:
        purge_rooms()
        if len(ROOMS) >= ROOM_MAX:
            raise HTTPException(status_code=503, detail="Too many active rooms")
        code = "".join(random.choices(string.ascii_uppercase, k=4))
        while code in ROOMS:
            code = "".join(random.choices(string.ascii_uppercase, k=4))
        ROOMS[code] = {
            "song": None,
            "name": "",
            "playing": False,
            "position": 0.0,
            "start_at": now_ms(),
            "mix_mode": "device",
            "version": 0,
            "touched": time.time(),
            "seen": {},
        }
        return {"code": code, "state": room_state(ROOMS[code])}


@app.get("/api/rooms/{code}")
def room_get(code: str, id: str = "") -> dict[str, Any]:
    with ROOMS_LOCK:
        purge_rooms()
        room = ROOMS.get(code.upper())
        if room is None:
            raise HTTPException(status_code=404, detail="Room not found")
        now = time.time()
        room["touched"] = now
        if id:
            room["seen"][id[:64]] = now
        room["seen"] = {d: t for d, t in room["seen"].items() if now - t <= DEVICE_WINDOW_SECONDS}
        return {
            "code": code.upper(),
            "state": room_state(room),
            "server_now": now_ms(),
            "devices": len(room["seen"]),
        }


@app.post("/api/rooms/{code}/state")
async def room_set_state(code: str, request: Request) -> dict[str, Any]:
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        raise HTTPException(status_code=400, detail="Invalid JSON")
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="Invalid body")

    song = body.get("song")
    if song is not None and not (isinstance(song, str) and SHA256_RE.match(song)):
        raise HTTPException(status_code=400, detail="Invalid song")
    name = body.get("name", "")
    if not isinstance(name, str):
        raise HTTPException(status_code=400, detail="Invalid name")
    playing = body.get("playing")
    if not isinstance(playing, bool):
        raise HTTPException(status_code=400, detail="Invalid playing")
    position = body.get("position")
    if isinstance(position, bool) or not isinstance(position, (int, float)) or not position >= 0:
        raise HTTPException(status_code=400, detail="Invalid position")
    start_at = body.get("start_at")
    if isinstance(start_at, bool) or not isinstance(start_at, (int, float)):
        raise HTTPException(status_code=400, detail="Invalid start_at")
    mix_mode = body.get("mix_mode")
    if mix_mode not in MIX_MODES:
        raise HTTPException(status_code=400, detail="Invalid mix_mode")

    with ROOMS_LOCK:
        purge_rooms()
        room = ROOMS.get(code.upper())
        if room is None:
            raise HTTPException(status_code=404, detail="Room not found")
        room.update(
            song=song,
            name=name[:200],
            playing=playing,
            position=float(position),
            start_at=int(start_at),
            mix_mode=mix_mode,
        )
        room["version"] += 1
        room["touched"] = time.time()
        return room_state(room)
