# OSP Clip Studio V6

Autonomous editor for outside-plant video: fiber, coax, CATV, poles, buckets, splicing, and the rest of the field work. The app measures the file, asks a vision model what the frames show, transcribes speech, and only then asks a director model for a timestamped plan. FFmpeg executes that plan. It does not run model-written shell commands, and it does not grade the whole video with a preset.

V4 color presets (`night`, `cinematic`, `punchy`, `clean`) are gone. A client that still posts `style` gets a warning and an edit based on the footage.

## Social edits and automatic vertical highlights

Choose **CapCut-style** for bold three-word caption groups with a yellow active-word pop, alternating punch-ins and occasional short dissolves. Speech stays at natural speed. An uploaded music track enables measured onset alignment on nearby non-speaking cuts; the app keeps the original pacing when no clear beat is found.

Choose **Up to 3 vertical highlights** to rank and export distinct groups from the AI-selected timeline. Each clip gets its own download and quality check; encodes run sequentially. Short sources may produce fewer clips. Scores reflect the director's importance estimates, not predicted social performance.

Enable **Track the subject** to use the vision-selected work/person box as a seed and track visual features on a 320×180, 4 fps stream. Vertical crop movement is smoothed and clamped to the picture. When tracking loses detail, the app holds framing; it reports untracked shots rather than guessing. This follows visible features, not a guarantee of identity tracking through occlusion.

Existing uploads can use these options by selecting them and clicking **Run the director again**. The stored analysis is reused.

## Finished edits and 800 MB uploads

Highlights now ask for a complete visual story: a strong opening, useful work detail, complete spoken thoughts, and a meaningful close. Quiet work footage is retained. Speech can play over a better shot from elsewhere in the same source, with captions following that audio. Opening titles overlay footage rather than adding a blank title card. No synthetic narration is generated.

The video limit is **800 MiB** (838,860,800 bytes); the duration limit remains 10 minutes. Browser uploads larger than 32 MiB use 16 MiB chunks stored on disk. If the connection drops, retry with the same file to resume the saved offset. Interrupted staging uploads expire after 24 hours. Music is attached at finalization and retains its separate 30 MiB limit. Staging and finalization temporarily require up to two copies of the source on disk.

For API clients: create a session with `POST /api/uploads` and JSON `{"filename":"clip.mov","size":12345}`, send raw chunks with `PUT /api/uploads/<upload_id>?offset=<byte-offset>`, inspect progress with `GET /api/uploads/<upload_id>`, and submit the normal job form with `upload_id` instead of `video`. Use `DELETE /api/uploads/<upload_id>` to discard a staging upload. Direct multipart video uploads remain supported.

## Reliability and usability update

- Retry, timeline re-render, and director revision run with the application context they need.
- Queue admission reserves each job once and saves its queued status before work starts.
- Shot detection streams 320×180 frames at 8 fps into PySceneDetect, avoiding OpenCV decoder pools and frame queues. Gray quality and motion samples remain 160×90.
- Vision stills are extracted sequentially, so 16 requested images no longer open 16 source decoders at once.
- FFmpeg decoders, filters, and encoders use one thread. H.264 exports disable frame lookahead to reduce retained buffers. OpenCV and numerical library thread pools are also bounded.
- Audio quality review skips video decoding.
- The studio previews your selected video, shows upload progress, preserves export playback while polling, and keeps Retry visible even if no export was produced.

A regression test monitors the combined resident memory of the app and FFmpeg children, terminates the test if they exceed 512 MiB, and checks a 30.4-second 4K input, 16 vision stills, and both 720p export shapes with the web app and provider SDK resident. It uses the test AI provider and does not call OpenAI. Local analysis plus 16 stills peaked around 200 MiB; a complete export run peaked around 285 MiB before loading the web app and SDK. Those measurements are specific to the synthetic test video, not a guarantee for every codec or input.

## What it does

1. **Measure.** PySceneDetect finds shot changes. A histogram pass adds any hard cut it missed. OpenCV reads luminance, blur, and sparse optical flow. FFmpeg measures silence, loudness, clipping, and a half-second energy curve.
2. **Watch.** A small set of JPEGs (capped by `BUDGET_MAX_FRAMES`) goes to a vision model at low detail. Up to `BUDGET_MAX_CLOSEUPS` frames can be sent again larger if the model flags a real work detail.
3. **Listen.** Speech audio is transcribed with word timestamps when the provider supports them. Filler words are marked from those timestamps. Silent files skip transcription and say so.
4. **Decide.** The director model returns JSON. The schema rejects anything outside the source, unrealistic speed changes, and private-looking titles. The server then trims black frames and dead air, locks speech to natural speed, attaches transcript words as captions, and blurs plates, addresses, documents, or customer faces only where the vision notes included a box.
5. **Render.** Each kept span is reframed, graded mildly, captioned, and joined. Dissolves are rare. Music, if you upload a track you have rights to, is ducked under speech. Portrait and landscape are separate encodes, one after the other, so a small instance is not asked to do both at once.
6. **Review.** The export is checked for black frames, exposure, duration, audio sync, clipping, caption burn-in, transition count, and word-splitting cuts. A failed check revises the plan deterministically and renders again, inside `BUDGET_MAX_RETRIES` and `BUDGET_MAX_RENDER_SEC`.

If `OPENAI_API_KEY` is missing, upload returns **503** and the page explains that autonomous editing did not run. Nothing is substituted.

The director is told the filename is not evidence and that it must not invent work, addresses, or completion. That is a model instruction plus schema checks. It is not a guarantee. Review an export before you publish it. Blur boxes are only as good as the vision notes.

## Run locally

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
sudo apt-get install -y ffmpeg fonts-dejavu-core   # Debian/Ubuntu
export OPENAI_API_KEY=sk-...
export WORK_DIR=/tmp/osp-v6
python server.py
```

Open `http://localhost:10000`. Health is `/health`.

```bash
pip install -r requirements-dev.txt
pytest -q
```

Tests use a local stand-in that still has to read the measured evidence. They do not call OpenAI. Production code has no offline edit plan.

## Render

`render.yaml` is a Docker web service with a 10 GB disk mounted at `/var/data` and `WORK_DIR=/var/data/osp`. Set `OPENAI_API_KEY` in the Render dashboard. Do not put the key in git.

This repo was not deployed to Render from here. After you merge and deploy, check `/health` (`ok: true`, `ai_configured: true`) and run one short clip before a long one. The default pipeline is tuned for one 720p encode at a time on Starter; unusual codecs or large reference-frame buffers can require more memory. `MAX_CONCURRENT_JOBS` defaults to 1 because FFmpeg is the memory limit. Free instances sleep and lose a disk that is not attached; without the disk, files live under `/tmp` and disappear on restart.

A restart marks an in-progress job as interrupted. The upload is kept until `JOB_TTL_HOURS` (48). Use **Retry** so a second model call only happens when you ask for it.

## Environment

See `.env.example`. The ones that matter:

| Variable | Role |
| --- | --- |
| `OPENAI_API_KEY` | Required for an autonomous edit |
| `AI_PROVIDER` | `openai` only. `AI_BASE_URL` is optional for a compatible endpoint |
| `VISION_MODEL`, `DIRECTOR_MODEL`, `TRANSCRIBE_MODEL` | Default `gpt-4o-mini` and `whisper-1` |
| `WORK_DIR` | Job files, status, and outputs |
| `MAX_UPLOAD_MB`, `MAX_DURATION_SEC` | Default 800 MB and 10 minutes |
| `BUDGET_MAX_FRAMES`, `BUDGET_MAX_TOKENS`, `BUDGET_MAX_COST_USD` | Hard stops |
| `BUDGET_MAX_RETRIES`, `BUDGET_MAX_RENDER_SEC` | Quality-review loop |
| `AI_PRICE_JSON` | Optional rate overrides |

Costs shown in the UI are **estimates** from published 2025 list rates (`gpt-4o-mini` input $0.15 / output $0.60 per million tokens, Whisper $0.006 per minute) plus the token counts the API actually returned. Check them against your invoice. Override the table with `AI_PRICE_JSON` when your prices differ. Unknown chat models are priced like `gpt-4o-mini` and flagged.

## API

- `POST /api/jobs` with `video`, optional `music`, `mode` (`highlights` or `full`), `orientation` (`portrait`, `landscape`, `both`), and `instructions`.
- `GET /api/jobs/<id>` progress, cost, warnings.
- `GET /api/jobs/<id>/plan` the plan plus what the models saw.
- `PUT /api/jobs/<id>/plan` with `{ "segments": [{ "keep", "source_start", "source_end" }] }` re-renders without another model call. Edges can still snap off black frames and mid-word cuts.
- `POST /api/jobs/<id>/redirect` with `{ "instructions" }` runs the director again. That spends more tokens.
- `POST /api/jobs/<id>/retry` after an error, using the stored upload.
- `GET /api/jobs/<id>/media/source|portrait|landscape` and `?download=1`.
- `POST /api/create`, `GET /api/status/<id>`, and `GET /download/<id>` still work for older clients.

Job URLs are unguessable and there is no login. Treat the service as private.

## Layout

- `osp/analysis` — probe, scenes, motion, picture, sound. No model calls.
- `osp/ai` — vision, speech, director prompts, OpenAI client.
- `osp/schema.py` — the only plan the renderer will accept.
- `osp/render` — FFmpeg.
- `osp/qa` — export checks.
- `osp/jobs` — status files and the pipeline.
- `templates/` and `static/` — the phone and Chromebook UI.

## Limits

- One encode at a time, 720p, one FFmpeg thread, by default.
- Highlights default to 45 seconds or less. Full mode keeps useful time and drops dead air; it does not promise a frame-accurate copy of a 10 minute tape.
- Word-level captions need provider word timestamps. Otherwise captions use real segment times, or they are turned off.
- Sensitive blur only runs when the vision model returns a box. A plate it did not mark can still be in frame.
- Render was not verified on your live Render service.
