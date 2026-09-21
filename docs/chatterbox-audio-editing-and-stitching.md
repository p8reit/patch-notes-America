# Chatterbox audio editing and stitching

## Speech chunks are the durable unit

A **speech chunk** is a natural synthesis unit: one ordered piece of dialogue
with its speaker, voice settings, text, and pause metadata. It is not a
fixed-size slice of an episode and is never grouped into batches merely because
a counter reached a configured limit.

Generation job manifests own one ordered, top-level `chunks` collection. Each
chunk is checkpointed independently with these durable fields:

- `status`: `queued`, `running`, `retrying`, `complete`, or `failed`;
- `attempt`: the most recent synthesis attempt number;
- `output`: the path to the original synthesized WAV;
- `normalized_output`: the path to the validated, normalized WAV;
- `audio_metrics`: measurements captured during normalization; and
- `error`: the latest synthesis or validation error, or `null`.

This makes retries and restart recovery proportional to the actual failed work.
A valid completed chunk is reused after a process restart; an incomplete or
invalid chunk returns to the queue without discarding other completed speech.

## Stitching the episode

After all chunks complete, the application reads them in manifest order and
assembles the final episode directly from their normalized WAV files. Pause and
speaker-transition rules are applied at that final timeline step. There is no
intermediate audio batch that defines correctness or resumability.

The manifest reader remains compatible with older jobs. When it encounters a
legacy `segments` collection, it flattens each segment's chunks in segment and
chunk order into the top-level `chunks` collection. Existing chunk state and
artifacts are preserved, including attempts, outputs, metrics, and errors.

## Editorial outputs are separate concepts

An editor may still want intermediate deliverables, but synthesis batches are
not an editorial model. Represent those deliverables separately as either:

- **named chapters**, with meaningful titles and ordered chunk boundaries; or
- **export ranges**, with explicit start/end chunk IDs or timeline timestamps.

Chapters and export ranges may produce their own media files without owning or
duplicating synthesis status. This keeps editorial intent stable even if chunk
sizes, voices, retry policy, or render infrastructure change.
