# Patch Notes: America

Initial podcast-production application for turning a narration script into a finished MP3 using a locally hosted KokoroTTS server.

## MVP features

- Browser-based episode editor on port `8081`
- Preloaded pilot narration script
- Configurable Kokoro voice and tempo
- Automatic TTS-friendly script cleanup
- Automatic script chunking (default max 700 characters)
- Durable segment batch jobs with progress polling and per-segment audio
- KokoroTTS generation through the image's `POST /tts/generate` API
- Per-chunk WAV files retained for selective regeneration/debugging
- FFmpeg assembly into a final 128 kbps MP3
- Persistent `episodes/` source scripts and `output/` generated episodes
- Health endpoint showing whether the app can reach Kokoro
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
    +--> KokoroTTS :7860
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

### Start Patch Notes and Kokoro

Kokoro is required, so the default Compose stack always starts it with Patch
Notes. Start both services from the project directory:

```bash
./scripts/start-and-check.sh
```

The app always listens on container port `8080`. Compose publishes it on the
host using `APP_BIND_ADDRESS` and `APP_PORT`, which default to
`127.0.0.1:8081`. The startup script rebuilds and force-recreates the app,
starts Kokoro, waits for the configured health endpoint, and prints logs from
both services on failure. Startup is considered successful only after the app
reports that Kokoro is online, so audio generation is ready when the script
returns.

Open:

```text
http://127.0.0.1:8081
```

The top status card should show:

```text
App ready · Kokoro online
```

Kokoro defaults to `127.0.0.1:7860`. Set `KOKORO_PORT` in `.env` if another
audio service already owns that host port. Communication from Patch Notes to
Kokoro stays on Docker's private network and is not affected by the selected
host port. The **Open KokoroTTS** link uses the browser's current hostname plus
`KOKORO_PORT`, rather than hard-coding `localhost`. Set `KOKORO_PUBLIC_URL`
when Kokoro is published through a different hostname, path, or HTTPS reverse
proxy.

Audio requests use the API contract exposed by the configured
`hangrylabs/kokorotts` image: `text`, `voice`, and `speed` are sent to
`POST /tts/generate`. The endpoint, health path, and per-chunk read timeout can
be overridden with `KOKORO_TTS_PATH`, `KOKORO_HEALTH_PATH`, and
`KOKORO_TIMEOUT_SECONDS`. A response is accepted only when it contains a
WAV/RIFF payload, preventing an API JSON response from being saved and passed
to FFmpeg as audio.

The Kokoro image is currently an `amd64` image. Compose explicitly requests
`linux/amd64`. On an ARM64 Docker host, the startup script registers amd64
binfmt/QEMU support before starting Kokoro; this prevents the repeated
`exec /usr/local/bin/python: exec format error` restart loop. This one-time
registration uses the privileged `tonistiigi/binfmt` installer container.
Docker Desktop users must allow x86/amd64 emulation. `KOKORO_PLATFORM` in
`.env` can be changed when a native ARM64 Kokoro image is available.

## Generate the pilot

1. Open `http://127.0.0.1:8081`.
2. The pilot script is preloaded.
3. Start with voice `am_michael` and tempo `1.00`.
4. Click **Generate episode**.
5. When complete, click **Download MP3**.

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

## Segmented generation jobs

The browser now submits episode renders to `POST /api/generation-jobs` instead
of holding one request open for an entire episode. Speech chunks are grouped
into configurable segments (eight chunks by default). Each segment moves
through `queued`, `running`, `complete`, or `failed`, and writes its own MP3
before the final episode is assembled. Progress survives page/API timeouts in
`output/<job-id>/job.json`.

```text
POST /api/generation-jobs
GET  /api/generation-jobs/{job_id}
```

The job manifest has a media output map per segment. It currently contains
`audio`; a video worker can later add `video` without changing how episodes,
segments, status polling, or retries are represented.

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

This stops both Patch Notes and its required Kokoro service.

## Local development

```bash
python -m venv .venv
```

Linux/macOS/WSL:

```bash
source .venv/bin/activate
pip install -r requirements.txt pytest
KOKORO_URL=http://127.0.0.1:7860 uvicorn app.main:app --reload --port "${APP_PORT:-8081}"
```

Windows PowerShell:

```powershell
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt pytest
pytest -q
$env:KOKORO_URL="http://127.0.0.1:7860"
uvicorn app.main:app --reload --port 8081
```

## Initial roadmap

### Phase 1 — audio MVP

- [x] Script editor
- [x] Script chunking
- [x] Kokoro integration
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
- Kokoro voice
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

Speaker names are matched case-insensitively, but every configured host must have a unique name. Text appearing before the first speaker tag is assigned to the first host, which keeps older single-host scripts compatible.

Generated chunk filenames include the speaker name, for example:

```text
chunks/chunk-001-major-patchnotes.wav
chunks/chunk-002-alex.wav
chunks/chunk-003-sam.wav
```

`metadata.json` also records the host, voice, tempo, and text used for every generated chunk.

## Persistent host personalities

Host entries are now persistent character profiles rather than audio-only settings. The default cast is stored in `config/host_profiles.json` and is mounted into the application container so edits survive rebuilds and restarts.

The seeded cast contains three deliberately different podcast personalities:

- **Wade Mercer — Southern Everyman:** practical, warm, skeptical, dry, and focused on what a story means in ordinary life. Southern without being written as a caricature.
- **Marcus Reed — City Pragmatist:** fast, analytical, culturally plugged-in, media-aware, and willing to challenge weak logic or a convenient headline.
- **Julian Cross — Worldly Context Guy:** composed, internationally minded, historically aware, and responsible for widening the frame beyond the immediate American political argument.

Every profile supports:

```text
name
role
Kokoro voice
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
6. Generate the episode through Kokoro as usual.

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
