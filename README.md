# 🎤 Karaoke Mixer

A browser-based karaoke audio mixer with a GitHub Pages frontend and a Demucs-powered FastAPI backend for real vocal/instrumental separation.

## What this project does

- Upload an audio file.
- Display a playback waveform.
- Send the audio to the backend for real vocal/instrumental separation.
- Load separate **Vocals** and **Instrumental** stems into the browser mixer.
- Adjust vocal and instrumental volume independently.
- Change pitch and tempo during playback.
- Select an audio output device when supported by the browser.
- Enter and preview lyrics.

## Architecture

```text
GitHub Pages frontend
        │
        │ POST /api/separate
        ▼
Coolify backend
FastAPI + Demucs
        │
        ├── vocals.wav
        └── instrumental.wav
        │
        ▼
Browser AudioBuffers
        │
        ▼
Karaoke Mixer
```

## Repository structure

```text
/
├── index.html                         # Production frontend / GitHub Pages entry
├── karaoke-mixer-prototype.html       # Prototype/reference frontend
├── README.md                          # Newcomer/project guide
└── backend/
    ├── app.py                         # FastAPI API + Demucs execution
    ├── Dockerfile                     # Backend container image
    ├── docker-compose.yml              # Local/Coolify service definition
    ├── requirements.txt                # Python dependencies
    ├── .env.example                    # Example configuration
    └── results/                        # Runtime-generated stems
```

## Frontend

`index.html` is a static HTML/CSS/JavaScript application intended for GitHub Pages.

The browser does **not** run Demucs. When a separation API is configured, the browser uploads the original audio to:

```text
https://YOUR-API-DOMAIN/api/separate
```

The frontend reads the API URL from either:

```javascript
window.SEPARATION_API_URL
```

or:

```javascript
localStorage.getItem('SEPARATION_API_URL')
```

For a quick browser test:

```javascript
localStorage.setItem(
  'SEPARATION_API_URL',
  'https://YOUR-API-DOMAIN/api/separate'
);
location.reload();
```

If the backend is unavailable, the frontend keeps the original audio available as a fallback so playback does not completely fail.

## Backend

The backend is a FastAPI service that accepts an uploaded audio file, runs Demucs in two-stem mode, and returns URLs for the generated stems.

Main file:

```text
backend/app.py
```

### Health check

```http
GET /health
```

Example:

```json
{
  "ok": true,
  "service": "karaoke-mixer-separation",
  "demucs_model": "htdemucs"
}
```

### Separation API

```http
POST /api/separate
Content-Type: multipart/form-data
```

Form field:

```text
file=<audio file>
```

Response:

```json
{
  "job_id": "...",
  "vocals": "https://YOUR-API-DOMAIN/api/results/<job_id>/vocals.wav",
  "instrumental": "https://YOUR-API-DOMAIN/api/results/<job_id>/instrumental.wav"
}
```

## How Demucs is used

The backend runs Demucs in two-stem mode:

```text
--two-stems=vocals
```

Demucs produces a vocal stem and a non-vocal stem. The API exposes those to the frontend as:

```text
vocals.wav
instrumental.wav
```

## Local development

From the repository root:

```bash
cd backend
```

### Docker Compose

```bash
docker compose up --build
```

The API will normally listen on:

```text
http://localhost:8000
```

Health check:

```text
http://localhost:8000/health
```

### Python directly

Use Python 3.11, install the dependencies, then run:

```bash
pip install -r requirements.txt
uvicorn app:app --host 0.0.0.0 --port 8000
```

FFmpeg is required for common audio formats.

## Coolify deployment

The intended production setup is:

```text
GitHub Pages → frontend
Coolify      → FastAPI + Demucs backend
```

Recommended Coolify configuration:

```text
Repository:
https://github.com/harisakthi1994-spec/karoke-mixer.github.io.git

Base Directory:
/backend

Compose File:
docker-compose.yml

Service:
karaoke-api

Container Port:
8000
```

### Environment variables

Set production values in Coolify Environment Variables rather than hard-coding them into Compose:

```text
ALLOWED_ORIGINS=https://harisakthi1994-spec.github.io
PUBLIC_BASE_URL=https://YOUR-API-DOMAIN
DEMUCS_MODEL=htdemucs
MAX_UPLOAD_MB=150
DEMUCS_TIMEOUT_SECONDS=1800
```

`PUBLIC_BASE_URL` is the backend base URL only, for example:

```text
https://karaoke-api.example.com
```

The frontend endpoint is then:

```text
https://karaoke-api.example.com/api/separate
```

## First deployment expectations

The first Docker build can take much longer than a normal web app because the image installs FFmpeg plus the Python/Demucs stack.

The first actual separation can also take longer because the Demucs model has to be initialized/downloaded.

CPU-only inference may be slow. A GPU-enabled server is preferred for a responsive production experience.

## Runtime audio flow

```text
1. User selects an audio file.
2. Browser decodes it for waveform/playback.
3. Browser uploads the file to POST /api/separate.
4. FastAPI writes a temporary input file.
5. Demucs separates vocals from the rest of the track.
6. Backend stores the final WAV stems.
7. Backend returns the two stem URLs.
8. Browser downloads both stems.
9. Browser decodes them into separate AudioBuffers.
10. Mixer controls each stem independently.
```

## Mixer controls

```text
Vocals volume       → vocal GainNode
Instrumental volume → instrumental GainNode
Pitch               → source detune
Tempo               → source playbackRate
Play / Pause        → both stems together
Stop                → restart from beginning
```

## Important implementation rules

### Do not reintroduce the old browser Demucs code

The original frontend attempted to use an unsupported Transformers.js source-separation pipeline. The project now uses the real backend for Demucs inference.

### Keep production configuration in Coolify

Do not commit production credentials, private tokens, or machine-specific settings to Git.

### Results cleanup

Generated stems are stored under `backend/results/`. For a larger production installation, add a scheduled cleanup policy or move results to object storage.

## Troubleshooting

### Separation service is not configured

Check `SEPARATION_API_URL` in `window` or local storage.

### HTTP 404 on `/api/separate`

Confirm the Coolify backend is running and that the configured domain points to the API service on port `8000`.

### CORS error

Make sure `ALLOWED_ORIGINS` contains the exact frontend origin:

```text
https://harisakthi1994-spec.github.io
```

### Container exits during deployment

Check Coolify deployment/runtime logs. Typical causes are insufficient memory, dependency installation failure, or incorrect Compose configuration.

### Separation is slow

Check CPU/RAM/GPU resources and whether the backend is running CPU-only.

### Health check fails

Open:

```text
https://YOUR-API-DOMAIN/health
```

You should receive JSON with `ok: true`.

## Newcomer onboarding path

Read files in this order:

```text
README.md
   ↓
index.html
   ↓
backend/app.py
   ↓
backend/Dockerfile
   ↓
backend/docker-compose.yml
   ↓
Coolify deployment
```

Work on `index.html` for UI/UX and browser audio behavior. Work on `backend/app.py` for the separation API and Demucs integration.

## Development checklist

Before changing the project:

1. Read this README.
2. Understand whether your change belongs to frontend or backend.
3. Keep frontend/backend responsibilities separate.
4. Keep the playback fallback intact when practical.
5. Test `/health` after backend changes.
6. Test with a short audio file before testing long songs.
7. Keep deployment-specific environment values in Coolify.

## Current status

The project has two layers:

- **Frontend:** static karaoke mixer UI, playback controls, waveform, lyrics, output-device selection, and browser-side mixing.
- **Backend:** FastAPI + Demucs service for real vocal/instrumental separation, intended for deployment through Coolify.
