# Karaoke Mixer: pending work (updated 2026-10-11)

## Done and verified live
- Job-queue separation: upload, poll, MP3 stems, progress, timeouts
- Pause/resume keeps position, including with tempo changes
- Vocal and instrumental sliders
- Output device switching
- Split output: Output A = instrumental only, Output B = vocals + instrumental, with a delay slider per output (main, commit 33fca07). Works in Chrome, Edge and Brave only.

## Planned, not started

### 1. Sync test for Bluetooth latency (chosen approach, do later)
Bluetooth output lags the wired/speaker output by roughly 150-300 ms. The browser can't measure this reliably, and the microphone auto-calibration idea was rejected as impractical.
- Add a "Sync test" button that plays a steady beat on both outputs.
- Add fine-adjust buttons (±10 ms) next to the existing delay sliders.
- Save the delay per device (keyed by deviceId, in localStorage) and apply it automatically when that device is selected.
- Optional: quick presets, e.g. "Bluetooth earbuds ~200 ms".
- Alternatives noted: low-latency codec hardware (aptX LL), or a per-output delay tool outside the browser (Voicemeeter on Windows, Airfoil on Mac; not checked for this setup).

### 2. Backend and repo cleanup (Claude Code prompt already written)
- CPU-only PyTorch in backend/Dockerfile and backend/requirements.txt (keep the torchcodec pin; if the build fails on a torch/torchcodec version error, the pins need fixing)
- README refresh: job flow, env vars, split output, pitch/tempo coupling
- Delete the unused worker.js stub

### 3. Backlog
- Independent pitch and tempo (needs SoundTouch or Rubber Band; currently coupled)
- Click-to-seek on the waveform
- Download buttons for the separated stems
- Synced lyrics
- Saved songs cache in the browser (IndexedDB, keyed by file hash) so a repeat song loads without re-upload. Parked by Hari, revisit later.
- Optional: server-side cache by file hash, GPU host for faster separation
