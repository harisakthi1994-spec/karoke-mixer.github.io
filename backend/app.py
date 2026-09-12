from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import uuid
from pathlib import Path
from typing import Any

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse

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

app = FastAPI(title="Karaoke Mixer Separation API", version="1.0.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=False,
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)


def run_demucs(input_path: Path, output_dir: Path) -> tuple[Path, Path]:
    """Run Demucs in two-stem mode and return vocals + instrumental paths."""
    command = [
        "python",
        "-m",
        "demucs",
        "--two-stems=vocals",
        "-n",
        os.getenv("DEMUCS_MODEL", "htdemucs"),
        "-o",
        str(output_dir),
        str(input_path),
    ]

    completed = subprocess.run(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        timeout=int(os.getenv("DEMUCS_TIMEOUT_SECONDS", "1800")),
    )

    if completed.returncode != 0:
        raise RuntimeError(completed.stdout[-12000:] or "Demucs failed")

    # Demucs typically writes: <output>/<model>/<track-name>/{vocals,no_vocals}.wav
    candidates = list(output_dir.rglob("vocals.wav"))
    if not candidates:
        raise RuntimeError("Demucs completed but vocals.wav was not produced")

    vocals = candidates[0]
    no_vocals = vocals.parent / "no_vocals.wav"
    if not no_vocals.exists():
        # Some Demucs builds use accompaniment.wav; accept that too.
        accompaniment = vocals.parent / "accompaniment.wav"
        if accompaniment.exists():
            no_vocals = accompaniment
        else:
            raise RuntimeError("Demucs completed but instrumental stem was not produced")

    return vocals, no_vocals


@app.get("/health")
def health() -> dict[str, Any]:
    return {
        "ok": True,
        "service": "karaoke-mixer-separation",
        "demucs_model": os.getenv("DEMUCS_MODEL", "htdemucs"),
    }


@app.post("/api/separate")
async def separate(audio: UploadFile = File(...)) -> dict[str, Any]:
    if not audio.filename:
        raise HTTPException(status_code=400, detail="Missing file name")

    suffix = Path(audio.filename).suffix.lower()
    allowed = {".mp3", ".wav", ".ogg", ".flac", ".m4a", ".aac"}
    if suffix not in allowed:
        raise HTTPException(status_code=400, detail=f"Unsupported audio format: {suffix}")

    job_id = uuid.uuid4().hex
    job_dir = RESULTS_ROOT / job_id
    work_dir = job_dir / "work"
    work_dir.mkdir(parents=True, exist_ok=True)

    input_path = work_dir / f"input{suffix}"

    try:
        size = 0
        with input_path.open("wb") as out:
            while True:
                chunk = await audio.read(1024 * 1024)
                if not chunk:
                    break
                size += len(chunk)
                if size > MAX_UPLOAD_BYTES:
                    raise HTTPException(
                        status_code=413,
                        detail=f"File is larger than {MAX_UPLOAD_MB} MB",
                    )
                out.write(chunk)

        vocals, instrumental = run_demucs(input_path, work_dir / "separated")

        final_vocals = job_dir / "vocals.wav"
        final_instrumental = job_dir / "instrumental.wav"
        shutil.copy2(vocals, final_vocals)
        shutil.copy2(instrumental, final_instrumental)

        # Remove temporary input + model-specific nested output tree to reduce disk use.
        shutil.rmtree(work_dir, ignore_errors=True)

        base_url = str(os.getenv("PUBLIC_BASE_URL", "")).rstrip("/")
        return {
            "job_id": job_id,
            "vocals": f"{base_url}/api/results/{job_id}/vocals.wav" if base_url else f"/api/results/{job_id}/vocals.wav",
            "instrumental": f"{base_url}/api/results/{job_id}/instrumental.wav" if base_url else f"/api/results/{job_id}/instrumental.wav",
        }
    except HTTPException:
        shutil.rmtree(job_dir, ignore_errors=True)
        raise
    except Exception as exc:
        shutil.rmtree(job_dir, ignore_errors=True)
        raise HTTPException(status_code=500, detail=f"Stem separation failed: {exc}") from exc
    finally:
        await audio.close()


@app.get("/api/results/{job_id}/{stem_name}")
def result_file(job_id: str, stem_name: str):
    if stem_name not in {"vocals.wav", "instrumental.wav"}:
        raise HTTPException(status_code=404, detail="Unknown stem")

    path = RESULTS_ROOT / job_id / stem_name
    if not path.exists():
        raise HTTPException(status_code=404, detail="Stem not found")

    return FileResponse(path, media_type="audio/wav", filename=stem_name)


@app.on_event("startup")
def cleanup_stale_results() -> None:
    # Keep the implementation simple for V1. A production deployment can replace
    # this with a scheduled cleanup job or object-storage lifecycle policy.
    return None
