# Patch Notes: America

Initial podcast-production application for turning a narration script into a finished MP3 using a locally hosted Chatterbox server.

## MVP features

- Browser-based episode editor on port `8081`
- Standalone Image Studio for generating downloadable portrait, square, or landscape PNG artwork outside the episode workflow
- Video Studio with editable storyboards, imported free B-roll, local images, and mixed-media proof exports
- Complete editable episode saves, including the cast, research packet, source notes, script, conversation settings, show-open settings, and intro audio
- Preloaded pilot narration script
- Per-host Chatterbox reference voice, tempo, exaggeration, CFG weight, temperature, Min P, Top P, and repetition penalty
- Optional uploaded intro track with multi-host spoken lines mixed over the music or played after it
- Automatic TTS-friendly script cleanup
- Automatic script chunking (default max 700 characters)
- Durable speech-chunk jobs with progress polling and resumable audio
- Compatibility spacing for resumable jobs created before transition-boundary metadata was added
- Persistent background render queue designed for slow CPU-only generation
- Chatterbox generation through the bundled service's `POST /v1/audio/speech` API
- Per-chunk WAV files retained for selective regeneration/debugging
- FFmpeg assembly into a final 128 kbps MP3
- Persistent `episodes/` source scripts and `output/` generated episodes
- Health endpoint showing whether the app can reach Chatterbox
- Basic unit tests and GitHub Actions CI

The production workspace uses a wide desktop editor with side navigation and a
responsive stacked layout on smaller screens. See the [design references](docs/studio-design-reference.md)
for the research and visual direction behind the interface.

## Architecture

For a product-level view of the interface, workflows, data, APIs, and runtime
boundaries, see the [application design map](docs/application-design-map.md).

```text
Browser :${APP_PORT:-8081}
    |
    v
host port ${APP_PORT:-8081} -> FastAPI container :8080
    |
    +--> script cleanup/chunking
    |
    +--> Chatterbox :8000
    |       |
    |       +--> chunk-001.wav
    |       +--> chunk-002.wav
    |       +--> ...
    |
    +--> FFmpeg
            |
            +--> final episode.mp3
```

## Deployment modes

The Compose configuration deliberately keeps container ports stable while
making every host port configurable. It also avoids fixed `container_name`
values, so Patch Notes can run alongside theHunter and other Compose projects
without container-name collisions.

Copy the sample settings before starting:

```bash
cp .env.example .env
```

Change `APP_PORT` in `.env` if theHunter already uses `8081`. The default binds
Patch Notes only to `127.0.0.1:8081`; set `APP_BIND_ADDRESS=0.0.0.0` only when
the app must be reachable from other machines.

### Start Patch Notes and Chatterbox

Chatterbox is required, so the default Compose stack always starts it with Patch
Notes. Start both services from the project directory:

```bash
./scripts/start-and-check.sh
```

That command deliberately starts the portable CPU mode. On an NVIDIA host with
the NVIDIA Container Toolkit installed, select CUDA explicitly:

```bash
./scripts/start-and-check.sh --gpu
```

Seeing `Torch 2.6.0+cu124` or `CUDA 12.4` in the status card only means the
container has CUDA-enabled libraries installed. It does **not** mean Docker has
made a GPU visible. `CUDA visible: false` together with
`Requested/resolved: cpu / cpu` proves the CPU Compose configuration was
started. The `--gpu` option adds `docker-compose.gpu.yml`, requests the NVIDIA
device, and sets `CHATTERBOX_DEVICE=cuda`; startup then fails rather than
silently falling back to CPU if CUDA is unavailable.

The startup check also verifies that the live `/api/health` response reports
the selected device. If `--gpu` reaches an old CPU container or a different
Compose project on the configured port, the script now exits with an explicit
device-mismatch error instead of reporting a successful CUDA startup.

The app always listens on container port `8080`. Compose publishes it on the
host using `APP_BIND_ADDRESS` and `APP_PORT`, which default to
`127.0.0.1:8081`. The startup script rebuilds and force-recreates the app,
starts Chatterbox, removes containers orphaned by older Compose configurations,
waits for the configured health endpoint, and prints logs from both services
on failure. Startup is considered successful only after the app reports that
Chatterbox is online, so audio generation is ready when the script returns.

Open:

```text
http://127.0.0.1:8081
```

The top status card shows readiness only after the model has loaded and a fixed
end-to-end synthesis smoke test has produced a valid WAV:

```text
App ready · synthesis verified
```

Chatterbox defaults to `127.0.0.1:8000`. Set `CHATTERBOX_PORT` in `.env` if another
audio service already owns that host port. Communication from Patch Notes to
Chatterbox stays on Docker's private network and is not affected by the selected
host port. The **Open Chatterbox** link uses the browser's current hostname plus
`CHATTERBOX_PORT`, rather than hard-coding `localhost`. Set `CHATTERBOX_PUBLIC_URL`
when Chatterbox is published through a different hostname, path, or HTTPS reverse
proxy.

Audio requests use the bundled Chatterbox service: `input`, `model`, `voice`,
`speed`, and `response_format` are sent to `POST /v1/audio/speech`. The endpoint,
health path, and per-chunk read timeout can
be overridden with `CHATTERBOX_TTS_PATH`, `CHATTERBOX_HEALTH_PATH`, and
`CHATTERBOX_TIMEOUT_SECONDS`. A response is accepted only when it contains a
WAV/RIFF payload, preventing an API JSON response from being saved and passed
to FFmpeg as audio.

Narration is split into requests of at most 280 characters by default, and the
bundled service rejects direct requests over 300 characters. Chatterbox has a
finite acoustic-token window; oversized requests can exhaust that window and
degrade into static, a sustained tone, or repeated audio rather than completing
the speech. Keep `MAX_CHARS_PER_CHUNK` at or below
`CHATTERBOX_MAX_INPUT_CHARS` if either limit is customized. A new generation
job is required after changing the limit because completed chunks are reused.

The first generation downloads Chatterbox model weights into the persistent
`chatterbox-models` volume. CPU inference is the known-good default and can be
slow. The default 600-second request timeout accommodates model startup.

The service defaults to `CHATTERBOX_GENERATION_MODE=baseline`, which calls only
parameters exposed by the installed model's public `generate` signature. Set
the mode to `advanced` only for the pinned Chatterbox 0.1.6 image; its Min P,
Top P, and repetition-penalty bridge is version- and signature-checked during
startup. Unknown private APIs fail startup rather than being patched blindly.

To qualify a GPU, use the repeatable 16-case acceptance matrix in
`scripts/qualify-chatterbox.py`. It renders one short sentence and one medium
paragraph with both the built-in voice and one normalized reference voice,
through both the pinned package directly and the repository's `generate_audio`
adapter, on CPU and CUDA. Every successful case is saved as a PCM WAV whose
name identifies the device, path, voice, and text case. The report beside the
WAVs records the fixed seed/settings, package and GPU environment, SHA-256
digests, and objective duration, peak, RMS, DC-offset, clipping, and finite
sample measurements.

Run it in the GPU image, supplying an authorized reference recording from the
read-only voices mount and a writable host artifact directory:

```bash
mkdir -p artifacts/chatterbox-qualification
docker compose -f docker-compose.yml -f docker-compose.gpu.yml run --rm \
  -v "$PWD/artifacts/chatterbox-qualification:/artifacts/chatterbox-qualification" \
  chatterbox python /service/qualify-chatterbox.py \
  --reference-voice /voices/REFERENCE_ID.wav \
  --output-dir /artifacts/chatterbox-qualification
```

The command exits zero only when all 16 cases pass, and
`qualification-report.json` is the evidence for the gate. Run this matrix and
require a passing report on the target production hardware before promoting
the GPU launch command below. Once that report passes, the GPU command is the
recommended production command; retain the base CPU command as a diagnostic
path. Until then, production remains on the base CPU configuration. If direct
CUDA fails, investigate the CUDA/Torch/GPU compatibility
stack first. If direct CUDA succeeds but adapter CUDA fails, investigate
`generate_audio`, sampler injection, tensor conversion, and encoding. If both
CUDA paths pass but complete episode output fails, investigate
`synthesize_chunk`, normalization, and final assembly. A missing GPU is a gate
failure rather than a skipped test.

The base `docker-compose.yml` intentionally selects CPU inference and makes no
GPU request, so it remains portable. GPU inference has a dedicated checked-in
override. On a host with the NVIDIA Container Toolkit installed, launch it with
this exact command:

```bash
docker compose -f docker-compose.yml -f docker-compose.gpu.yml up -d --build
```

The equivalent checked startup command is `./scripts/start-and-check.sh --gpu`.

The override sets `CHATTERBOX_DEVICE=cuda` and uses the Compose Deploy
Specification's NVIDIA device reservation (`deploy.resources.reservations.devices`).
This is the only GPU allocation mechanism used: do not add the older
`runtime: nvidia`, a top-level `gpus`, or manual `/dev` mappings. It requests one
GPU and exposes only the NVIDIA `compute` driver capability needed for
inference (rather than graphics, display, video, or compatibility capabilities).

Chatterbox's health check reads `/health` and fails when the requested device is
not ready. The app waits for that check, and both the API and browser disable
episode submission unless the model-loading and fixed synthesis smoke test
passed. `/health` reports the requested and resolved device, Torch/CUDA/cuDNN
versions, representative model parameter placement, smoke-audio measurements,
and the actionable startup failure. A GPU deployment with unavailable CUDA
never falls back to CPU or proceeds as though it were healthy. After startup, verify both the
configured mode and actual CUDA access from inside the running container:

```bash
docker compose -f docker-compose.yml -f docker-compose.gpu.yml exec chatterbox \
  python3 -c 'import os, torch; assert os.environ["CHATTERBOX_DEVICE"] == "cuda"; assert torch.cuda.is_available(); print(torch.cuda.get_device_name(0))'
docker compose -f docker-compose.yml -f docker-compose.gpu.yml exec chatterbox \
  python3 -c 'import json, urllib.request; data=json.load(urllib.request.urlopen("http://localhost:8000/health")); assert data["ok"] and data["requested_device"] == "cuda" and data["resolved_device"].startswith("cuda"); print(data)'
```

For the portable CPU deployment, continue to use `./scripts/start-and-check.sh`
or `docker compose up -d --build`. You may also set
`CHATTERBOX_DEVICE=cuda ./scripts/start-and-check.sh` as a non-positional
equivalent of `--gpu`; the script selects the required Compose override rather
than merely changing an environment value.

#### Validated Chatterbox GPU software stack

The Chatterbox image uses a deliberately pinned GPU stack rather than accepting
an implicit transitive PyTorch install:

| Component | Validated/pinned value | Compatibility basis |
| --- | --- | --- |
| Chatterbox | `chatterbox-tts==0.1.6` | Its published package metadata requires Python `>=3.10`, `torch==2.6.0`, and `torchaudio==2.6.0`. |
| Python | `3.10` | Supplied by the Ubuntu 22.04 CUDA image and within Chatterbox's declared Python range. |
| PyTorch | `torch==2.6.0+cu124` | Official PyTorch CUDA 12.4 wheel. |
| TorchAudio | `torchaudio==2.6.0+cu124` | Official matching CUDA 12.4 wheel; Torch and TorchAudio releases must match. |
| CUDA user-space runtime | `12.4.1` | `nvidia/cuda:12.4.1-cudnn-runtime-ubuntu22.04`; matches the wheel's CUDA 12.4 build. |
| cuDNN | CUDA 12.4 image's versioned cuDNN runtime | Its numeric runtime version is recorded during the build. |

The CUDA 12.4 runtime requires a host NVIDIA driver new enough to support CUDA
12.4 (Linux driver `>=550.54.14`). Newer NVIDIA drivers are backward compatible
with this CUDA runtime. Before enabling GPU inference, record the actual host
GPU, driver, and compute capability (the build environment used for this change
does not expose `nvidia-smi`, so it cannot truthfully supply host-specific
values):

```bash
nvidia-smi --query-gpu=name,driver_version,compute_cap --format=csv,noheader
```

The result is the deployment-specific validated combination. Confirm container
access with the same CUDA release, then build Chatterbox:

```bash
docker run --rm --gpus all nvidia/cuda:12.4.1-base-ubuntu22.04 \
  nvidia-smi --query-gpu=name,driver_version,compute_cap --format=csv,noheader
docker compose build --no-cache chatterbox
docker compose run --rm chatterbox cat /image-build-versions.txt
```

The build prints and stores the resolved Python, Chatterbox, Torch, TorchAudio,
CUDA, and cuDNN versions in `/image-build-versions.txt`. It also fails at image
build time unless Torch and TorchAudio import successfully and exactly match the
pinned CUDA builds. This smoke check does not require a GPU; host/device access
is checked only when the container runs with the NVIDIA Compose device
reservation.

#### Applying timeout configuration changes

The application source is copied into the app image when it is built, and
environment variables are read when the Python process starts. Consequently,
`docker compose restart` alone does not install an updated `app/main.py` or
apply changes made to the Compose environment. After pulling this fix or
changing `CHATTERBOX_TIMEOUT_SECONDS` in `.env`, rebuild and recreate the app:

```bash
docker compose up -d --build --force-recreate app
```

Recreating the app container does not remove episode, output, config, or model
data. To confirm that the new container has both the fixed source and the value
from `.env`, run:

```bash
docker compose exec app python -c \
  'from app.main import CHATTERBOX_TIMEOUT_SECONDS; print(CHATTERBOX_TIMEOUT_SECONDS)'
```

If that command still reports a `NameError`, force a clean app-image rebuild:

```bash
docker compose build --no-cache app
docker compose up -d --force-recreate app
```

For voice cloning, open a host's **Voice** section, choose an authorized WAV,
MP3, FLAC, or M4A recording, and wait for the upload to finish. The application
validates and converts it to 24 kHz mono 16-bit PCM WAV, stored under
`data/voices/<host-id>/reference.wav`. Use **Play Reference**, adjust
Exaggeration/CFG Weight, and use **Preview Voice** before saving the cast. Do not
clone a voice without the speaker's permission. `MAX_VOICE_UPLOAD_MB` controls
the upload limit and defaults to 50 MB.

### Single-host performance editor

When an episode has exactly one host, **Analyze delivery** divides the spoken
transcript into editable performance beats and suggests a deterministic delivery
preset and intensity for each beat. The producer can change the text, split or
merge beats, choose another preset, adjust intensity, and preview a beat before
rendering. The host's saved voice controls remain the baseline; presets apply
bounded adjustments to exaggeration and CFG weight.

Adjacent paragraphs with the same suggested delivery are grouped into one beat
so ordinary narration does not restart the voice at every paragraph.

Resolved controls are deliberately subtle and continuity-limited: each beat
starts from the preceding beat's effective settings, and exaggeration/CFG
changes are capped before synthesis. Repeated compatible beats may build
gradually, while a return to baseline also happens gradually instead of jumping
between paragraph-level performances.

Performance editing is opt-in. Leaving **Apply approved performance beats when
rendering** unchecked preserves the existing host-level rendering behavior.
When enabled, the accepted beats and their resolved numeric settings are frozen
in the generation job so retries and restart recovery use the same delivery.
Saved episode documents retain disabled or enabled beats, while older documents
load with performance editing disabled.

### Removing containers from older versions

The recommended startup script removes orphaned services automatically. For a
stack started manually, remove stale services once and bring up the current
Chatterbox-backed stack:

```bash
docker compose down --remove-orphans
docker compose up -d --build --remove-orphans chatterbox app
```

This cleanup does not remove the bind-mounted `episodes/`, `output/`, `config/`,
or `data/` directories, so queued jobs and uploaded voices remain available.

## Generate the pilot

1. Open `http://127.0.0.1:8081`.
2. The pilot script is preloaded.
3. Open each host's **Voice** section to upload, play, tune, and preview a unique
   reference recording. Advanced sampling controls remain available below it.
4. Click **Generate episode**.
5. When complete, click **Download MP3**.

Use **Save episode** at the top of the editor to create a resumable episode
file. Choose it from the saved-episode menu and click **Load** to restore the
entire editing session. Saving again updates the same episode. Saved episode
documents and their optional intro tracks live under `episodes/saved/`, so they
survive application and container restarts with the existing `episodes/` bind
mount. A restored intro track is also reused automatically when the episode is
submitted for audio generation; it does not need to be uploaded again.

### Import episode details from JSON

Manual entry and AI drafting remain available. In **Episode setup**, expand
**Import episode JSON**, paste JSON or upload a `.json`/`.txt` file (up to 2 MB),
then choose **Read JSON**. Select an episode from the list and choose
**Import selected episode**. When there are multiple episodes, explicitly choose
one from the list; the production document itself needs no episode title. A
production document can contain multiple episodes:

```json
{
  "schema_version": "1.0",
  "show": "Patch Notes",
  "production_date": "2026-10-28",
  "production_timezone": "America/New_York",
  "editorial_direction": "Light, entertaining, fact-checked",
  "episodes": [
    {
      "title": "My next episode",
      "host": "Wade Mercer",
      "theme": "Why this story matters",
      "cold_open": "Welcome to Patch Notes.",
      "transcript": "Welcome to Patch Notes.\n\nHere is today's story.",
      "estimated_runtime_minutes": 15,
      "promotional_creative": {"image_prompt": "An editorial photograph"},
      "fact_check": {
        "confirmed": ["A verified fact"],
        "uncertain_or_developing": ["An unresolved question"],
        "editorial_cautions": ["A framing caution"]
      },
      "sources": [{"title": "Source title", "url": "https://example.com"}]
    },
    {
      "title": "A second episode",
      "host": "Wade Mercer",
      "cold_open": "Here is the next story.",
      "transcript": "Here is the next story.\n\nLet's take a closer look."
    }
  ]
}
```

A single episode object, a bare array of episode objects, or a production document
with a singular `episode` object also works:

```json
{
  "schema_version": "1.0",
  "show": "Patch Notes America",
  "production_date": "2026-10-29",
  "episode": {
    "title": "My next episode",
    "host": "Wade Mercer",
    "transcript": "Welcome to the show.",
    "estimated_runtime_minutes_at_145_wpm": 11.1
  }
}
```

Use either `episode` or `episodes` in a production document, not both. Each episode requires non-empty `title`,
`host`, and `transcript` strings. The host name must uniquely match an existing
Cast profile (case-insensitive); configure a missing host and its voice before
importing. Matching retains its identity, voice, and performance settings without
saving changes to workspace profiles. Optional estimated runtime must be 2–60
minutes and is rounded to the nearest whole minute; the default is 8 minutes.
`estimated_runtime_minutes_at_145_wpm` is also accepted, with
`estimated_runtime_minutes` taking precedence when both are supplied.
An image prompt is limited to 2000 characters.

Import fills the script, maps fact-check lists and
sources into the research editor, and fills the image prompt and conversation
tone. Shared production metadata and all episode fields except the transcript
are retained in **Quick story / source notes**, including description, format,
cold open, closing, promotional clip prompt, and extra producer fields.
`cold_open` fills **Cold open / host intro lines**, and its show-open controls
expand after import. If the transcript begins with that exact opening passage,
the importer separates it from the main script so it is spoken once. The original
transcript is retained in source notes whenever it is split. A transcript
containing only the cold open leaves the main script empty for you to write.
Closing text remains in the transcript and notes; it is not appended again. Fact-check
labels are imported as provided; the importer does not independently verify claims.

Import validates the complete document before replacing editor content and uses
the existing unsaved-edit warning. Invalid JSON or an unmatched host keeps the
current episode intact. It creates a new, unsaved editor session, clears the old
intro audio and performance beats, fills the imported cold open, and leaves audio generation to the normal
workflow. Review all fields and choose **Save episode** to retain the imported
episode using the existing saved-document format. Other episodes in the JSON are
available for subsequent imports; importing does not batch-save or render them.
Save the first imported episode before selecting and importing the next one.
Changed JSON is read again before it can replace the editor, so an old selection
cannot import details from an earlier document.

The imported `description` is displayed below **Episode title** in a read-only
**Episode description** box. Choose **Copy description** to reuse the text in
your publishing platform. If automatic clipboard access is unavailable, the text
is selected for copying with Ctrl+C or Cmd+C. Loading a saved imported episode
restores this display from the description already retained in source notes;
no saved-document migration is needed. Episodes without a description show an
empty box, with copying disabled. Keep the imported source notes to retain the
description for later publishing.

To create a show open, select an **Intro track** and add **Host intro lines** using
the same `[Host Name]` speaker tags as the main script. Leave **Play the host
intro over the music** selected to start both together, and use **Music volume
under voice** to keep the speech clear. The complete track is retained, and the
episode starts after both the music and spoken intro finish. Clear the overlap
option to play the full track before the host introduction instead.

Use **Episode image prompt** to guide the setting, subjects, mood, and
composition of generated social-clip artwork. The prompt is saved with the
episode and applied to both automatic and custom clips while the application's
safety and no-text constraints remain in force. Photography is the default
style; explicit producer styles are respected.

### Adjusting silence detection and timing

Audio timing is configured in `.env`. The detection threshold and boundary
settings apply to newly rendered speech chunks:

```text
# Detect quieter speech (the default is -45 dB).
SILENCE_THRESHOLD_DB=-50

# Retain up to this much quiet audio before and after detected speech.
MAX_LEADING_SILENCE_MS=300
MAX_TRAILING_SILENCE_MS=450

# Preserve this buffer around detected speech to avoid clipping quiet consonants.
SPEECH_SAFETY_BUFFER_MS=60
```

If quiet speech is being reported as `WAV contains no audible speech`, make
`SILENCE_THRESHOLD_DB` more negative in small steps, such as `-45` to `-50`.
If background noise is being treated as speech, move it in the other direction,
such as `-45` to `-40`. `MAX_LEADING_SILENCE_MS` and
`MAX_TRAILING_SILENCE_MS` change how much silence is retained after speech has
been detected; they do not change detection sensitivity. When a TTS response
contains a valid but unusually quiet waveform, detection automatically falls
back to a threshold relative to that recording's peak. Completely silent
(all-zero) WAV files are still rejected and retried.

The pauses deliberately inserted between chunks can be adjusted separately:

```text
SAME_SPEAKER_PAUSE_MS=225
NORMAL_TRANSITION_PAUSE_MS=400
SPEAKER_CHANGE_PAUSE_MS=500
DRAMATIC_PAUSE_MS=1000
```

After editing `.env`, rebuild and recreate the app so it receives the new
values. Existing rendered chunks are reused by resumed jobs, so start a new
render when evaluating detection changes:

```bash
docker compose up -d --build --force-recreate app
```

Generated files are retained under:

```text
output/<timestamp>-<episode-title>/
├── chunks/
│   ├── chunk-001.wav
│   ├── chunk-002.wav
│   └── ...
├── concat.txt
├── metadata.json
├── script.txt
└── <episode-title>.mp3
```

## Durable generation jobs

The browser now submits episode renders to `POST /api/generation-jobs` instead
of holding one request open for an entire episode. Each natural speech chunk
moves through `queued`, `running`, `retrying`, `complete`, or `failed` and is
checkpointed in the manifest. The final MP3 is assembled directly from the
ordered completed chunks. Progress survives page/API timeouts in
`output/<job-id>/job.json`; no arbitrary fixed-size render batches are used.

Final MP3 assembly uses FFmpeg's two-pass EBU R128 loudness normalization. The
complete program is measured first, then mastered to -14 LUFS integrated with a
-1.5 dB true-peak ceiling and encoded as a 128 kbps, 44.1 kHz stereo MP3. This
final-program pass preserves the relative dynamics between speech chunks while
producing consistent playback loudness suitable for Spotify-oriented delivery.

The queue uses one worker by default so concurrent episodes do not compete for
all CPU and memory. After submitting an episode, it is safe to close the
browser: open the app later and the **Render queue** lists active and completed
jobs with download links. Queued or running manifests are automatically
recovered after an app/container restart; interrupted renders restart from the
last unfinished chunk. Completed WAV files are validated before reuse, avoiding
the loss of hours of CPU rendering while preventing corrupt partial files from
entering the final episode.

Use **Cancel** beside a queued or running render to stop it, including a render
waiting for automatic clip artwork. Cancellation closes its active provider
request and frees the app's queue worker for the next episode. The job and
unfinished chunks become `cancelled`, while completed chunks and any assembled
MP3 remain on disk. An assembled MP3 keeps its download link even if optional
clips were cancelled. Cancelled jobs are terminal and are not recovered after
an app restart; the queue's **Cancelled** filter shows them. Submit a new render
to generate that episode again.

The bundled local image service checks disconnected requests before inference
and after each denoising step. A step already running must finish before the
model lock is released. Synchronous audio/FFmpeg work also completes its
current operation before the app can handle cancellation. Closing an OpenAI or
Chatterbox request stops the app waiting; those services may finish computation
already in progress. This change does not select a different CPU/GPU mode.

Transient TTS container crashes and dropped connections are retried five times
by default with an increasing delay. Configure `TTS_MAX_ATTEMPTS` and
`TTS_RETRY_DELAY_SECONDS` for a backend that takes longer to restart. Each
attempt and each completed chunk is written to `job.json`, so the Render queue
continues to show useful progress during a slow CPU render.

### Shared production FIFO

Audio renders, Video Studio builds/exports/image batches, standalone images,
scene images, previews, local planning, and custom clips share one FIFO
admission queue. Only one production job runs at a time, even when
`GPU_MODEL_HANDOFF=false` or `JOB_WORKERS` exceeds one. A durable job reserves
its position when submitted, before a feature worker reaches inference. Its
entire render, retries, model handoffs, and cleanup keep that turn; nested
speech/image/encoding operations do not enqueue again. Long videos therefore
delay short image or audio requests. Hosted image requests also use this queue.

The **Shared production queue** panel above the studio tabs reports the running
job and numbered waiting positions. `GET /api/production-queue` returns
`concurrency`, `active` (`null` or `{kind, id}`), and `waiting` (`{kind, id,
position}` entries). Existing audio/video panels retain job progress, errors,
cancellation, and downloads. Waiting durable jobs remain `queued` until
admitted. Existing Chatterbox readiness checks still apply to audio submission.

Audio/video manifests remain the durable recovery source; no schema migration
is needed. Startup restores their combined submission order using `created_at`,
with deterministic IDs breaking ties. Legacy manifests without a usable
timestamp recover first in deterministic order because their original order
cannot be reconstructed. Completed speech chunks and prepared video assets
retain their existing reuse rules. Cancelling a waiting job removes its
reservation; cancelling running work releases admission after cleanup.
Unexpected job errors do not stop later jobs from being dispatched.

Standalone images, previews, manual scene images/planning, and legacy synchronous
render/clip endpoints retain their request/response contracts. Their waiting
requests are held in memory and are **not** recovered after an app restart;
resubmit them if interrupted. Provider timeouts and existing bounded TTS retries
remain in effect. The queue does not impose a new whole-job timeout or estimate
completion times.

Run one Uvicorn application worker and one app instance per shared GPU. Admission
and model locks are process-local; this does not coordinate multiple replicas or
external GPU clients. Serialization limits contention, but one model/job can
still exceed VRAM. GPU handoff, CUDA checks, authentication, ports, and CPU/GPU
deployment selection retain their existing behavior.

```text
POST /api/generation-jobs
GET  /api/generation-jobs
GET  /api/generation-jobs/{job_id}
POST /api/generation-jobs/{job_id}/cancel
```

The cancellation endpoint returns the updated job and chunk progress, is
idempotent for cancelled jobs, and returns `409` for completed or failed jobs.
It also accepts pre-existing queued/running manifests without a migration.

The job manifest owns an ordered top-level `chunks` array. Every chunk retains
its status, attempt count, raw and normalized outputs, audio metrics, and last
error. Older manifests with chunks nested under `segments` are migrated when
read. If editorial grouping is needed later, use named chapters or explicit
export ranges that refer to chunks rather than changing synthesis durability.
See [Chatterbox audio editing and stitching](docs/chatterbox-audio-editing-and-stitching.md)
for the manifest and assembly model.

## Useful commands

Check the app:

```bash
curl http://127.0.0.1:8081/api/health
```

Watch logs:

```bash
docker compose logs -f app
```

Restart after code/config changes:

```bash
./scripts/start-and-check.sh
```

If a manual `curl` reports `Failed to connect`, confirm that the container is
running and that Docker published the expected port:

```bash
docker compose ps app
docker compose logs --tail=100 app
```

Use `127.0.0.1` rather than `localhost` for this check. In some WSL and Docker
Desktop configurations, `localhost` can resolve through a different IPv6 or
Windows forwarding path and reset the connection even though the IPv4-published
port is available.

Stop the application:

```bash
docker compose down
```

This stops both Patch Notes and its required Chatterbox service.

## Local development

```bash
python -m venv .venv
```

Linux/macOS/WSL:

```bash
source .venv/bin/activate
pip install -r requirements.txt pytest
CHATTERBOX_URL=http://127.0.0.1:8000 uvicorn app.main:app --reload --port "${APP_PORT:-8081}"
```

Windows PowerShell:

```powershell
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt pytest
pytest -q
$env:CHATTERBOX_URL="http://127.0.0.1:8000"
uvicorn app.main:app --reload --port 8081
```

## Initial roadmap

This roadmap is an inventory of the current implementation and the work needed
to qualify an MVP release. Checked items are backed by the current routes and
test suite; they are not promises that the deployment has completed production
qualification. The rendering, persistence, and deployment contracts described
elsewhere in this README and in the [application design map](docs/application-design-map.md)
remain unchanged.

### Implemented MVP capabilities

- [x] Editable seeded script, TTS cleanup, safe chunking, Chatterbox synthesis,
  durable background jobs, resumable completed chunks, progress polling, final
  MP3 download, and podcast loudness mastering.
- [x] Persistent multi-host cast profiles with reference-voice upload,
  discovery, playback, preview, and per-host synthesis controls.
- [x] Complete saved-episode documents, saved-episode selection/restoration,
  and a persistent render queue that provides the current episode/job history.
- [x] Intro audio and multi-host spoken-open controls, including mixing speech
  over the intro or placing it after the track.
- [x] Structured, multi-story research packets with per-story sources and
  uncertainty, save/list/load routes, a resumable packet editor, and drafting
  from either a packet or quick source notes. Loading another packet warns
  before replacing packet edits.
- [x] Conversation drafting and audio generation, with generated dialogue
  returned to the script editor for producer review before rendering.
- [x] Unsaved-work protection for episode, cast, research, show-open, image
  prompt, and script edits when leaving the page or replacing work with a saved
  episode or AI draft. Successful save/load snapshots reset the warning.
- [x] Social-clip suggestion, custom-render, artwork, and download API routes,
  plus automatic clip rendering that does not change a completed episode to a
  failed render.
- [x] Automated checks for core route round trips, render recovery and chunk
  reuse, saved data, research-packet interactions, unsaved-work warnings,
  accessibility wiring, responsive workflow structure, and a mocked primary
  producer journey.

### MVP release blockers

- [ ] **Make Clip Studio reachable in the real browser journey.** Acceptance:
  after a completed job is returned or selected, the Distribution region is
  visible and keyboard-reachable; the producer can request suggestions, apply
  one, render a custom clip, and follow the returned MP4 download link in a
  browser-level test. The existing clip APIs and automatic-render behavior
  remain unchanged.
- [ ] **Qualify the supported deployment modes.** Acceptance: on the intended
  release hosts, the documented CPU startup completes its readiness checks and
  produces a downloadable MP3; the documented NVIDIA startup fails closed when
  CUDA is unavailable and, on the target GPU host, reports the requested CUDA
  device, passes synthesis readiness, and produces a downloadable MP3. Record
  the tested OS, Docker/Compose, GPU/driver (when applicable), commands, and
  results without weakening the existing device-safety contract.
- [ ] **Complete primary-journey release regression coverage.** Acceptance: an
  automated browser test exercises load or create episode → configure cast →
  resume or save research → draft/edit → save → submit → observe queue progress
  → download MP3 → open Clip Studio → render/download MP4, using controlled
  provider boundaries and verifying recoverable-error states without requiring
  live model or network access.
- [ ] **Publish release documentation.** Acceptance: a release runbook names
  prerequisites, supported CPU/GPU modes, configuration and secret handling,
  storage/backup expectations, startup/readiness checks, the producer workflow,
  known limitations, troubleshooting and rollback steps, and the exact release
  validation commands; every internal link and command is checked against the
  release candidate.

### MVP hardening and usability

- [ ] **Verify accessibility beyond source-level checks.** Acceptance: keyboard
  operation, focus order/visibility, labels and descriptions, validation focus,
  live-region announcements, contrast, zoom/reflow, and a representative screen
  reader journey are manually or browser-automation audited; release-blocking
  findings are resolved and the audit environment/results are recorded.
- [ ] Exercise the resumable research-packet UI and unsaved-work protection in
  the same real-browser coverage used for release qualification, including
  canceling a replacement, confirming a replacement, leaving dirty work, and
  confirming that a successful save/load establishes a clean snapshot.
- [ ] Add producer-facing empty, loading, success, and recovery guidance where
  deployment qualification or accessibility review finds gaps; retain entered
  form data on recoverable failures.
- [ ] Document and rehearse file-backed data backup/restore and interrupted-job
  recovery on release-like storage, without changing persisted formats or job
  recovery semantics.

### Explicit post-MVP features

These items are outside the release boundary unless product ownership
explicitly promotes them:

- [ ] Regenerate one failed or unsatisfactory speech chunk from the UI.
- [ ] Outro assets and a structured narration/music/sound-effect cue system.
- [ ] Script and saved-episode revision history.
- [ ] User roles, editorial review, and approval workflows.
- [ ] Podcast-host or social-platform publishing integrations.
- [ ] Automated research ingestion, US-politics and North-Carolina topic
  shortlists, and an independent fact-check/source-validation stage.
- [ ] Automated show notes and expanded distribution metadata/templates.

## Repository creation

The project is already initialized as a local Git repository. To create the GitHub repository with GitHub CLI:

```bash
gh repo create patch-notes-america --private --source=. --remote=origin --push
```

Use `--public` instead if you want the source public.

## Multi-host episodes

The application supports any number of hosts in a single episode. Use **+ Add host** in the web UI to add cast members. Each host has independent settings for:

- Display/name used in the script
- Stable host ID and optional uploaded reference voice
- Exaggeration and CFG weight (plus advanced Chatterbox controls)
- Tempo

Switch speakers by putting the configured host name on its own line inside square brackets:

```text
[Major Patchnotes]
Welcome back to Patch Notes: America.

[Alex]
And today we have a lot to talk about.

[Sam]
Starting with the story everyone is arguing about this morning.

[Major Patchnotes]
Oh good. Nothing ever goes wrong after a sentence like that.
```

Speaker names are matched exactly and every configured host must have a unique name. Once resolved, rendering carries the stable host ID and its voice profile. Text before the first speaker tag is assigned to the first host, which keeps older single-host scripts compatible.

Generated chunk filenames include the speaker name, for example:

```text
chunks/chunk-001-major-patchnotes.wav
chunks/chunk-002-alex.wav
chunks/chunk-003-sam.wav
```

`metadata.json` also records the host, voice, tempo, Chatterbox sampling controls, and text used for every generated chunk.

## Persistent host personalities

Host entries are now persistent character profiles rather than audio-only settings. The default cast is stored in `config/host_profiles.json` and is mounted into the application container so edits survive rebuilds and restarts.

The seeded cast contains three deliberately different podcast personalities:

- **Wade Mercer — Southern Everyman:** practical, warm, skeptical, dry, and focused on what a story means in ordinary life. Southern without being written as a caricature.
- **Marcus Reed — City Pragmatist:** fast, analytical, culturally plugged-in, media-aware, and willing to challenge weak logic or a convenient headline.
- **Julian Cross — Worldly Context Guy:** composed, internationally minded, historically aware, and responsible for widening the frame beyond the immediate American political argument.

Every profile supports:

```text
name
id
role
Chatterbox voice
reference_audio_path
reference_audio_filename
exaggeration
cfg_weight
tempo
traits
debate style
humor style
interruption frequency
sentence / speaking style
political posture
character flaws
character notes
```

Use **Save cast** in the web interface to persist profile edits. The API is also available directly:

```text
GET  /api/host-profiles
POST /api/host-profiles
POST /api/hosts/{host_id}/voice
GET  /api/hosts/{host_id}/voice/audio
POST /api/hosts/{host_id}/voice/preview
DELETE /api/hosts/{host_id}/voice
```

Episode `metadata.json` includes the complete host profiles used for that episode. This is intentional: a future transcript-generation stage can consume the exact same profile objects, keeping character voice and behavior consistent with the cast used for audio rendering.

### Character design rule

The profiles define behavior, perspective, conversational rhythm, and flaws—not impersonations of real podcasters. The goal is to capture traits that make hosts engaging while keeping the characters original.

## Conversation engine

The app can turn a verified story/source packet into an editable, personality-aware multi-host discussion before audio rendering.

1. Configure and save any number of host profiles.
2. Paste verified story facts, source summaries, useful quotes, uncertainty notes, and desired angles into **Story / source notes**.
3. Choose the desired spoken length and tone.
4. Select **Draft conversation**.
5. Review/edit the generated speaker-tagged script.
6. Generate the episode through Chatterbox as usual.

The generator uses every host's role, traits, debate style, humor style, interruption tendency, speaking style, political posture, flaws, and character notes. It explicitly instructs the model not to imitate real-world podcast personalities and not to invent facts beyond the supplied source packet.

### AI configuration

Copy `.env.example` to `.env` and supply an API key:

```bash
cp .env.example .env
```

Then edit:

```text
OPENAI_API_KEY=your_key_here
OPENAI_MODEL=gpt-5.6-luna
OPENAI_IMAGE_MODEL=gpt-image-2
```

The implementation uses the OpenAI Responses API for conversation drafting and the Images API for clip artwork and the standalone **Image Studio** tab. `OPENAI_RESPONSES_URL`, `OPENAI_MODEL`, `OPENAI_IMAGES_URL`, and `OPENAI_IMAGE_MODEL` are environment-configurable. Image Studio returns a downloadable PNG without attaching it to an episode or saving it on the server. Its Creation activity panel shows elapsed seconds, submitted format and prompt length, HTTP response status, received PNG size, and completion or failure details. A notice appears after a minute while waiting; the provider does not expose internal generation stages or a completion percentage through this request. Activity resets on each new request and stays visible after completion or failure. If image generation is unavailable, the MP4 still renders with the existing branded background and reports the artwork error separately.

### Hosted image quality and cost

`OPENAI_IMAGE_QUALITY=high` applies consistently to Image Studio, clip artwork
and new Video Studio projects. Supported choices are `high`, `medium`, `low`;
invalid settings fail explicitly. Image Studio and Video Studio expose hosted
quality and local preset controls; clip artwork uses the environment defaults,
without changing saved episodes. OpenAI requests use 1024×1024, 1536×1024 or
1024×1536 and send no local preset/seed. Video scenes currently request landscape
images, cropped for the selected export format. Hosted High generally costs
more and takes longer than Low; pricing depends on the configured model, size,
quality, input and current provider rates. Check the provider pricing before
large batches; retries/regeneration are additional paid calls. Local generation
has no hosted per-image charge but uses GPU time, electricity, RAM and storage.

### Generate artwork locally on NVIDIA

The local service provides two explicit profiles targeting the **12 GB RTX
3060**. This is a memory-budget design, not a hardware benchmark; 6/8 GB cards
are not qualified. SDXL Base has roughly 3.5 billion parameters across its UNet,
text encoders and VAE: fp16 weights alone are about 7 GB (decimal), before
latents, classifier-free-guidance activations, CUDA workspace and VAE decode.
Full CUDA placement can approach or exceed the usable 12 GB at native resolution,
especially alongside speech. Both presets require full CUDA placement: CPU
offload is prohibited. VAE slicing/tiling and one loaded image model at a time
reduce VRAM pressure. Model loading, tokenization and PNG encoding still use
host CPU/RAM; neural image generation stays on the GPU. Exact peak RAM/VRAM
must be measured on the target host; select Draft when Detail cannot fit.

| Profile | Model | Steps / guidance | Actual square / landscape / portrait PNG |
| --- | --- | --- | --- |
| Draft | `stabilityai/sdxl-turbo` | 2 / 0 | 512×512 / 768×512 / 512×768 |
| Detail | `stabilityai/stable-diffusion-xl-base-1.0` | 30 / 5 | 1024×1024 / 1152×768 / 768×1152 |

Detail uses Base's model-configured scheduler, fp16 safetensors, and its VAE's
configured precision/upcast behavior, rather than applying Turbo's zero guidance
and two steps to new weights. Only these model families are supported; arbitrary
model substitutions fail explicitly. `LOCAL_IMAGE_STEPS` accepts 1–4 for Turbo;
`LOCAL_IMAGE_DETAIL_STEPS` accepts 20–50 and `LOCAL_IMAGE_DETAIL_GUIDANCE` 1–10.
The app's size field is a provider aspect request; local PNGs have the actual
sizes above. FFmpeg scaling to 1080p adds no facial detail.

Configure `.env`:

```text
CLIP_IMAGE_PROVIDER=local
LOCAL_IMAGE_PRESET=detail
LOCAL_IMAGE_MODEL=stabilityai/sdxl-turbo
LOCAL_IMAGE_STEPS=2
LOCAL_IMAGE_CPU_OFFLOAD=false
LOCAL_IMAGE_DETAIL_MODEL=stabilityai/stable-diffusion-xl-base-1.0
LOCAL_IMAGE_DETAIL_STEPS=30
LOCAL_IMAGE_DETAIL_GUIDANCE=5.0
LOCAL_IMAGE_DETAIL_CPU_OFFLOAD=false
LOCAL_IMAGE_MAX_PROMPT_CHARS=4000
```

Start/rebuild with `./scripts/start-and-check.sh --gpu --local-image`.
Weights persist in `imagegen-models`. First downloads may take minutes. The
service starts parked with handoffs enabled; model load requires a real CUDA
image smoke test. Video Studio leases load the storyboard's requested preset
directly, so a Draft storyboard does not first load and test Detail. Legacy
load requests without a preset use the last selected profile (initially
`LOCAL_IMAGE_PRESET`, Detail by default). Switching profiles and unloading remove
offload hooks, release model references, collect garbage and clear the CUDA
cache before another model loads. The requested model is smoke-tested at native
square resolution with its actual step/guidance settings. Failed smoke tests
also release the loaded model. Rebuild both app and imagegen for this control
request change; stored projects and assets need no migration.

Existing `.env` files must set both `LOCAL_IMAGE_CPU_OFFLOAD=false` and
`LOCAL_IMAGE_DETAIL_CPU_OFFLOAD=false`. Enabled offload settings now fail
readiness with a configuration error, including parked startup. Model placement
validates UNet, VAE and text-encoder parameters/buffers on CUDA; insufficient
VRAM fails instead of moving weights to CPU. Peak memory remains hardware-dependent.
Failed loading or smoke testing leaves inference unready. Keep handoffs enabled
to park speech during local image work. The legacy full Compose configuration
has no automatic handoffs; manage model contention explicitly there.

Draft is the faster preview option. Detail's 30 steps, larger latents,
model loads and full-resolution smoke tests can make requests take
minutes; no measured 3060 latency or peak-memory figures are available here.
The app allows a 600-second provider timeout. Each request is serialized.
If Detail runs out of VRAM, select Draft explicitly rather than expecting a
CPU offload. Both offload settings must remain false. Faces/hands can still contain artifacts; inspect results and retry with
clear lighting, framing, fictional subjects and fewer people. Neither model nor
High hosted quality guarantees perfect faces.

The default prompt limit accommodates the application's safety framing plus
the full 2,000-character producer direction. If customized, keep
`LOCAL_IMAGE_MAX_PROMPT_CHARS` at `4000` or higher so valid episode image
prompts are not rejected with HTTP 422.

The local service powers both clip artwork and Image Studio and is not published to the host network. The app calls its
OpenAI-compatible endpoint over the Compose network. `OPENAI_API_KEY` remains
optional for clip art when `CLIP_IMAGE_PROVIDER=local`; it is still required
if conversation drafting uses the OpenAI provider.

If a clip falls back to the branded background because the local artwork
service is unreachable, start or recreate the complete CUDA stack with
`./scripts/start-and-check.sh --gpu --local-image`. Starting only the app and
Chatterbox while `CLIP_IMAGE_PROVIDER=local` leaves the profiled image service
unavailable.

### Run the conversation engine locally

An optional Ollama profile can replace paid conversation calls while leaving
prompt construction, speaker validation, and audio generation unchanged:

```bash
docker compose --profile local-ai up -d ollama
docker compose exec ollama ollama pull llama3.1:8b
```

Set the following in `.env`, then restart the app:

```text
CONVERSATION_PROVIDER=local
LOCAL_AI_URL=http://ollama:11434/api/chat
LOCAL_AI_MODEL=llama3.1:8b
```

The model remains on the named `ollama-data` volume. Choose a smaller model for
CPU-only or low-memory hosts, or a larger model when adequate GPU/RAM is
available. Keep the default `openai` provider when quality or latency from the
local host is insufficient.

### Conversation API

`POST /api/conversation-draft`

Form fields:

- `story_notes` — verified source/story packet
- `hosts_json` — current cast profiles
- `target_minutes` — 2 through 60
- `tone` — free-form writing direction

The response returns a speaker-tagged script that is validated against the configured cast before being sent back to the editor.

## Episode research packets

The conversation engine now supports multi-story research packets. Each story records a headline, importance, verified facts, disputed/uncertain claims, discussion angles, and source references. Packets can be saved under `config/research_packets/`, selected and loaded back into the packet editor, and used to draft a complete episode. The saved-packet status reports loading, empty, success, and failure states; loading warns before it replaces unsaved packet edits. Saving refreshes the list and keeps the new packet selected. The generator is instructed to cover every story, weight high-importance stories more heavily, preserve uncertainty, use natural transitions/callbacks, and never read source URLs aloud.

Malformed stored packet files are omitted from the collection response. Requesting a malformed packet directly returns HTTP `422` without rewriting the file; valid stored objects are returned with all existing fields intact for compatibility.

API endpoints: `POST /api/research-packets`, `GET /api/research-packets`, `GET /api/research-packets/{id}`, and `POST /api/conversation-draft-packet`.

## Social Clip Studio

After an episode is rendered, the app exposes a Clip Studio for short-form distribution.

- Audio completes independently of visuals by default. Set `AUTO_SOCIAL_CLIPS=true` to retain the legacy automatic three-clip workflow; custom Clip Studio remains available.

## Video Studio

For one-click production, select a finished episode and click **Auto-create video
with local AI**. If no storyboard is loaded, it creates one first. Otherwise it
uses the loaded project and selected export range. The queued job plans
unassigned, unlocked scenes with Ollama, chooses free B-roll from the selected
provider, downloads selected footage, generates images using the storyboard
image provider, and renders.
Existing selected media and locked scenes are preserved. Image and planner model
stages run sequentially through the existing GPU handoffs.

Automatic selection requires a configured `PIXABAY_API_KEY` or `PEXELS_API_KEY`
and the configured `VIDEO_PLANNER_MODEL` installed in Ollama. After starting the
GPU/image stack, start the optional local planner with the GPU override:

```bash
docker compose -f docker-compose.yml -f docker-compose.gpu.yml --profile local-ai up -d ollama
docker compose exec ollama ollama pull llama3.1:8b
```

Replace `llama3.1:8b` with your configured model if different. Run these after
the startup script, which recreates its selected services. Use `sudo` if Docker
requires it on your host. The app reports missing keys or unavailable Ollama
without using a paid model.

The AI chooses from provider metadata (tags, source URLs, duration, dimensions);
it does not inspect video frames. Automatic search first tries the scene query
and its shorter form, then asks local AI for up to three distinct, concrete
search phrases when those fail. Each phrase searches up to two pages of 30
results; short pages stop pagination. Safe clips long enough for the scene
are ranked in batches of ten, allowing related settings and activities to
illustrate abstract or named-event passages without claiming actual event footage.
Only chosen footage is downloaded. Unseen clips are ranked first, with reused
clips considered if those do not match; repeated choices reuse their download. Missing
matches can become generated images, with notes on the job. If no B-roll can
be found for the range, the job stops with an actionable error rather than
producing an image-only video. Planning batches and selected clips are
checkpointed for restart recovery. Automatic planning requests a scene-specific JSON
schema and retries a malformed batch once before reporting an error; invalid
batches never replace scene choices. Review the completed video before publishing.

Choose a finished episode in **Video Studio**, create or load a storyboard,
and choose **Create an image** or **Find free B-roll** for every scene.
Search footage using the scene's quick search, preview candidates or open their
source pages, then click **Use this B-roll** to prepare that selection without
downloading it. Import MP4/MOV/WebM footage with its source,
creator and license record. Choose **OpenAI** or **Local** for scene images in
the storyboard. OpenAI uses
the configured hosted endpoint/key and quality; Local uses the CUDA service and
Draft/Detail preset. Only local image requests acquire an image GPU lease.
Both B-roll and images are required in an export range. Crop focal points,
footage trims and gentle image zooms are editable. Source podcast audio is
hashed and left unchanged; source changes require a new video project.

For long episodes, review five collapsible scene cards at a time. Filter to
**Needs attention**, **B-roll**, or **Images**, or jump directly to a scene
number. Navigation retains edits across pages; **Save storyboard** persists all
scenes. The readiness summary explains missing footage or locked image scenes.
Image scenes with no asset are valid plans: their images generate during the
build. The selected export range must include both kinds of visuals.

The build button and live feedback stay together in a sticky bar. Validation
errors appear there and open the relevant scene when identified locally. New
storyboards default to images, so choose and select B-roll before building.
Job progress, errors and downloads appear above the scene editor. GPU diagnostics
are optional and cannot block job polling. If a build is accepted but refreshing
status fails, the confirmation keeps its job ID and polling retries.

For manual control, click **Generate assets from my scene choices** when the storyboard is ready. One
durable job downloads selected footage, generates missing images with the selected provider,
and renders the selected range after all storyboard assets are ready. Existing
assets are reused. Progress shows the current stage and cancellation works
during preparation and rendering. Restart recovery retains each completed asset.
The queued storyboard is frozen; later edits affect future jobs. Prepared media
is also saved back to the editor if its revision has not changed. Reload the
project after completion to inspect those assets. Preview/export buttons remain
available for already-prepared media.

Projects and assets live under `data/video/`, covered by the existing data mount.
`VIDEO_DIR` can override this root. Project schema version 1 and optimistic
revision checks prevent lost edits. New audio metadata includes an additive
`video_timeline` covering music and mixed intros; legacy utterance timing remains
readable. Existing saved episodes, voice identities and MP3 paths do not change.

Exports cover any selected range or the full episode, with a half-resolution preview
and a horizontal 1080p export option. Local image quality depends on the installed
model's native resolution; the export dimensions do not add image detail.
Start local images using the documented `--gpu --local-image` mode and verify
that CUDA is actually visible. This change has not been benchmarked on a 3060.
- Measures every rendered TTS chunk with `ffprobe` and stores precise start/end timestamps in episode metadata.
- Scores 20–60 second candidate moments, favoring strong hooks, questions, conversational turns, and multiple speakers.
- Lets you override the suggested window with exact start/end times.
- Exports vertical 9:16, square 1:1, or horizontal 16:9 MP4.
- Burns each spoken passage once as a synchronized speaker caption, without a second pull-quote layer that repeats and overlaps the same text.
- Generates a hyper-realistic editorial image with lifelike fictional people when relevant through either OpenAI Images or the optional local SDXL Base/Turbo service, stores it beside the MP4, and uses it as the video background behind branding and captions.
- Applies the optional saved **Episode image prompt** to automatic and custom clip artwork without replacing the built-in visual-safety and no-text constraints.
- Falls back to the existing dark branded background if image generation is unavailable; artwork errors do not fail an otherwise valid clip.
- Adds Patch Notes: America branding and a custom clip headline.
- Saves exports under `output/<episode>/clips/`.

Clip APIs:

```text
GET  /api/episodes/{episode_slug}/clip-suggestions
POST /api/episodes/{episode_slug}/clips
GET  /api/episodes/{episode_slug}/clips/{clip_id}/download
```

Automatic clip failure is recorded separately and never changes a successfully assembled episode back to failed. The clip-selection logic is deliberately local and deterministic for MVP, so it works without additional AI calls. A later ranking layer can use the transcript, story importance, audience metrics, or a model to improve viral-moment selection.

Video exports run as durable jobs. Closing the browser does not stop them. The
Video jobs panel shows actual preparation counts, phase and errors, with Cancel
controls for active work. App restarts requeue interrupted exports; cancelled jobs
stay cancelled. Validated rendered scenes are cached by their source and filter
inputs so retries reuse completed work. Each export freezes its project revision.
FFmpeg process groups are terminated on cancellation, leaving reusable media.
Exports include burned phrase captions, SRT, chapters and a credits file. Caption
timing inside an utterance is approximate; it is not word-level forced alignment.

### Local automation and GPU ownership

**Suggest visuals locally** calls only the configured Ollama endpoint, in small
scene batches. Locked scenes and timestamps are preserved; invalid output or a
concurrent edit leaves the saved project unchanged. Install `VIDEO_PLANNER_MODEL`
(default `llama3.1:8b`) in the optional `local-ai` service. Manual scene editing
works without a planner. **Generate missing images** queues a durable cancellable
batch for unlocked scenes whose visual source is image; B-roll scenes are
never converted to images by this action.

Set `PIXABAY_API_KEY` or an existing `PEXELS_API_KEY` in your local `.env` for
producer-triggered searches. Ten candidates are returned per query, searches are
cached for 24 hours, and only chosen footage is downloaded. Pexels currently
pauses new API keys; manual import remains available. Source/creator/license
links are kept with selected assets. Prepared selections retain a trusted copy
of the chosen candidate independently of search-cache refreshes. Unselected
candidates are not downloaded by the asset queue. No scraping or bulk stock
downloads are used.

Modern Compose enables `GPU_MODEL_HANDOFF=true`. The app creates a private
`data/video/model-control.key` (never commit it). Speech and image services read
that key through a read-only `/control` mount. A local GPU lease unloads idle
speech/image/planner models, loads the required model, and restores speech with
its real synthesis smoke test after visual work. The image service starts parked
after validating CUDA; inference is unavailable until its load/smoke test passes.
The resource panel reports the active stage and last observed free VRAM. These
controls are authenticated and refuse to unload active inference. No Docker
socket is exposed to the app. Set handoff false only when managing model memory
yourself; the legacy full Compose file does not configure automatic handoff.

Busy model-control responses (HTTP 409) are retried once per second for up to
120 seconds so cancelled inference can finish before models are unloaded or
speech is restored. The existing model-load request timeout still applies to
loading and smoke inference. If image cleanup remains busy, speech is not loaded
alongside it; the error requests a service health/log check. A health check can
recover speech left parked by an interrupted handoff when no GPU lease is active:
it unloads other models and requires Chatterbox's real synthesis smoke test.
Actual smoke-test failures remain unready and are not automatically retried by
health checks. Audio submission remains disabled until synthesis is verified.
Speech-provider HTTP failures retain their synthesis diagnostics and the existing
chunk retry policy, including fresh seeds for retryable 5xx responses. They are
not relabeled as GPU handoff failures; model-control and restoration failures
still report the affected service and handoff operation.
The short readiness probe retains its 30-second maximum duration. Episode
speech chunks allow up to 60 seconds before speed adjustment; a 40-second chunk
does not fail solely because it exceeds the smoke probe's limit. Both paths
retain the same static, clipping, non-finite, constant-tone and repetition checks.

Rebuild app, chatterbox and imagegen together when adopting these controls.
GPU/CPU Compose choices remain explicit and failed CUDA validation never falls
back silently. GPU hardware/memory performance still requires target-host testing.

If a video job reports **GPU model handoff failed**, its error identifies the
service, operation and HTTP status. HTTP 401/403 from Chatterbox/imagegen indicates model-control
authorization: verify that app, chatterbox and imagegen read the same
`model-control.key` through their existing mounts (do not print the key).
HTTP 404 suggests an outdated service or incorrect endpoint/model configuration;
HTTP 503 requires checking that service's readiness and model-load logs.
For Ollama authorization errors, check its endpoint/access settings.
Connection failures and timeouts identify the affected service as well. If
restoring speech also fails, the message retains an earlier HTTP failure when
available. These diagnostics do not disable authentication, GPU checks, or
model restoration.

For a read-only readiness/mount/authentication check on the deployment host:

```bash
python3 scripts/check-image-handoff.py
```

It checks all three running containers, reports readiness and the control-file
mount sources, and compares key fingerprints internally without printing keys
or fingerprints. A parked image model with CUDA visible is acceptable before a
lease; this check does not load models or claim smoke inference succeeded. The
command exits nonzero for missing/unreadable/mismatched keys, different control
mount sources, failed readiness or Docker access. To check another Compose stack,
repeat `--compose-file PATH` for each file used to start it. Legacy full Compose
has no automatic handoff/control mounts and will not pass this modern-stack
check. Use the reported service/HTTP operation plus service logs to distinguish
authorization (401/403) from model readiness (503), stale endpoints (404), or
cancelled inference still finishing (409). Do not delete or print the control key.

Health endpoints return JSON even for a parked or unready model; HTTP 200 alone
does not prove CUDA/model readiness. Read their `ok`, `parked`, `cuda_visible`,
`synthesis_ready` and `failure` fields as applicable. Filter container logs to
avoid losing the handoff error among repeated health checks:

```bash
docker compose logs --since=1h chatterbox imagegen ollama | grep -Ei '/models/|403|404|503|error|exception|out of memory'
```

Use `sudo` if required by your Docker setup. Rebuild app to receive the detailed
messages; rebuilding all inference services is necessary only when their code
or model-control endpoints are outdated. Leave `GPU_MODEL_HANDOFF` enabled while
diagnosing a failure so multiple models do not compete for the 3060's memory.

### Formats, image presets, and encoding

Choose horizontal 16:9 (1920×1080), vertical 9:16 (1080×1920), or square
(1080×1080) from the same saved storyboard. Set an excerpt range for social
clips, or use the full duration for an episode. Each format has independent
scene crop focal points. Split scenes while preserving source trims; merge
requires the same media and continuous B-roll trims. Locked scenes must be
unlocked before those edits. Export all three formats queues independent jobs.

New storyboards use `CLIP_IMAGE_PROVIDER`, `OPENAI_IMAGE_QUALITY` and
`LOCAL_IMAGE_PRESET` (default Detail). Projects without additive `image_provider`
retain Local, and projects without `image_preset` retain Draft, preserving legacy
behavior; missing `image_quality` uses the configured hosted quality. These
fields round-trip in version 1; no migration rewrites stored projects. Existing
assets are reused until explicitly regenerated. Hosted seeds are unsupported
and recorded as null; local seeds and actual model/revision are recorded with
assets. Pin `LOCAL_IMAGE_MODEL_REVISION` (Draft) and
`LOCAL_IMAGE_DETAIL_MODEL_REVISION` (Detail) for reproducibility and review model
licenses. Free local inference does not imply unrestricted commercial licensing.

Software H.264 remains the default encoder. NVIDIA H.264 is an explicit option;
it requires the GPU Compose override, a working driver/toolkit, and FFmpeg NVENC
support. The override grants the app `video,utility` capabilities while inference
services retain their compute capability. Failed NVENC jobs report the failure;
choose software explicitly rather than silently falling back. Exports use H.264
baseline/yuv420p, AAC and fast-start MP4. Caption presets include standard, large
and SRT-only. Word-level forced alignment is not included: phrase timing remains
approximate within an utterance, while the actual mixed-intro speech duration
and episode pauses are preserved.

After updating, rebuild the services with `./scripts/start-and-check.sh --gpu
--local-image` (one shell line). To use local planning, also enable the `local-ai`
profile and install `VIDEO_PLANNER_MODEL` in Ollama. Add free provider API keys
only to your local `.env`; downloaded/imported footage works without them.
