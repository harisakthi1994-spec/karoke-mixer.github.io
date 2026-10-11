# 🎤 Karaoke Mixer

A browser-based karaoke audio mixer with a GitHub Pages frontend and a Demucs-powered FastAPI backend for real vocal/instrumental separation.

## What this project does

- Upload an audio file (MP3, WAV, OGG, FLAC).
- Display a live frequency visualiser during playback.
- Send the audio to the backend for real vocal/instrumental separation via an async job queue.
- Load separate **Vocals** and **Instrumental** stems into the browser mixer.
- Adjust vocal and instrumental volume independently.
- Change pitch and tempo during playback (pitch and tempo are coupled — adjusting one affects the other due to a Web Audio API limitation).
- Select an audio output device; on Chrome/Edge/Brave, split output between two devices (e.g. instrumental to a room speaker, vocals + instrumental to headphones) with per-device delay sliders for Bluetooth sync.
- Enter and preview lyrics.

## Architecture

```text
GitHub Pages frontend
        │
        │ POST /api/separate  →  202 Accepted + job_id
        │ GET  /api/jobs/{id} →  poll for status/progress
        │ GET  /api/results/{job_id}/{vocals|instrumental}.mp3
        ▼
Coolify backend
FastAPI + Demucs (CPU-only PyTorch)
```

## Repository structure

```text
/
├── index.html                         # Production frontend / GitHub Pages entry
├── karaoke-mixer-prototype.html       # Prototype/reference frontend
├── README.md
└── backend/
    ├── app.py                         # FastAPI API + async job queue + Demucs
    ├── Dockerfile                     # CPU-only PyTorch build
    ├── docker-compose.yml             # Local/Coolify service definition
    ├── requirements.txt               # Python dependencies (torch installed separately)
    ├── .env.example                   # Example configuration
    └── results/                       # Runtime-generated stems (auto-cleaned)
```

## Job flow

### 1. Submit a separation job

```http
POST /api/separate
Content-Type: multipart/form-data

audio=<audio file>
```

Returns **202 Accepted**:

```json
{
  "job_id": "abc123...",
  "status": "queued",
  "status_url": "https://YOUR-API-DOMAIN/api/jobs/abc123..."
}
```

### 2. Poll for status

```http
GET /api/jobs/{job_id}
```

While processing:

```json
{
  "job_id": "abc123...",
  "status": "processing",
  "stage": "separating",
  "progress": 42,
  "queue_position": 1
}
```

When done:

```json
{
  "job_id": "abc123...",
  "status": "done",
  "stage": "done",
  "progress": 100,
  "format": "mp3",
  "vocals": "https://YOUR-API-DOMAIN/api/results/abc123.../vocals.mp3",
  "instrumental": "https://YOUR-API-DOMAIN/api/results/abc123.../instrumental.mp3"
}
```

### 3. Download stems

```http
GET /api/results/{job_id}/vocals.mp3
GET /api/results/{job_id}/instrumental.mp3
```

### Health check

```http
GET /health
```

```json
{
  "ok": true,
  "service": "karaoke-mixer-separation",
  "demucs_model": "htdemucs",
  "output_format": "mp3",
  "active_jobs": 0
}
```

## Environment variables

All variables have defaults; copy `backend/.env.example` and adjust for production.

| Variable | Default | Description |
|---|---|---|
| `ALLOWED_ORIGINS` | `https://harisakthi1994-spec.github.io` | Comma-separated CORS origins |
| `PUBLIC_BASE_URL` | _(empty)_ | Backend base URL, e.g. `https://karaoke-api.example.com` |
| `DEMUCS_MODEL` | `htdemucs` | Demucs model name |
| `OUTPUT_FORMAT` | `mp3` | Stem format: `mp3` or `wav` |
| `MP3_BITRATE` | `192` | MP3 bitrate in kbps |
| `MAX_UPLOAD_MB` | `150` | Maximum upload size in MB |
| `DEMUCS_TIMEOUT_SECONDS` | `1800` | Per-job timeout |
| `MAX_CONCURRENT_JOBS` | `1` | Parallel Demucs workers |
| `MAX_QUEUED_JOBS` | `5` | Max jobs waiting in queue |
| `RATE_LIMIT_PER_HOUR` | `10` | Separations per IP per hour (0 = disabled) |
| `RESULTS_TTL_MINUTES` | `120` | How long stems are kept on disk |
| `RESULTS_ROOT` | `/app/results` | Stem output directory |

## Frontend

`index.html` is a static HTML/CSS/JavaScript application served from GitHub Pages.

The browser reads the separation API URL from:

```javascript
window.SEPARATION_API_URL  // set in a <script> tag in index.html
```

or falls back to:

```javascript
localStorage.getItem('SEPARATION_API_URL')
```

For a quick browser test without redeploying:

```javascript
localStorage.setItem('SEPARATION_API_URL', 'https://YOUR-API-DOMAIN/api/separate');
location.reload();
```

If the backend is unavailable or separation fails, the frontend falls back to the original audio so playback never completely breaks.

### Split output (Chrome/Edge/Brave only)

After separation, a **Split outputs** toggle appears in the Audio Output Device card. When enabled:

- **Output A** receives the instrumental stem only (e.g. a room speaker).
- **Output B** receives vocals + instrumental (e.g. the singer's headphones).

Each output has a delay slider (0–500 ms) to compensate for Bluetooth latency (~150–300 ms typical).

### Pitch and tempo

Pitch and tempo sliders are coupled: changing pitch also shifts the playback speed, and changing tempo also shifts the pitch. This is a Web Audio API limitation — true independent control requires a more complex DSP pipeline.

## Local development

```bash
cd backend
docker compose up --build
```

API: `http://localhost:5000` (host port mapped in `docker-compose.yml`)

Health: `http://localhost:5000/health`

### Python directly

```bash
cd backend
pip install torch torchaudio --index-url https://download.pytorch.org/whl/cpu
pip install -r requirements.txt
uvicorn app:app --host 0.0.0.0 --port 8000
```

FFmpeg must be installed for common audio input formats.

## Coolify deployment

```text
Repository:    https://github.com/harisakthi1994-spec/karoke-mixer.github.io.git
Base Directory: /backend
Compose File:  docker-compose.yml
Service:       karaoke-api
Container Port: 8000
```

Set `ALLOWED_ORIGINS`, `PUBLIC_BASE_URL`, and any tuning variables in Coolify Environment Variables.

## First deployment expectations

- The Docker build installs FFmpeg and the full Demucs/PyTorch (CPU) stack — expect several minutes.
- The first separation also downloads the Demucs model weights (~200 MB); subsequent jobs are faster.
- CPU-only inference is slow (~5–15 min for a 3-minute song on a modest VPS). A GPU server is preferred for a responsive production experience.

## Troubleshooting

| Symptom | Check |
|---|---|
| `SEPARATION_API_URL` not configured | `window.SEPARATION_API_URL` in `index.html` or localStorage |
| HTTP 404 on `/api/separate` | Coolify backend running; domain → port 8000 |
| CORS error | `ALLOWED_ORIGINS` contains the exact frontend origin |
| Job returns `status: error` | Coolify runtime logs; likely OOM or FFmpeg missing |
| Stems out of sync in split mode | Increase delay on the non-Bluetooth output |
| Health check fails | `GET https://YOUR-API-DOMAIN/health` should return `{"ok":true,...}` |

## Important implementation rules

- Do not reintroduce browser-side Demucs. The project uses the real backend for inference.
- Keep production credentials in Coolify, not in Git.
- Results are auto-cleaned after `RESULTS_TTL_MINUTES`; do not rely on them persisting indefinitely.
