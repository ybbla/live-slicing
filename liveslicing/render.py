"""Render a video from an EDL.

Implements the render pipeline in the correct order:

  1. Per-segment extract with color grade + 30ms audio fades baked in
  2. Concat segments (lossless copy → re-encode fallback → crossfade for multi-segment)
  3. If subtitles: single filter graph with subtitles filter LAST
  4. Two-pass loudness normalization (-14 LUFS)

Supports both single-clip EDL (v1 ranges[]) and multi-clip EDL (v3 clips[].segments[]).
Uses Microsoft YaHei for CJK subtitles with sentence-level splitting.

Usage:
    python -m liveslicing.render <edl.json> -o final.mp4
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

try:
    from liveslicing.grade import get_preset, auto_grade_for_clip
except Exception:
    def get_preset(name: str) -> str:
        return ""

    def auto_grade_for_clip(video, start=0.0, duration=None, verbose=False):  # type: ignore
        return "eq=contrast=1.03:saturation=0.98", {}


try:
    from liveslicing.timeline_view import render_timeline  # for self-eval QC
except Exception:
    render_timeline = None  # type: ignore


# -------- Subtitle style (bold-overlay, proven at 1920×1080 and 1080×1920) --
#
# MarginV is NOT taste — it is a platform safe-zone rule.
# TikTok / IG Reels / Shorts UI (caption, username, music, right-rail actions)
# covers roughly the bottom ~25–30% of a 1080×1920 frame. Captions placed near
# the bottom edge get clipped or obscured by the UI. libass auto-scales the
# render canvas relative to PlayResY=288, so MarginV=90 lands the caption
# baseline roughly 30% up from the bottom on any aspect — clear of the UI on
# every major vertical-video platform. Do not drop this below ~75 without a
# specific reason.
# Base subtitle style (MarginV replaced dynamically per video aspect/orientation).
# FontName=Microsoft YaHei is Windows-builtin CJK font with proper bold glyphs.
# Outline=2 gives 2px black stroke, readable on any background.
SUB_FORCE_STYLE_BASE = (
    "FontName=Microsoft YaHei,FontSize=18,Bold=1,"
    "PrimaryColour=&H00FFFFFF,OutlineColour=&H00000000,BackColour=&H00000000,"
    "BorderStyle=1,Outline=2,Shadow=0,"
    "Alignment=2"
)


def get_subtitle_style(video_path: Path) -> str:
    """Return SUB_FORCE_STYLE with MarginV tuned to video orientation.

    MarginV is libass distance from baseline to bottom edge (PlayResY=288 units).
    - Landscape: ~150px from bottom on 1080p → MarginV=40
    - Portrait (vertical short-form): ~330px from bottom on 1920h → MarginV=50
      (avoids TikTok/Reels/视频号 bottom UI controls which cover ~25% of screen)
    """
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=width,height",
             "-of", "csv=p=0", str(video_path)],
            capture_output=True, text=True, check=True,
        )
        w, h = map(int, out.stdout.strip().split(","))
        margin_v = 50 if h > w else 40  # portrait needs more bottom margin
    except Exception:
        margin_v = 45  # safe default if probe fails
    return f"{SUB_FORCE_STYLE_BASE},MarginV={margin_v}"

# -------- Helpers ------------------------------------------------------------


def run(cmd: list[str], quiet: bool = False) -> None:
    if not quiet:
        print(f"  $ {' '.join(str(c) for c in cmd[:6])}{' …' if len(cmd) > 6 else ''}")
    subprocess.run(cmd, check=True)


def resolve_grade_filter(grade_field: str | None) -> str:
    """The EDL's 'grade' field can be a preset name, a raw ffmpeg filter, or 'auto'.

    Returns the filter string to embed into the per-segment -vf chain.
    For 'auto', returns the sentinel "__AUTO__" which is resolved per-segment.
    """
    if not grade_field:
        return ""
    if grade_field == "auto":
        return "__AUTO__"
    # Preset names are short identifiers, filter strings contain '=' or ','.
    if re.fullmatch(r"[a-zA-Z0-9_\-]+", grade_field):
        try:
            return get_preset(grade_field)
        except KeyError:
            print(f"warning: unknown preset '{grade_field}', using as raw filter")
            return grade_field
    return grade_field


def resolve_path(maybe_path: str, base: Path) -> Path:
    """Resolve a path that may be absolute or relative to `base`."""
    p = Path(maybe_path)
    if p.is_absolute():
        return p
    return (base / p).resolve()


# -------- HDR → SDR tone mapping (HLG / PQ sources) --------------------------
#
# iPhone defaults to HLG HDR in Rec.2020 (and many mirrorless cameras ship PQ).
# If the source is HDR and we only downconvert bit depth (yuv420p10le → yuv420p)
# without tone-mapping, the output is 8-bit but still carries HLG/PQ transfer
# metadata. Players that honor the metadata (screen recorders, most social
# upload re-encodes) interpret 8-bit values in an HDR container and the result
# looks oversaturated / blown out. QuickTime on macOS can hide this locally —
# screen recording and uploaded renders cannot.
#
# Fix: detect HDR via color_transfer and prepend a zscale+tonemap chain to the
# vf graph so the output is clean Rec.709 SDR.

HDR_TRANSFERS = {"smpte2084", "arib-std-b67"}  # PQ (HDR10) and HLG

TONEMAP_CHAIN = (
    "zscale=t=linear:npl=100,"
    "format=gbrpf32le,"
    "zscale=p=bt709,"
    "tonemap=tonemap=hable:desat=0,"
    "zscale=t=bt709:m=bt709:r=tv,"
    "format=yuv420p"
)


def is_hdr_source(video: Path) -> bool:
    """Return True if the source uses a PQ or HLG transfer function."""
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=color_transfer",
             "-of", "default=noprint_wrappers=1:nokey=1", str(video)],
            capture_output=True, text=True, check=True,
        )
        return out.stdout.strip() in HDR_TRANSFERS
    except subprocess.CalledProcessError:
        return False


def is_portrait_source(video: Path) -> bool:
    """Return True if the video's height > width (portrait / vertical)."""
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=width,height",
             "-of", "csv=p=0", str(video)],
            capture_output=True, text=True, check=True,
        )
        w, h = map(int, out.stdout.strip().split(","))
        return h > w
    except Exception:
        return False


# -------- Per-segment extraction (Rule 2 + Rule 3) --------------------------


def extract_segment(
    source: Path,
    seg_start: float,
    duration: float,
    grade_filter: str,
    out_path: Path,
    preview: bool = False,
    draft: bool = False,
    fade_duration: float = 0.03,
) -> None:
    """Extract a cut range as its own MP4 with grade + audio fades baked in.

    `-ss` before `-i` for fast accurate seeking. Scale to 1080p from 4K.
    Portrait sources (height > width) are scaled by height to preserve orientation.

    Quality ladder:
      - final (default): 1080p libx264 fast CRF 20
      - preview:         1080p libx264 medium CRF 22 (evaluable for QC)
      - draft:           720p libx264 ultrafast CRF 28 (cut-point check only)
    """
    out_path.parent.mkdir(parents=True, exist_ok=True)

    portrait = is_portrait_source(source)
    if draft:
        scale = "scale=-2:1280" if portrait else "scale=1280:-2"
    else:
        scale = "scale=-2:1920" if portrait else "scale=1920:-2"

    vf_parts: list[str] = []
    if is_hdr_source(source):
        vf_parts.append(TONEMAP_CHAIN)
    vf_parts.append(scale)
    if grade_filter:
        vf_parts.append(grade_filter)
    vf = ",".join(vf_parts)

    # Audio fades at both edges — prevent pops
    fade_dur = max(0.01, min(0.1, fade_duration))  # Clamp 10-100ms
    fade_out_start = max(0.0, duration - fade_dur)
    af = f"afade=t=in:st=0:d={fade_dur:.3f},afade=t=out:st={fade_out_start:.3f}:d={fade_dur:.3f}"

    if draft:
        preset, crf = "ultrafast", "28"
    elif preview:
        preset, crf = "medium", "22"
    else:
        preset, crf = "fast", "20"

    cmd = [
        "ffmpeg", "-y",
        "-ss", f"{seg_start:.3f}",
        "-i", str(source),
        "-t", f"{duration:.3f}",
        "-vf", vf,
        "-af", af,
        "-c:v", "libx264", "-preset", preset, "-crf", crf,
        "-pix_fmt", "yuv420p", "-r", "24",
        "-c:a", "aac", "-b:a", "192k", "-ar", "48000",
        "-movflags", "+faststart",
        str(out_path),
    ]
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)


def extract_all_segments(
    edl: dict,
    edit_dir: Path,
    preview: bool,
    draft: bool = False,
    fade_duration: float = 0.03,
) -> list[Path]:
    """Extract every EDL range into edit_dir/clips_graded/seg_NN.mp4.
    Returns the ordered list of segment paths.

    If the EDL `grade` is "auto", analyze each segment range with
    `auto_grade_for_clip` and apply a per-segment subtle correction.
    Otherwise, apply the same preset/raw filter to every segment.
    """
    resolved = resolve_grade_filter(edl.get("grade"))
    is_auto = resolved == "__AUTO__"
    clips_dir = edit_dir / (
        "clips_draft" if draft else ("clips_preview" if preview else "clips_graded")
    )
    clips_dir.mkdir(parents=True, exist_ok=True)

    ranges = edl["ranges"]
    sources = edl["sources"]

    seg_paths: list[Path] = []
    print(f"extracting {len(ranges)} segment(s) → {clips_dir.name}/")
    if is_auto:
        print("  (auto-grade per segment: analyzing each range)")
    for i, r in enumerate(ranges):
        src_name = r["source"]
        src_path = resolve_path(sources[src_name], edit_dir)
        start = float(r["start"])
        end = float(r["end"])
        duration = end - start
        out_path = clips_dir / f"seg_{i:02d}_{src_name}.mp4"

        if is_auto:
            seg_filter, _stats = auto_grade_for_clip(src_path, start=start, duration=duration, verbose=False)
        else:
            seg_filter = resolved

        note = r.get("beat") or r.get("note") or ""
        print(f"  [{i:02d}] {src_name}  {start:7.2f}-{end:7.2f}  ({duration:5.2f}s)  {note}")
        if is_auto:
            print(f"        grade: {seg_filter or '(none)'}")
        extract_segment(src_path, start, duration, seg_filter, out_path, preview=preview, draft=draft, fade_duration=fade_duration)
        seg_paths.append(out_path)

    return seg_paths


# -------- Concat with crossfade for internal cuts ---------------------------
# Internal segment joins use short crossfades (video xfade + audio acrossfade)
# to eliminate hard cuts / audio pop dips from 30ms per-segment fades.
# First/last segment edges still keep their 30ms fades for pop protection.

XFADE_DURATION = 0.15   # 150ms video crossfade
ACROSSFADE_DURATION = 0.10  # 100ms audio crossfade


def _probe_duration(video: Path) -> float:
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", str(video)],
            capture_output=True, text=True, check=True,
        )
        return float(out.stdout.strip() or 0.0)
    except Exception:
        return 0.0


def concat_segments(segment_paths: list[Path], out_path: Path, edit_dir: Path,
                    xfade_duration: float = XFADE_DURATION, acrossfade_duration: float = ACROSSFADE_DURATION) -> None:
    """Concatenate segments. Multi-segment clips use crossfades at internal joins
    to eliminate hard cuts / audio dips; single-segment clips are just renamed/copied.
    Tries lossless concat first for single-segment; multi-segment always uses
    crossfade re-encoding for smooth transitions.
    """
    out_path.parent.mkdir(parents=True, exist_ok=True)

    if len(segment_paths) == 1:
        # Single segment: just copy directly (already has edge fades from extract)
        import shutil
        shutil.copyfile(segment_paths[0], out_path)
        return

    # Multi-segment: use xfade + acrossfade for smooth internal joins
    xfade_dur = max(0.05, min(0.3, xfade_duration))
    acrossfade_dur = max(0.03, min(0.2, acrossfade_duration))
    print(f"concat (with {xfade_dur*1000:.0f}ms crossfades) → {out_path.name}")

    durations = [_probe_duration(p) for p in segment_paths]
    inputs: list[str] = []
    for p in segment_paths:
        inputs += ["-i", str(p)]

    filter_parts: list[str] = []
    n = len(segment_paths)
    # Start with first segment
    cur_v = "[0:v]"
    cur_a = "[0:a]"
    offset = durations[0] - xfade_dur  # offset for first xfade

    for i in range(1, n):
        next_v = f"[{i}:v]"
        next_a = f"[{i}:a]"
        out_v_label = f"[v{i}]"
        out_a_label = f"[a{i}]"
        # Video xfade
        filter_parts.append(
            f"{cur_v}{next_v}xfade=transition=fade:duration={xfade_dur}:offset={offset:.3f}{out_v_label}"
        )
        # Audio acrossfade
        filter_parts.append(
            f"{cur_a}{next_a}acrossfade=d={acrossfade_dur}:c1=tri:c2=tri{out_a_label}"
        )
        cur_v = out_v_label
        cur_a = out_a_label
        # Update offset for next crossfade: add remaining duration of current segment after crossfade
        offset += durations[i] - xfade_dur

    filter_complex = ";".join(filter_parts)

    cmd = [
        "ffmpeg", "-y",
        *inputs,
        "-filter_complex", filter_complex,
        "-map", cur_v, "-map", cur_a,
        "-c:v", "libx264", "-preset", "fast", "-crf", "20",
        "-pix_fmt", "yuv420p", "-r", "24",
        "-c:a", "aac", "-b:a", "192k", "-ar", "48000",
        "-movflags", "+faststart",
        str(out_path),
    ]
    try:
        subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    except subprocess.CalledProcessError as e:
        # Fallback to plain concat if crossfade fails (e.g. segment too short for fade duration)
        print(f"  crossfade concat failed, falling back to plain concat: {e.stderr[:200]!r}")
        inputs_fb: list[str] = []
        for p in segment_paths:
            inputs_fb += ["-i", str(p)]
        filter_complex_fb = "".join(f"[{i}:v][{i}:a]" for i in range(n)) + f"concat=n={n}:v=1:a=1[v][a]"
        cmd_fb = [
            "ffmpeg", "-y",
            *inputs_fb,
            "-filter_complex", filter_complex_fb,
            "-map", "[v]", "-map", "[a]",
            "-c:v", "libx264", "-preset", "fast", "-crf", "20",
            "-pix_fmt", "yuv420p", "-r", "24",
            "-c:a", "aac", "-b:a", "192k", "-ar", "48000",
            "-movflags", "+faststart",
            str(out_path),
        ]
        subprocess.run(cmd_fb, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)


# -------- Master SRT (Rule 5) ------------------------------------------------


PUNCT_BREAK = set(".,!?;:")


def _srt_timestamp(seconds: float) -> str:
    total_ms = int(round(seconds * 1000))
    h, rem = divmod(total_ms, 3600_000)
    m, rem = divmod(rem, 60_000)
    s, ms = divmod(rem, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def _words_in_range(transcript: dict, t_start: float, t_end: float) -> list[dict]:
    out: list[dict] = []
    for w in transcript.get("words", []):
        if w.get("type") != "word":
            continue
        ws = w.get("start")
        we = w.get("end")
        if ws is None or we is None:
            continue
        if we <= t_start or ws >= t_end:
            continue
        out.append(w)
    return out


def _utterances_in_range(transcript: dict, t_start: float, t_end: float) -> list[dict]:
    """Utterances (sentence-level, with punctuation) overlapping [t_start, t_end]."""
    out: list[dict] = []
    for u in transcript.get("utterances", []):
        us = u.get("start")
        ue = u.get("end")
        if us is None or ue is None:
            continue
        if ue <= t_start or us >= t_end:
            continue
        out.append(u)
    return out


def _split_sentence(text: str, max_len: int = 18) -> list[str]:
    """Split a sentence into subtitle lines suitable for Chinese short-form video.

    Goals:
      - Each line ≤ max_len CJK chars (comfortable reading width for 1080p vertical/horizontal).
      - Break ONLY at punctuation (，。！？；：、,.!?;:) so punctuation stays at line END,
        never at line start. This prevents the "comma-on-line-start" artifact caused by
        libass's auto-wrap which doesn't respect punctuation.
      - Merge short pieces together up to max_len; never leave a bare punctuation mark
        on its own line.
      - If a piece (between two punctuation marks) itself exceeds max_len, hard-split
        it at max_len as a last resort.

    Returns a list of line strings. If the whole text fits in one line, returns [text].
    """
    text = (text or "").strip()
    if not text:
        return []
    if len(text) <= max_len:
        return [text]
    # Split AFTER each punctuation mark (punctuation stays attached to preceding clause)
    parts = re.split(r"(?<=[，。！？；：、,.!?;:])", text)
    parts = [p for p in parts if p.strip()]
    if not parts:
        return [text]
    # Merge short clauses into lines ≤ max_len
    lines: list[str] = []
    cur = ""
    for p in parts:
        if not cur or len(cur) + len(p) <= max_len:
            cur += p
        else:
            lines.append(cur)
            cur = p
    if cur:
        lines.append(cur)
    # Last-resort hard-split any line still over max_len
    final: list[str] = []
    for line in lines:
        while len(line) > max_len:
            final.append(line[:max_len])
            line = line[max_len:]
        if line:
            final.append(line)
    return final or [text]


def build_master_srt(edl: dict, edit_dir: Path, out_path: Path) -> None:
    """Build an output-timeline SRT from per-source transcripts.

    Per-sentence subtitles (one utterance = one cue) when the transcript has
    `utterances` (Volcengine keeps punctuation there) — far more coherent for
    Chinese than the old 2-word chunks. Text keeps original case + punctuation.
    Falls back to word-level chunking only if utterances are absent.

    Output times: cue.start - segment_start + segment_offset (cumulative).
    """
    transcripts_dir = edit_dir / "transcripts"
    sources = edl["sources"]

    entries: list[tuple[float, float, str]] = []
    seg_offset = 0.0

    for r in edl["ranges"]:
        src_name = r["source"]
        seg_start = float(r["start"])
        seg_end = float(r["end"])
        seg_duration = seg_end - seg_start

        tr_path = transcripts_dir / f"{src_name}.json"
        if not tr_path.exists():
            print(f"  no transcript for {src_name}, skipping captions for this segment")
            seg_offset += seg_duration
            continue

        transcript = json.loads(tr_path.read_text(encoding="utf-8"))
        utts = _utterances_in_range(transcript, seg_start, seg_end)

        if utts:
            # Per-sentence cues (preferred): one utterance → one or more cues,
            # each cue has at most 2 lines (prevent tall subtitle blocks from
            # covering speakers' mouths). Multi-line cues are joined with newline
            # so libass renders them as a block; line breaks are chosen at
            # punctuation to avoid libass auto-wrapping punctuation onto line starts.
            for u in utts:
                u_start = max(seg_start, float(u["start"]))
                u_end = min(seg_end, float(u["end"]))
                lines = _split_sentence(u.get("text", ""))
                if not lines:
                    continue
                dur = max(0.001, u_end - u_start)
                # Group lines into chunks of ≤2 lines → one cue per chunk
                MAX_LINES_PER_CUE = 2
                n_chunks = (len(lines) + MAX_LINES_PER_CUE - 1) // MAX_LINES_PER_CUE
                for ci in range(n_chunks):
                    chunk = lines[ci * MAX_LINES_PER_CUE : (ci + 1) * MAX_LINES_PER_CUE]
                    s = u_start + dur * ci / n_chunks
                    e = u_start + dur * (ci + 1) / n_chunks
                    out_start = max(0.0, s - seg_start) + seg_offset
                    out_end = max(0.0, e - seg_start) + seg_offset
                    if out_end <= out_start:
                        out_end = out_start + 0.4
                    sub_text = "\n".join(chunk)
                    entries.append((out_start, out_end, sub_text))
        else:
            # Fallback: word-level chunking (no upper — keep original case)
            words_in_seg = _words_in_range(transcript, seg_start, seg_end)
            current: list[dict] = []
            for w in words_in_seg:
                text = (w.get("text") or "").strip()
                if not text:
                    continue
                current.append(w)
                ends_in_punct = bool(text) and text[-1] in PUNCT_BREAK
                if len(current) >= 2 or ends_in_punct:
                    local_start = max(seg_start, current[0].get("start", seg_start))
                    local_end = min(seg_end, current[-1].get("end", seg_end))
                    out_start = max(0.0, local_start - seg_start) + seg_offset
                    out_end = max(0.0, local_end - seg_start) + seg_offset
                    if out_end <= out_start:
                        out_end = out_start + 0.4
                    t = " ".join((x.get("text") or "").strip() for x in current)
                    t = re.sub(r"\s+", " ", t).strip().rstrip(",;:")
                    entries.append((out_start, out_end, t))
                    current = []
            if current:
                local_start = max(seg_start, current[0].get("start", seg_start))
                local_end = min(seg_end, current[-1].get("end", seg_end))
                out_start = max(0.0, local_start - seg_start) + seg_offset
                out_end = max(0.0, local_end - seg_start) + seg_offset
                if out_end <= out_start:
                    out_end = out_start + 0.4
                t = " ".join((x.get("text") or "").strip() for x in current)
                t = re.sub(r"\s+", " ", t).strip().rstrip(",;:")
                entries.append((out_start, out_end, t))

        seg_offset += seg_duration

    # Sort and write as SRT
    entries.sort(key=lambda e: e[0])
    lines: list[str] = []
    for i, (a, b, t) in enumerate(entries, start=1):
        lines.append(str(i))
        lines.append(f"{_srt_timestamp(a)} --> {_srt_timestamp(b)}")
        lines.append(t)
        lines.append("")
    out_path.write_text("\n".join(lines), encoding="utf-8-sig")
    print(f"master SRT → {out_path.name} ({len(entries)} cues)")


# -------- Loudness normalization (social-ready audio) -----------------------


# Social-media standard: -14 LUFS integrated, -1 dBTP peak, LRA 11 LU.
# Matches YouTube / Instagram / TikTok / X / LinkedIn normalization targets.
LOUDNORM_I = -14.0
LOUDNORM_TP = -1.0
LOUDNORM_LRA = 11.0


def measure_loudness(video_path: Path) -> dict[str, str] | None:
    """Run ffmpeg loudnorm first pass and parse the JSON measurement.

    Returns a dict with measured_i, measured_tp, measured_lra, measured_thresh,
    target_offset, or None if measurement failed.
    """
    filter_str = (
        f"loudnorm=I={LOUDNORM_I}:TP={LOUDNORM_TP}:LRA={LOUDNORM_LRA}:print_format=json"
    )
    cmd = [
        "ffmpeg", "-y", "-hide_banner", "-nostats",
        "-i", str(video_path),
        "-af", filter_str,
        "-vn", "-f", "null", "-",
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    # loudnorm prints the JSON to stderr at the end of the run
    stderr = proc.stderr

    # Find the JSON block — loudnorm output contains a `{ ... }` block
    start = stderr.rfind("{")
    end = stderr.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return None
    try:
        data = json.loads(stderr[start : end + 1])
    except json.JSONDecodeError:
        return None
    needed = {"input_i", "input_tp", "input_lra", "input_thresh", "target_offset"}
    if not needed.issubset(data.keys()):
        return None
    return data


def apply_loudnorm_two_pass(
    input_path: Path,
    output_path: Path,
    preview: bool = False,
) -> bool:
    """Run two-pass loudnorm on input_path, write normalized copy to output_path.

    Returns True on success, False if measurement failed (caller should fall
    back to copying the input unchanged).

    In preview mode, skips the measurement pass and uses a one-pass approximation
    for speed. Final mode always does the proper two-pass.
    """
    if preview:
        # One-pass approximation — faster, slightly less accurate.
        filter_str = f"loudnorm=I={LOUDNORM_I}:TP={LOUDNORM_TP}:LRA={LOUDNORM_LRA}"
        cmd = [
            "ffmpeg", "-y", "-hide_banner", "-nostats",
            "-i", str(input_path),
            "-c:v", "copy",
            "-af", filter_str,
            "-c:a", "aac", "-b:a", "192k", "-ar", "48000",
            "-movflags", "+faststart",
            str(output_path),
        ]
        print(f"  loudnorm (1-pass preview) → {output_path.name}")
        subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        return True

    # Full two-pass
    print(f"  loudnorm pass 1: measuring {input_path.name}")
    measurement = measure_loudness(input_path)
    if measurement is None:
        print("  loudnorm measurement failed — falling back to 1-pass")
        return apply_loudnorm_two_pass(input_path, output_path, preview=True)

    print(f"    measured: I={measurement['input_i']} LUFS  "
          f"TP={measurement['input_tp']}  LRA={measurement['input_lra']}")

    filter_str = (
        f"loudnorm=I={LOUDNORM_I}:TP={LOUDNORM_TP}:LRA={LOUDNORM_LRA}"
        f":measured_I={measurement['input_i']}"
        f":measured_TP={measurement['input_tp']}"
        f":measured_LRA={measurement['input_lra']}"
        f":measured_thresh={measurement['input_thresh']}"
        f":offset={measurement['target_offset']}"
        f":linear=true"
    )
    cmd = [
        "ffmpeg", "-y", "-hide_banner", "-nostats",
        "-i", str(input_path),
        "-c:v", "copy",
        "-af", filter_str,
        "-c:a", "aac", "-b:a", "192k", "-ar", "48000",
        "-movflags", "+faststart",
        str(output_path),
    ]
    print(f"  loudnorm pass 2: normalizing → {output_path.name}")
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    return True


# -------- Final compositing (Rule 1 + Rule 4) -------------------------------


def build_final_composite(
    base_path: Path,
    overlays: list[dict],
    subtitles_path: Path | None,
    out_path: Path,
    edit_dir: Path,
    source_video: Path | None = None,
) -> None:
    """Final pass: base → overlays (PTS-shifted) → subtitles LAST → out.

    If there are no overlays and no subtitles, just copy base to out.
    """
    has_overlays = bool(overlays)
    has_subs = subtitles_path is not None and subtitles_path.exists()

    if not has_overlays and not has_subs:
        # Nothing to do — just rename/copy base to final name
        run(["ffmpeg", "-y", "-i", str(base_path), "-c", "copy", str(out_path)], quiet=True)
        return

    inputs: list[str] = ["-i", str(base_path)]
    for ov in overlays:
        ov_path = resolve_path(ov["file"], edit_dir)
        inputs += ["-i", str(ov_path)]

    filter_parts: list[str] = []
    # PTS-shift every overlay so its frame 0 lands at start_in_output
    for idx, ov in enumerate(overlays, start=1):
        t = float(ov["start_in_output"])
        filter_parts.append(f"[{idx}:v]setpts=PTS-STARTPTS+{t}/TB[a{idx}]")

    # Chain overlays on top of base
    current = "[0:v]"
    for idx, ov in enumerate(overlays, start=1):
        t = float(ov["start_in_output"])
        dur = float(ov["duration"])
        end = t + dur
        next_label = f"[v{idx}]"
        filter_parts.append(
            f"{current}[a{idx}]overlay=enable='between(t,{t:.3f},{end:.3f})'{next_label}"
        )
        current = next_label

    # Subtitles LAST — Rule 1.
    # ffmpeg's subtitles filter parses its filename through a layer that treats
    # ':' and '\' specially; on Windows we must escape the drive ':' and use
    # forward slashes so backslashes aren't eaten as escapes.
    if has_subs:
        subs_abs = str(subtitles_path.resolve()).replace("\\", "/").replace(":", r"\:").replace("'", r"\'")
        sub_style = get_subtitle_style(source_video or base_path) if source_video else SUB_FORCE_STYLE_BASE + ",MarginV=45"
        filter_parts.append(
            f"{current}subtitles='{subs_abs}':charenc=UTF-8:force_style='{sub_style}'[outv]"
        )
        out_label = "[outv]"
    else:
        # Rename the last overlay output to [outv] for consistency
        if has_overlays:
            filter_parts.append(f"{current}null[outv]")
            out_label = "[outv]"
        else:
            out_label = "[0:v]"

    filter_complex = ";".join(filter_parts)

    cmd = [
        "ffmpeg", "-y",
        *inputs,
        "-filter_complex", filter_complex,
        "-map", out_label,
        "-map", "0:a",
        "-c:v", "libx264", "-preset", "fast", "-crf", "18",
        "-pix_fmt", "yuv420p",
        "-c:a", "copy",
        "-movflags", "+faststart",
        str(out_path),
    ]
    print(f"compositing → {out_path.name}")
    print(f"  overlays: {len(overlays)}, subtitles: {'yes' if has_subs else 'no'}")
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)


# -------- Main ---------------------------------------------------------------


def main() -> None:
    ap = argparse.ArgumentParser(description="Render a video from an EDL")
    ap.add_argument("edl", type=Path, help="Path to edl.json")
    ap.add_argument("-o", "--output", type=Path, required=True, help="Output video path")
    ap.add_argument(
        "--preview",
        action="store_true",
        help="Preview mode: 1080p, medium, CRF 22 — evaluable for QC, faster than final.",
    )
    ap.add_argument(
        "--draft",
        action="store_true",
        help="Draft mode: 720p, ultrafast, CRF 28 — cut-point verification only.",
    )
    ap.add_argument(
        "--build-subtitles",
        action="store_true",
        help="Build master.srt from transcripts + EDL offsets before compositing",
    )
    ap.add_argument(
        "--no-subtitles",
        action="store_true",
        help="Skip subtitles even if the EDL references one",
    )
    ap.add_argument(
        "--no-loudnorm",
        action="store_true",
        help="Skip audio loudness normalization. Default is on (-14 LUFS, -1 dBTP, LRA 11).",
    )
    args = ap.parse_args()

    edl_path = args.edl.resolve()
    if not edl_path.exists():
        sys.exit(f"edl not found: {edl_path}")

    edl = json.loads(edl_path.read_text())
    edit_dir = edl_path.parent
    out_path = args.output.resolve()

    # 1. Extract per-segment (auto-grade per range if EDL grade is "auto")
    segment_paths = extract_all_segments(
        edl, edit_dir, preview=args.preview, draft=args.draft
    )

    # 2. Concat → base
    if args.draft:
        base_name = "base_draft.mp4"
    elif args.preview:
        base_name = "base_preview.mp4"
    else:
        base_name = "base.mp4"
    base_path = edit_dir / base_name
    concat_segments(segment_paths, base_path, edit_dir)

    # 3. Subtitles: build if requested, resolve final path
    subs_path: Path | None = None
    if not args.no_subtitles:
        if args.build_subtitles:
            subs_path = edit_dir / "master.srt"
            build_master_srt(edl, edit_dir, subs_path)
        elif edl.get("subtitles"):
            subs_path = resolve_path(edl["subtitles"], edit_dir)
            if not subs_path.exists():
                print(f"warning: subtitles path in EDL does not exist: {subs_path}")
                subs_path = None

    # 4. Composite (overlays + subtitles LAST) → intermediate (pre-loudnorm) path
    overlays = edl.get("overlays") or []
    if args.no_loudnorm:
        # Composite directly to final output
        build_final_composite(base_path, overlays, subs_path, out_path, edit_dir)
    else:
        # Composite to a temp file, then run loudnorm → final output
        tmp_composite = out_path.with_suffix(".prenorm.mp4")
        build_final_composite(base_path, overlays, subs_path, tmp_composite, edit_dir)
        print("loudness normalization → social-ready (-14 LUFS / -1 dBTP / LRA 11)")
        apply_loudnorm_two_pass(tmp_composite, out_path, preview=args.draft)
        tmp_composite.unlink(missing_ok=True)

    size_mb = out_path.stat().st_size / (1024 * 1024)
    print(f"\ndone: {out_path} ({size_mb:.1f} MB)")


def _probe_stream_duration(sources: dict, src_name: str) -> float:
    """ffprobe the source video duration (seconds), 0 if unknown. Cached per call."""
    src_path = resolve_path(sources[src_name], Path("."))
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", str(src_path)],
            check=True, capture_output=True, text=True,
        )
        return float(out.stdout.strip())
    except Exception:
        return 0.0


def render_clips(
    edl: dict,
    edit_dir: Path,
    out_dir: Path | None = None,
    *,
    subtitles: bool = True,
    preview: bool = False,
    self_eval: bool = True,
    _self_eval_fn=None,
    on_progress=None,
) -> tuple[list[Path], list[str | None]]:
    """Render each clip in the EDL as an independent clip_NNN.mp4.

    Each clip may contain 1+ segments (discontinuous ranges that get spliced
    into one clip). For every clip we build a sub-EDL whose `ranges` are the
    clip's segments and run the SAME proven pipeline render.main uses:
      extract_all_segments (grade + 30ms fades per segment)
      → concat_segments (splice this clip's segments into one base)
      → build_master_srt (per-clip SRT, seg_offset makes times start at 0)
      → build_final_composite (burn subtitles LAST)
      → apply_loudnorm_two_pass → clip_NNN.mp4 + clip_NNN.srt

    Returns (final_paths, qc_flags).
    """
    def _p(pct: int, msg: str):
        print(msg, flush=True)
        if on_progress:
            try:
                on_progress("render", pct, msg.lstrip())
            except Exception:
                pass

    sources = edl.get("sources", {})
    qc_flags: list[str | None] = []
    clips = edl.get("clips")
    if clips is None:
        clips = [
            {"source": r["source"],
             "segments": [{"start": r["start"], "end": r["end"]}],
             "title": r.get("quote", ""), "reason": r.get("reason", "")}
            for r in edl.get("ranges", [])
        ]
    if not clips:
        print("render_clips: no clips in EDL")
        return [], []

    clips_dir = out_dir or (edit_dir / "clips")
    clips_dir.mkdir(parents=True, exist_ok=True)

    def _render_single_clip(
        clip_idx: int,
        clip: dict,
        pad_before: float = 0.05,
        pad_after: float = 0.08,
        fade_duration: float = 0.03,
        segment_offsets: list[dict] | None = None,
    ) -> tuple[Path, list[dict], str | None]:
        """Render a single clip with given padding/fade parameters. Returns (final_path, unpadded_segments, qc_flag)."""
        src_name = clip["source"]
        segs = clip.get("segments") or []
        if not segs:
            raise ValueError(f"clip {clip_idx} has no segments")

        # Apply segment offsets if provided
        adjusted_segs = []
        offsets = segment_offsets or []
        offset_map = {o.get("index", 0): o for o in offsets}
        for idx, s in enumerate(segs):
            off = offset_map.get(idx, {})
            start = float(s["start"]) + off.get("start_offset", 0.0)
            end = float(s["end"]) + off.get("end_offset", 0.0)
            adjusted_segs.append({"start": max(0.0, start), "end": max(0.0, end)})

        stream_dur = _probe_stream_duration(sources, src_name)
        padded_ranges = []
        for s in adjusted_segs:
            ss = max(0.0, float(s["start"]) - pad_before)
            ee = float(s["end"]) + pad_after
            if stream_dur:
                ee = min(stream_dur, ee)
            padded_ranges.append({"source": src_name, "start": ss, "end": ee})

        sub_edl = {
            "version": 1,
            "sources": sources,
            "ranges": padded_ranges,
            "grade": edl.get("grade", "auto"),
            "_unpadded_segments": [{"start": float(s["start"]), "end": float(s["end"])} for s in adjusted_segs],
        }

        # Auto-grade
        if sub_edl["grade"] == "auto":
            src_path = resolve_path(sources[src_name], edit_dir)
            clip_start = float(padded_ranges[0]["start"])
            clip_end = float(padded_ranges[-1]["end"])
            clip_total_dur = clip_end - clip_start
            try:
                unified_filter, _ = auto_grade_for_clip(
                    src_path, start=clip_start, duration=clip_total_dur, verbose=False,
                )
                sub_edl["grade"] = unified_filter or "subtle"
            except Exception:
                sub_edl["grade"] = "subtle"

        # Extract segments with custom fade duration
        seg_paths = extract_all_segments(sub_edl, edit_dir, preview=preview, draft=False, fade_duration=fade_duration)

        # Concat
        base = clips_dir / f"clip_{clip_idx:03d}_base.mp4"
        if len(seg_paths) == 1:
            seg_paths[0].replace(base)
        else:
            concat_segments(seg_paths, base, edit_dir, xfade_duration=max(0.05, fade_duration*2), acrossfade_duration=max(0.03, fade_duration*1.5))

        # SRT
        srt_path = clips_dir / f"clip_{clip_idx:03d}.srt"
        if subtitles:
            transcript_path = edit_dir / "transcripts" / f"{Path(src_name).stem}.json"
            if transcript_path.exists():
                build_master_srt(sub_edl, edit_dir, srt_path)
            else:
                srt_path = None
        else:
            srt_path = None

        # Burn subtitles
        sub_base = base
        subbed = clips_dir / f"clip_{clip_idx:03d}_sub.mp4"
        src_path = resolve_path(sources[src_name], edit_dir)
        if subtitles and srt_path and srt_path.exists():
            build_final_composite(base, [], srt_path, subbed, edit_dir, source_video=src_path)
            sub_base = subbed

        # Loudnorm
        final = clips_dir / f"clip_{clip_idx:03d}.mp4"
        ok = apply_loudnorm_two_pass(sub_base, final, preview=preview)
        if not ok:
            run(["ffmpeg", "-y", "-i", str(sub_base), "-c", "copy", str(final)], quiet=True)

        # Cleanup
        for sp in seg_paths:
            sp.unlink(missing_ok=True)
        base.unlink(missing_ok=True)
        subbed.unlink(missing_ok=True)
        final.with_suffix(".prenorm.mp4").unlink(missing_ok=True)

        # QC
        qc_flag = None
        unpadded = [{"start": float(s["start"]), "end": float(s["end"])} for s in adjusted_segs]
        if self_eval and render_timeline is not None and _self_eval_fn is not None:
            qc_flag = _self_eval_fn(
                final, unpadded, edit_dir, preview=preview,
                pad_before=pad_before, pad_after=pad_after, fade_duration=fade_duration,
            )
        return final, unpadded, qc_flag

    final_paths: list[Path] = []
    _p(2, f"  rendering {len(clips)} clip(s) → {clips_dir.name}/")
    n_total = len(clips)
    for i, clip in enumerate(clips, start=1):
        segs = clip.get("segments") or []
        if not segs:
            print(f"  [{i:03d}] skip (no segments)")
            continue
        title = clip.get("title") or clip.get("reason") or ""
        total_dur = sum(float(s["end"]) - float(s["start"]) for s in segs)
        segs_str = " + ".join(f"{float(s['start']):.1f}-{float(s['end']):.1f}" for s in segs)
        print(f"  [{i:03d}] {clip['source']}  {segs_str}  ({total_dur:.2f}s)  {title[:40]}")

        # Render with auto-fix retries
        final = None
        qc_flag = None
        pad_before, pad_after, fade_duration = 0.05, 0.08, 0.03
        for qc_attempt in range(4):  # initial + up to 3 retries
            try:
                final, unpadded, qc_flag = _render_single_clip(
                    i, clip,
                    pad_before=pad_before,
                    pad_after=pad_after,
                    fade_duration=fade_duration,
                )
                if qc_flag is None:
                    break  # No issues, done
                if qc_attempt >= 3:
                    print(f"        [QC WARN] Failed to fix after 3 attempts: {qc_flag}")
                    break
                # Get adjustment suggestions from QC function
                print(f"        [QC] Issue found (attempt {qc_attempt+1}/3): {qc_flag}, attempting auto-fix...")
                adjustments = _self_eval_fn(
                    None, unpadded, edit_dir, get_adjustments=True,
                    issues=qc_flag, pad_before=pad_before, pad_after=pad_after, fade_duration=fade_duration,
                ) if _self_eval_fn else None
                if not adjustments:
                    break
                pad_before = float(adjustments.get("pad_before", pad_before))
                pad_after = float(adjustments.get("pad_after", pad_after))
                fade_duration = float(adjustments.get("fade_duration", fade_duration))
                # Apply offsets for next render
            except Exception as e:
                print(f"        [QC] Render attempt {qc_attempt+1} failed: {e}")
                if qc_attempt >= 3:
                    raise
                continue

        if final is None:
            final = clips_dir / f"clip_{i:03d}.mp4"

        size_mb = final.stat().st_size / (1024 * 1024)
        pct = 5 + int(95 * i / n_total)
        _p(pct, f"  [{i}/{n_total}] → {final.name} ({size_mb:.1f} MB)")
        final_paths.append(final)
        qc_flags.append(qc_flag)

    _p(100, f"  渲染完成，共 {len(final_paths)} 条")
    return final_paths, qc_flags


if __name__ == "__main__":
    main()
