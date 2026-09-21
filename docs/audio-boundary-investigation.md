# Audio boundary investigation

The pipeline must concatenate complete speech segments; it must never overlap
adjacent PCM buffers or use a crossfade. The only generated samples between
segments are the pause frames written by `concatenate_wav_segments()`.

## Open-source approaches reviewed

* [pydub](https://github.com/jiaaro/pydub/blob/master/pydub/silence.py) scans
  audio in short chunks and classifies each chunk by aggregate loudness. This
  avoids treating a zero crossing as silence or a single click as speech.
* [FFmpeg `silencedetect`](https://ffmpeg.org/ffmpeg-filters.html#silencedetect)
  similarly requires audio to remain below a noise tolerance for a configured
  duration before reporting silence.
* [Coqui TTS](https://github.com/coqui-ai/TTS) performs long-form synthesis as
  sentence-sized utterances and concatenates completed waveform buffers rather
  than mixing adjacent utterances.

The original boundary implementation in this repository did not follow that
pattern: it classified individual PCM frames. One above-threshold click could
therefore make seconds of following silence look like speech, while very quiet
individual samples could create unstable boundary decisions.

The implementation now uses 10 ms RMS windows and requires 30 ms of sustained
activity. Both values are configurable. Trimming still retains the configured
leading/trailing allowance and safety buffer, and assembly remains strictly
sequential with an explicit pause. The original Chatterbox render is retained;
only a derived normalized copy is used in the final timeline.

Transcript content is not deduplicated or rewritten. Repeated words or passages
may be intentional, and text heuristics cannot safely diagnose an audio-boundary
problem.
