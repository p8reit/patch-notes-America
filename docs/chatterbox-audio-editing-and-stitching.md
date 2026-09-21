# Chatterbox audio editing and stitching

## Ordered utterance manifest

The editable unit is an **utterance**, not a file, array index, or render batch.
An episode manifest stores utterances in presentation order. Each record has this
logical shape (paths and timing are filled in as rendering progresses):

```json
{
  "id": "6f2f8d5e02d04fc4be83ee470ba26b10",
  "sequence": 12,
  "section_id": "episode",
  "parent_turn_id": "b83fb927bb314244af524048217d20cf",
  "fragment_index": 2,
  "fragment_count": 3,
  "host_id": "05932cd50c2f4ad5abb1e12866fddeef",
  "display_name": "Wade Mercer",
  "normalized_text": "The text sent to Chatterbox.",
  "voice_revision": {
    "voice": "host-05932cd50c2f4ad5abb1e12866fddeef",
    "reference": "voices/wade.wav"
  },
  "synthesis_settings": {
    "tempo": 1.0,
    "exaggeration": 0.5,
    "cfg_weight": 0.5,
    "temperature": 0.8,
    "min_p": 0.05,
    "top_p": 1.0,
    "repetition_penalty": 1.2
  },
  "raw_output": "chunks/utterance-6f2f8d5e02d04fc4be83ee470ba26b10.wav",
  "normalized_output": "chunks/normalized/utterance-6f2f8d5e02d04fc4be83ee470ba26b10.wav",
  "timing": {"start": 42.1, "end": 48.4, "duration": 6.3},
  "transition": {"dramatic_pause_after": false, "pause_after_ms": 180}
}
```

`id` is an opaque stable manifest identity. It remains attached to the utterance
when an editor reorders it; filenames may use the ID but do not define it.
`sequence` alone controls playback order. `section_id` identifies a chapter or
episode section. `host_id` is the durable cast identity while `display_name` is
the label shown by editing tools. `voice_revision` identifies the selected voice
and reference revision, and `synthesis_settings` captures every control needed
to reproduce the request. Raw and normalized outputs are kept separately so
normalization never destroys a successful synthesis.

## Turns, requests, and render batches

A speaker turn is parsed before size splitting. Short turns create one utterance;
oversized turns retain sentence-aware splitting and create several utterances
with the same `parent_turn_id`. `fragment_index` and `fragment_count` distinguish
those intentional oversized-turn fragments. Consequently an editor can replace
the whole parent turn or regenerate just one fragment without mistaking a render
batch boundary for a conversational boundary.

There is exactly **one Chatterbox request per utterance**. A request must never
span speakers. Generation segments are only operational checkpoint batches;
they neither create utterance identities nor alter parent-turn relationships.

## Editing and stitching

Selection, captions, and clip suggestions read utterances in `sequence` order and
use each record's `timing`. Regeneration updates `raw_output`,
`normalized_output`, and `timing` on that same ID. Stitching resolves audio from
the recorded output paths rather than sorting filenames, then applies
`transition` metadata between utterances. This keeps edits deterministic even
when files are renamed, fragments are moved, or generation segment sizes change.
