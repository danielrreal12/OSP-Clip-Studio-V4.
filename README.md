# OSP Clip Studio V4

## What actually works
- One-tap upload and export of MP4; vertical 9:16 or horizontal 16:9.
- Night denoising and exposure adjustment, cinematic/punchy/clean grading.
- FFmpeg audio loudness normalization; optional licensed MP3 background mix.
- Optional OpenAI Whisper transcription with burned-in segment captions.
- Optional AI-written title and social caption.
- Job progress polling and download.

## Not implemented
This build does **not** do intelligent visual scene selection, beat-synced cuts, motion-blur transitions, speed ramps, voice separation, or animated word-by-word subtitles. AI planning sees a transcript, not video frames. These are future upgrades, not current features.

## GitHub/Render deployment
1. Back up existing V3 repository. Replace root `server.py`, `requirements.txt`, `Dockerfile`, `render.yaml`, and add `templates/index.html`. Remove or leave old `index.html` unused. Keep Render service name consistent with existing service.
2. Render: use **Docker** runtime. If current service is a Python runtime, you may need a new Docker web service; Render runtime changes may not be supported in-place.
3. In Render Environment, add `OPENAI_API_KEY` as a secret. Never commit API keys to GitHub. Without the key, video processing still works, but AI transcription and edit planning are disabled.
4. Set a paid Render instance with enough CPU/RAM for FFmpeg; start with Starter and upgrade if renders time out or exhaust memory. Render free instances can spin down and have ephemeral storage.
5. Visit `/health` to verify `version: 4.0`. Upload a short sample video before attempting large videos.

## Limitations and operations
- Files are stored temporarily on the instance filesystem and can disappear on restart/redeploy. This version has no automatic cleanup, authentication, job queue persistence, or per-user isolation. It is intended for **private/testing use**, not public production deployment.
- For production, add authentication, background workers, object storage, rate limits, job cleanup, and upload scanning.
- Render may impose request duration, proxy body size, and resource limits. Large mobile uploads may fail; test with short clips.
- AI usage is billed separately by OpenAI according to transcription and text-token pricing. Render hosting is billed separately. Music is user-provided; do not upload copyrighted tracks without rights.
- Captions require audio, API key, and successful transcription; subtitle rendering requires FFmpeg libass (included in standard Debian ffmpeg package).
- This build has no client-side upload persistence. Keep Safari open until upload completes; once upload finishes, processing runs server-side, but restarting Render loses in-memory jobs.
