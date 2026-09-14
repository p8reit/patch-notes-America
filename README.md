# Patch Notes: America

Initial podcast-production application for turning a narration script into a finished MP3 using a locally hosted KokoroTTS server.

## MVP features

- Browser-based episode editor on port `8081`
- Preloaded pilot narration script
- Configurable Kokoro voice and tempo
- Automatic TTS-friendly script cleanup
- Automatic script chunking (default max 700 characters)
- KokoroTTS generation through `POST /tts/generate`
- Per-chunk WAV files retained for selective regeneration/debugging
- FFmpeg assembly into a final 128 kbps MP3
- Persistent `episodes/` source scripts and `output/` generated episodes
- Health endpoint showing whether the app can reach Kokoro
- Basic unit tests and GitHub Actions CI

## Architecture

```text
Browser :8081
    |
    v
FastAPI podcast app :8080 (inside Docker)
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

## Prerequisite

KokoroTTS should already be running on the Docker host:

```bash
docker run -d --name kokoro-tts -p 7860:7860 hangrylabs/kokorotts:v0.2
```

Verify it at:

```text
http://localhost:7860
```

## Start the podcast app

From the project directory:

```bash
docker compose up -d --build
```

Docker publishes host port `8081` to the app's internal port `8080`, so an
existing service on the host's port `8080` is not affected.

Open:

```text
http://localhost:8081
```

The top status card should show:

```text
App ready · Kokoro online
```

## Generate the pilot

1. Open `http://localhost:8081`.
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

## Useful commands

Check the app:

```bash
curl http://localhost:8081/api/health
```

Watch logs:

```bash
docker compose logs -f app
```

Restart after code/config changes:

```bash
docker compose up -d --build
```

Stop the application:

```bash
docker compose down
```

Kokoro remains separate and will continue running.

## Run everything from this repo instead

If you do **not** already have Kokoro running, use the full compose file:

```bash
docker compose -f docker-compose.full.yml up -d --build
```

Do not use that command while another container is already bound to port `7860`.

## Local development

```bash
python -m venv .venv
```

Windows PowerShell:

```powershell
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt pytest
pytest -q
uvicorn app.main:app --reload --port 8081
```

When running outside Docker, set `KOKORO_URL` to localhost:

```powershell
$env:KOKORO_URL="http://localhost:7860"
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
