# Chatterbox audio editing and stitching

Speech is rendered as ordered utterances. Every utterance records a
`parent_turn_id`, which groups chunks originating in one uninterrupted speaker
turn, and a `boundary_reason`, which explains the transition immediately before
that utterance. The first utterance has no preceding boundary and therefore uses
`null`.

## Boundary taxonomy

* **`technical_continuation`** — a sentence exceeded the configured Chatterbox
  input limit and had to be split by words (or, for an exceptionally long token,
  characters). The fragments are still one continuous thought, so assembly adds
  little or no silence.
* **`sentence_break`** — the input limit required a split after sentence-ending
  punctuation. This gets normal, short conversational spacing.
* **`paragraph_break`** — a paragraph could not remain in the preceding chunk.
  This gets normal paragraph spacing, slightly longer than a sentence boundary.
* **`speaker_change`** — a speaker tag began a new turn. Its pause is configured
  with `SPEAKER_CHANGE_PAUSE_MS`.
* **`section_break`** — the production moved between editorial sections, such as
  the host introduction and episode body. Its pause is configured with
  `SECTION_CHANGE_PAUSE_MS`.
* **`explicit_dramatic_pause`** — production metadata deliberately requests a
  dramatic beat, rather than one inferred from text or speakers. Its pause is
  configured with `DRAMATIC_PAUSE_MS`.

Chunking packs text up to `MAX_CHARS_PER_CHUNK`. It first uses punctuation and
paragraph boundaries and resorts to word splitting only when a single sentence
cannot fit. This avoids turning implementation limits into audible sentence
breaks.

## Assembly contract

`calculate_transition_pause()` reads only the incoming utterance's
`boundary_reason`; it does not infer timing from host or section labels. Audio is
assembled strictly in order by writing each complete PCM buffer followed by the
declared silence. Speech buffers are never overlapped or crossfaded.

Normalization operates on derived copies and retains the configured leading and
trailing silence plus `SPEECH_SAFETY_BUFFER_MS`. Boundary metadata changes only
the silence inserted *between* those normalized copies, so it does not weaken
the existing protection around quiet speech edges.
