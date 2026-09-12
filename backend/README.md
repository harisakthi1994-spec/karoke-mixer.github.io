# Karaoke Mixer Separation API

FastAPI + Demucs service for real vocal/instrumental separation.

## Local

```bash
docker compose up --build
```

Health: `http://localhost:8000/health`

Set these environment variables in production:

- `PUBLIC_BASE_URL=https://YOUR-API-DOMAIN`
- `ALLOWED_ORIGINS=https://harisakthi1994-spec.github.io`
- `DEMUCS_MODEL=htdemucs`

The API endpoint is `POST /api/separate` and returns URLs for `vocals.wav` and `instrumental.wav`.
