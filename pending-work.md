# Karaoke Mixer: pending work (updated 2026-10-12)

## Done and verified live
- Job-queue separation: upload, poll, MP3 stems, progress, timeouts
- Pause/resume keeps position, including with tempo changes
- Vocal and instrumental sliders; output device switching
- Split output with per-output delay and per-output vocal/instrumental mixes. Works in Chrome, Edge and Brave only.
- Sync test (click on both outputs), ±10 ms buttons, delay saved per device (localStorage `delay:<deviceId>`). Verified with Speakers (Realtek) + Bluetooth earbuds; the earlier "silent speaker" was a Windows output-device issue, not the app.
- Server-side stem cache keyed by SHA-256 (CACHE_TTL_DAYS / CACHE_MAX_GB cleanup) and the Available Songs library (`/api/library`, DELETE `/api/cache/{sha256}`). Old cache entries without meta.json are listed as "Unnamed song (xxxxxxxx)" and get their real name when the file is re-selected.
- Backend repo cleanup: CPU-only PyTorch, README refresh, worker.js removed. Docker cleanup schedule set in Coolify (disk stays low).
- Synced lyrics: LRC upload, live highlight, offset slider, per-song storage (keyed by sha256).
- UI redesign: "Import & Split" tab and "Player" tab (library, now-playing card, karaoke lyrics panel, collapsible Output settings).
- Click/drag-to-seek on the waveform (works while paused), Left/Right arrow keys seek ±5 s (Shift ±15 s).
- Lyrics Sync editor: line list with timestamps, keyboard shortcuts (R stamp, Up/Down, T, Y, E, Delete, K/Space), Download .lrc.

## Decided / rejected
- Microphone auto-sync for Bluetooth latency: rejected. A laptop mic can't reliably hear earbuds. The manual Sync test with saved per-device delay is the chosen approach.

## Parked (Hari: "focus on it later")
- First-play download delay for a saved song (~15 MB, ~30 s). Repeat plays are fast. Ideas noted: stems at 128 kbps, immutable Cache-Control headers, re-encode old cache, CDN (e.g. Cloudflare cache rule on /api/cache/*), progressive playback. First check in DevTools whether time is server wait or download.

## Backlog
- Download buttons for the separated stems
- Full-screen karaoke mode (lyrics only, large)
- Rename songs from the library list
- Independent pitch and tempo (SoundTouch or Rubber Band; currently coupled)
- Optional password/API key for library listing if the app is shared
- GPU host for faster separation
- Verify Coolify auto-deploy webhook on push

## Workflow notes
- Claude Code once edited a stale copy of index.html and overwrote features (commit 3a6b8c6); fixed by restoring from d3c6f13. Always start prompts with `git checkout main && git pull origin main`, use small targeted edits, never rewrite or recreate the file, and do not use the GitHub MCP tool.
- e3c582a is an orphan commit (twin of d3c6f13); ignore it.
