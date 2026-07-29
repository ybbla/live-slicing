# Third-Party Notices

This project, **live-slicing**, is derived from the
[video-use](https://github.com/browser-use/video-use) skill (MIT License,
Copyright (c) 2026 Browser Use).

The following source files contain code adapted from video-use helpers:

- `liveslicing/render.py` — multi-segment rendering pipeline, concat logic, subtitle burning
- `liveslicing/grade.py` — FFmpeg color grading presets, auto_grade frame analysis
- `liveslicing/pack_transcripts.py` — phrase-level transcript packing into markdown
- `liveslicing/timeline_view.py` — filmstrip + waveform QC visualization

These files have been substantially modified for the live-slicing use case:

- Render pipeline extended for multi-segment clips (crossfade splicing), CJK
  subtitles (Microsoft YaHei font, Chinese punctuation splitting), two-pass
  loudness normalization (-14 LUFS), cut-edge padding, portrait video scaling,
  Windows path escaping, and a `render_clips()` batch entry point.
- Color grading extended with `auto_grade_for_clip()` (subtle corrective filter
  via ffmpeg signalstats) and a "subtle" preset; Windows stderr capture fixed.
- Transcription layer completely replaced: ElevenLabs Scribe → Volcengine
  (火山) bigmodel flash ASR with 60-minute chunking and sentence-level
  `utterances[]` for Chinese subtitle generation.
- Clip selection is a new module (`liveslicing/pick_clips.py`) using Doubao
  (豆包) LLM via Volcengine Ark for semantic highlight selection, plus
  Doubao Vision self-evaluation of rendered cut points.
- Timeline view fonts updated for cross-platform CJK rendering (Microsoft
  YaHei / PingFang / Noto Sans CJK).
- A Flask Web UI has been added for non-technical users.

The original MIT license is included as `LICENSE`.
