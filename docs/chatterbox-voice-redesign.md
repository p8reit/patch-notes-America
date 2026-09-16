# Chatterbox voice-management redesign

## Decision summary

Treat a Chatterbox voice as a managed reference-audio asset, not as a preset
name supplied by the TTS model. The application should own voice upload,
validation, preview, assignment, and lifecycle. Chatterbox should receive an
immutable reference identifier (or resolved reference path) with each synthesis
request.

The redesign should be delivered in small migrations so existing jobs remain
renderable. The first release must make changing a host's voice possible from
the browser; tuning controls and richer voice-library features can follow.

## Why the current approach is insufficient

The present implementation exposes a `<select>` populated from WAV files that
already exist in the container's `/voices` mount. That directory is read-only,
there is no upload endpoint, and the repository intentionally contains no
speaker recordings. Consequently, a normal installation has only Chatterbox's
single built-in `default` voice. Saved cast profiles can also refer to missing
files, so the UI can display an assignment that cannot render.

There are additional lifecycle problems:

- A voice ID is just a filename stem. Replacing `host.wav` silently changes old
  casts and retried jobs, even though their metadata still says `host`.
- Voice availability is checked only when synthesis reaches Chatterbox. A long
  episode can therefore fail after it has been queued.
- A dropdown refresh is the only management operation. Users cannot listen to,
  rename, replace, archive, or inspect a reference.
- `default` looks like one choice among many, but it is a fallback model voice,
  not a catalog of built-in speaker presets.
- Host identity, reference audio, and synthesis settings are stored together.
  This makes it difficult to test a new voice without changing the cast.

## Target experience

### Voice Library

Add a dedicated **Voice Library** above the cast editor. It should:

1. Accept a WAV upload and a user-facing name.
2. Explain that the recording must be authorized, clean, single-speaker speech.
3. Validate the file before saving it and show duration, channels, sample rate,
   file size, and validation errors.
4. Generate a short preview from editable sample text.
5. Allow rename, replacement as a new revision, download, and archive.
6. Distinguish the built-in fallback from uploaded reference voices.

Uploading is the primary empty-state action. "Refresh voices" remains only as
an advanced reconciliation action for references copied onto disk manually.

### Cast assignment

Each host card should show a voice card rather than a bare dropdown:

- selected voice name, revision, and availability;
- **Preview**, **Change**, and **Manage voices** actions;
- an explicit unavailable state that blocks save/render instead of presenting a
  missing reference as selectable;
- synthesis controls under an **Advanced delivery settings** disclosure;
- **Reset delivery settings** and a short explanation that those controls alter
  delivery but do not create a distinct speaker.

Changing a selection should preview a fixed sentence without changing the
saved cast. **Save cast** commits the assignment. Episode submission should
show a preflight summary of host-to-voice assignments.

## Data model and storage

Store metadata separately from binary audio:

```text
config/voices.json
voices/
  <voice-id>/
    <revision-id>.wav
output/<job-id>/
  job.json
  voice-snapshots/
```

Suggested catalog record:

```json
{
  "id": "01JVOICE...",
  "name": "Wade reference",
  "status": "active",
  "active_revision_id": "sha256:...",
  "revisions": [{
    "id": "sha256:...",
    "path": "01JVOICE.../sha256-....wav",
    "sha256": "...",
    "duration_seconds": 18.4,
    "sample_rate": 24000,
    "channels": 1,
    "created_at": "..."
  }]
}
```

Host profiles should store `voice_id` and optionally a display-only
`voice_name`; do not use a filename or mutable name as identity. New job
manifests must snapshot `voice_id`, `voice_revision_id`, checksum, synthesis
settings, and the exact reference used. A queued job therefore keeps the same
sound if the library entry is later replaced or archived.

For the first implementation, bind-mount `voices/` read-write into the app and
read-only into Chatterbox. The app writes a temporary upload, validates and
normalizes it, then atomically renames it into the catalog. Chatterbox resolves
only catalog-generated, filename-safe revision IDs. Never accept an arbitrary
filesystem path from a request.

## API boundary

### Application API

Add these endpoints:

```text
GET    /api/voices
POST   /api/voices                         multipart upload + name
GET    /api/voices/{voice_id}
PATCH  /api/voices/{voice_id}              rename/archive
POST   /api/voices/{voice_id}/revisions    replacement upload
POST   /api/voices/{voice_id}/preview      sample text + optional settings
GET    /api/voices/{voice_id}/audio
POST   /api/voices/reconcile               optional disk import
```

Return structured errors (`code`, `message`, and field details), including
`invalid_audio`, `voice_unavailable`, `reference_changed`, and
`preview_failed`. Upload and preview endpoints need explicit size, text-length,
and timeout limits.

Before accepting a generation job, the app must resolve every assignment to an
active revision. Return HTTP 409 with all unavailable hosts in one response.
Persist the resolved revision in the job manifest before enqueueing it.

### Chatterbox adapter

Keep Chatterbox-specific behavior behind the bundled service. Evolve its
request contract from mutable `voice` filename stems to an explicit versioned
shape:

```json
{
  "input": "...",
  "voice": {"type": "reference", "revision_id": "sha256-..."},
  "generation": {
    "exaggeration": 0.5,
    "cfg_weight": 0.5,
    "temperature": 0.8,
    "min_p": 0.05,
    "top_p": 1.0,
    "repetition_penalty": 1.2
  },
  "output": {"format": "wav", "speed": 1.0}
}
```

Retain the existing flat request for one migration release. Translate both
forms into one internal request object, advertise capabilities and limits from
`GET /capabilities`, and remove signature inspection from the request path by
detecting supported Chatterbox parameters once at model startup.

`GET /voices` should no longer be the source of truth for the UI. It becomes an
adapter diagnostic listing resolvable revisions. The app catalog is the source
of truth; reconciliation reports catalog entries whose files are missing and
unmanaged files that can be imported.

## Validation and safety

On upload:

- limit request size before reading the complete body;
- decode with FFmpeg/ffprobe rather than trusting extension or MIME type;
- require one audio stream, a configurable duration range, and non-empty audio;
- normalize to a documented PCM WAV format and remove embedded metadata;
- calculate SHA-256 after normalization and deduplicate identical revisions;
- reject traversal, symlinks, and user-controlled destination paths;
- save provenance/consent confirmation and audit timestamps without claiming
  that a checkbox proves consent.

Archiving must be non-destructive while any cast or job references the voice.
Deletion can be introduced later with reference checks and a retention policy.

## Cache and regeneration semantics

A chunk is reusable only when its cache key covers all sound-affecting inputs:

```text
sha256(normalized text + voice revision checksum + generation settings +
       speed + Chatterbox model/build version)
```

Store this key beside each WAV. Changing a host voice invalidates only that
host's affected chunks; changing personality text alone does not invalidate
audio for an already-written script. Retried and resumed jobs always use their
snapshotted revision, never the host's latest assignment.

## Migration and delivery plan

### Phase 0 — repair the baseline

- Change bundled host profiles to `default` unless corresponding authorized
  references actually ship.
- Reject unavailable voice assignments when saving a cast and when creating a
  job.
- Mark missing selections as errors, not extra selectable options.
- Add contract tests proving two different references resolve to two different
  Chatterbox prompt paths.

**Exit criterion:** a clean checkout can save and render its bundled cast, and
cannot enqueue a cast with a missing voice.

### Phase 1 — minimum viable voice changes

- Add the catalog, secure upload/normalization, list, preview, and archive APIs.
- Add the Voice Library and host voice picker.
- Give the app read-write and Chatterbox read-only access to the voice volume.
- Add generation preflight and structured error display.

**Exit criterion:** from a clean installation, a user can upload two authorized
clips, preview both, assign them to different hosts, render, switch assignments,
and hear the change without editing files or restarting containers.

### Phase 2 — reproducibility

- Introduce immutable revisions and snapshot them into jobs.
- Add checksum-based chunk cache keys and selective regeneration.
- Migrate legacy filename voices into catalog records; preserve a temporary
  alias map so in-flight jobs continue to resolve.

**Exit criterion:** replacing a library reference does not alter queued,
resumed, or historical jobs, while a new job uses the new revision.

### Phase 3 — adapter hardening

- Add the versioned Chatterbox request contract and `/capabilities`.
- Detect model capabilities at startup and surface unsupported controls in the
  UI instead of silently ignoring them.
- Add model/build version to health, manifests, and cache keys.

**Exit criterion:** API compatibility tests cover the pinned Chatterbox release,
and an unsupported setting cannot appear editable or be silently discarded.

### Phase 4 — operational polish

- Add waveform/listen UX, reference quality guidance, audit events, quotas,
  cleanup tooling, backup/restore documentation, and optional object storage.
- Add metrics for upload failures, preview latency, synthesis failures by voice
  revision, and cache reuse.

## Test strategy

- **Unit:** catalog validation, safe IDs, atomic writes, deduplication, host
  migration, cache keys, capability mapping, and structured errors.
- **API integration:** upload/list/preview/archive, invalid/corrupt/oversized
  input, missing revision preflight, and legacy request compatibility.
- **Chatterbox contract:** mock model calls and assert each revision resolves to
  the expected `audio_prompt_path`; assert generation settings reach supported
  model parameters.
- **Job recovery:** queue a job, replace/archive the assigned voice, restart the
  worker, and verify the snapshotted revision is still used.
- **Browser:** upload two references, preview and assign them, save/reload the
  cast, switch one host, render a short script, and verify unavailable voices
  block submission with a useful message.
- **Security:** traversal names, symlinks, malformed multipart bodies, disguised
  non-audio files, decompression/resource limits, and concurrent replacement.

## Rollout and compatibility

Gate the new library behind `VOICE_LIBRARY_ENABLED` for one release. On startup,
perform a dry-run migration report before changing profiles. Back up
`host_profiles.json`; write catalog/profile changes atomically; retain legacy
filename aliases until no queued manifest uses them. Provide a rollback that
restores the profile backup and continues accepting the old flat synthesis
request.

The feature is ready to become the default only after the Phase 1 browser flow
passes against the real containerized Chatterbox service on both CPU and GPU
configurations. Unit tests alone cannot establish that two reference recordings
produce perceptibly distinct voices, so release validation must include a
short human listening check using authorized test recordings.
