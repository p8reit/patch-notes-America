# Patch Notes: America

Initial podcast-production application for turning a narration script into a finished MP3 using a locally hosted Chatterbox server.

## MVP features

- Browser-based episode editor on port `8081`
- Complete editable episode saves, including the cast, research packet, source notes, script, conversation settings, show-open settings, and intro audio
- Preloaded pilot narration script
- Per-host Chatterbox reference voice, tempo, exaggeration, CFG weight, temperature, Min P, Top P, and repetition penalty
- Optional uploaded intro track with multi-host spoken lines mixed over the music or played after it
- Automatic TTS-friendly script cleanup
- Automatic script chunking (default max 700 characters)
- Durable speech-chunk jobs with progress polling and resumable audio
- Persistent background render queue designed for slow CPU-only generation
- Chatterbox generation through the bundled service's `POST /v1/audio/speech` API
- Per-chunk WAV files retained for selective regeneration/debugging
- FFmpeg assembly into a final 128 kbps MP3
- Persistent `episodes/` source scripts and `output/` generated episodes
- Health endpoint showing whether the app can reach Chatterbox
- Basic unit tests and GitHub Actions CI

## Architecture

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

The top status card should show:

```text
App ready · Chatterbox online
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
`chatterbox-models` volume. Chatterbox is configured for NVIDIA GPU inference:
Compose reserves the host GPUs for the container and defaults
`CHATTERBOX_DEVICE` to `gpu`. The service translates `gpu` to PyTorch's `cuda`
device name internally. The host must have the NVIDIA driver, NVIDIA
Container Toolkit, and Docker configured with the NVIDIA runtime. Confirm that
Docker can see the GPU before starting the service:

```bash
docker run --rm --gpus all nvidia/cuda:12.4.1-base-ubuntu22.04 nvidia-smi
```

Rebuild and recreate Chatterbox after pulling this configuration change, then
verify both the container's CUDA access and the device reported by the service:

```bash
docker compose up -d --build --force-recreate chatterbox
docker compose exec chatterbox python -c \
  'import torch; available = torch.cuda.is_available(); print("CUDA available:", available); print("GPU:", torch.cuda.get_device_name(0) if available else "none"); print("Torch CUDA:", torch.version.cuda)'
curl --fail http://127.0.0.1:8000/health
```

The CUDA check should print `True` and the GPU name; the health response should
contain `"device":"cuda"`, `"cuda_available":true`, and the GPU name. Compose
uses the NVIDIA runtime explicitly, exposes all GPUs, and enables the driver's
compute and utility capabilities. To deliberately run without a GPU, remove
the GPU device reservation and `runtime: nvidia` from a local Compose override,
then set `CHATTERBOX_DEVICE=cpu`. The default 600-second request timeout
accommodates model startup.

If PyTorch reports a CUDA build (for example, `Torch CUDA: 12.4`) but
`torch.cuda.is_available()` is `False`, the image has CUDA-enabled PyTorch but
the running container cannot reach the host driver. Do not call
`torch.cuda.get_device_name()` unless availability is `True`; doing so produces
the `Found no NVIDIA driver` traceback without adding diagnostic information.
First verify the driver on the host, then recreate the container rather than
only restarting it and inspect Docker's GPU request:

```bash
nvidia-smi
docker compose up -d --build --force-recreate chatterbox
docker inspect "$(docker compose ps -q chatterbox)" \
  --format '{{json .HostConfig.DeviceRequests}} {{.HostConfig.Runtime}}'
docker compose exec chatterbox python -c \
  'import torch; print(torch.cuda.is_available(), torch.cuda.get_device_name(0) if torch.cuda.is_available() else "none")'
```

The host command must display the GPU and driver version. The inspection output
should include the `nvidia` driver, the `gpu` capability, and the `nvidia`
runtime. If the host check fails, install or repair the host NVIDIA driver. If
only the container checks fail, reinstall or configure NVIDIA Container Toolkit
before recreating the service.

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

To create a show open, select an **Intro track** and add **Host intro lines** using
the same `[Host Name]` speaker tags as the main script. Leave **Play the host
intro over the music** selected to start both together, and use **Music volume
under voice** to keep the speech clear. The complete track is retained, and the
episode starts after both the music and spoken intro finish. Clear the overlap
option to play the full track before the host introduction instead.

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

The queue uses one worker by default so concurrent episodes do not compete for
all CPU and memory. After submitting an episode, it is safe to close the
browser: open the app later and the **Render queue** lists active and completed
jobs with download links. Queued or running manifests are automatically
recovered after an app/container restart; interrupted renders restart from the
last unfinished chunk. Completed WAV files are validated before reuse, avoiding
the loss of hours of CPU rendering while preventing corrupt partial files from
entering the final episode.

Transient TTS container crashes and dropped connections are retried five times
by default with an increasing delay. Configure `TTS_MAX_ATTEMPTS` and
`TTS_RETRY_DELAY_SECONDS` for a backend that takes longer to restart. Each
attempt and each completed chunk is written to `job.json`, so the Render queue
continues to show useful progress during a slow CPU render.

Set `JOB_WORKERS` above `1` only for a GPU-backed TTS service or a host known to
have enough capacity. Run one Uvicorn application worker because the queue is
process-local; multiple Uvicorn workers would each create a queue consumer.

```text
POST /api/generation-jobs
GET  /api/generation-jobs
GET  /api/generation-jobs/{job_id}
```

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

### Phase 1 — audio MVP

- [x] Script editor
- [x] Script chunking
- [x] Chatterbox integration
- [x] MP3 assembly
- [x] Pilot seed script
- [ ] Voice browser/preview
- [ ] Regenerate one failed/bad chunk
- [ ] Episode history page
- [ ] Add intro/outro assets
- [ ] Add sound-effect cue system
- [ ] Loudness target suitable for podcast publishing

### Phase 2 — production workflow

- [ ] Structured episode format with narration/SFX/music cues
- [ ] Source/reference metadata per political story
- [ ] Script revision history
- [ ] Approval state before publishing
- [ ] Automated show notes
- [ ] Artwork and episode metadata

### Phase 3 — weekly automation

- [ ] Research ingestion
- [ ] US politics topic shortlist
- [ ] North Carolina topic shortlist
- [ ] Fact-check/source validation stage
- [ ] Draft generation
- [ ] Audio generation
- [ ] Human approval
- [ ] Podcast-host publishing integration

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
```

The implementation uses the OpenAI Responses API by default. `OPENAI_RESPONSES_URL` and `OPENAI_MODEL` are environment-configurable so this layer can be swapped or proxied later without changing the podcast/audio pipeline.

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

The conversation engine now supports multi-story research packets. Each story records a headline, importance, verified facts, disputed/uncertain claims, discussion angles, and source references. Packets can be saved under `config/research_packets/` and used to draft a complete episode. The generator is instructed to cover every story, weight high-importance stories more heavily, preserve uncertainty, use natural transitions/callbacks, and never read source URLs aloud.

API endpoints: `POST /api/research-packets`, `GET /api/research-packets`, `GET /api/research-packets/{id}`, and `POST /api/conversation-draft-packet`.

## Social Clip Studio

After an episode is rendered, the app exposes a Clip Studio for short-form distribution.

- Measures every rendered TTS chunk with `ffprobe` and stores precise start/end timestamps in episode metadata.
- Scores 20–60 second candidate moments, favoring strong hooks, questions, conversational turns, and multiple speakers.
- Lets you override the suggested window with exact start/end times.
- Exports vertical 9:16, square 1:1, or horizontal 16:9 MP4.
- Burns synchronized speaker captions directly into the video.
- Adds Patch Notes: America branding and a custom clip headline.
- Saves exports under `output/<episode>/clips/`.

Clip APIs:

```text
GET  /api/episodes/{episode_slug}/clip-suggestions
POST /api/episodes/{episode_slug}/clips
GET  /api/episodes/{episode_slug}/clips/{clip_id}/download
```

The clip-selection logic is deliberately local and deterministic for MVP, so it works without additional AI calls. A later ranking layer can use the transcript, story importance, audience metrics, or a model to improve viral-moment selection.
