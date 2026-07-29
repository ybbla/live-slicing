"""Transcribe a video with Volcengine 大模型录音文件识别（极速版）.

Extracts mono 16kHz audio via ffmpeg, POSTs base64 audio to Volcengine ASR
(flash, single-request, no submit/query polling), normalizes the response to
the on-disk `words[]` contract that downstream pack/render/timeline code reads,
and writes it to <edit_dir>/transcripts/<video_stem>.json.

The transcript JSON written here preserves the contract ElevenLabs Scribe used:
top-level {"words": [...]}, each entry
  {type ∈ word|spacing, text, start (float s), end (float s), speaker_id}
`spacing` entries (silence gaps) are synthesized from word-to-word gaps so
pack_transcripts' silence-detection works unchanged.

Volcengine returns: result.utterances[], each
  {text, start_time, end_time (ms), additions: {speaker: "N"}, words: [...]}
words[]: {text, start_time, end_time (ms), confidence}

Cached: if the output file already exists, the call is skipped.

Usage:
    python -m liveslicing.transcribe <video_path>
    python -m liveslicing.transcribe <video_path> --edit-dir /custom/edit
"""

from __future__ import annotations

import argparse
import base64
import json
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

import requests


FLASH_URL = "https://openspeech.bytedance.com/api/v3/auc/bigmodel/recognize/flash"
RESOURCE_ID = "volc.bigasr.auc_turbo"   # 极速版
CODE_OK = "20000000"

# 极速版实测单请求上限：56min/102.5MB wav（POST base64 ~140MB）稳过；2h/219.7MB
# 撞 HTTP 413（网关 payload 墙，非 ASR 业务拒）。文档「≤2h/≤100MB」中 100MB 是软建议
# （102.5MB 照样过），真实硬墙在 POST 140~301MB 之间。安全单段取 60min（wav~110MB /
# POST~150MB，离 413 阈值有余量）。1h 直播单段、2h 分 2 段。段数大减顺带让说话人
# 跨段 ID 不一致问题几乎不再出现（单段无跨段）。
DEFAULT_MAX_CHUNK_MINUTES = 60


def extract_audio(video_path: Path, dest: Path) -> None:
    cmd = [
        "ffmpeg", "-y", "-i", str(video_path),
        "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le",
        str(dest),
    ]
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _ms(v) -> float:
    """Volcengine timestamps are in milliseconds; convert to float seconds."""
    try:
        return float(v) / 1000.0
    except (TypeError, ValueError):
        return 0.0


def normalize_response(resp: dict, time_offset_s: float = 0.0) -> tuple[list[dict], list[dict]]:
    """Turn Volcengine flash response into the words[] + utterances[] contracts.

    Returns (words, utterances):
      - words: flat word-level entries (type/text/start/end/speaker_id) for
        pack_transcripts' silence detection (unchanged contract).
      - utterances: sentence-level entries {text, start, end, speaker_id} where
        `text` KEEPS Volcengine's original punctuation — used by build_master_srt
        for per-sentence subtitles (2-word chunks were too fragmented for Chinese).

    `time_offset_s` shifts every timestamp — used when a long audio was split
    into chunks; each chunk's times are relative to chunk start, so we add the
    chunk's start offset to get absolute stream time.
    """
    result = resp.get("result", {})
    utterances = result.get("utterances") if isinstance(result, dict) else None
    if not utterances:
        return [], []

    words: list[dict] = []
    utts_out: list[dict] = []
    for utt in utterances:
        # 说话人字段在 additions.speaker（实测）。回落试探常见别名。
        spk = None
        add = utt.get("additions") or {}
        if isinstance(add, dict) and "speaker" in add:
            spk = add.get("speaker")
        if spk is None:
            spk = utt.get("speaker", utt.get("speaker_id"))
        spk_id = f"speaker_{spk}" if spk is not None else None

        # 句级 utterance（保留原始标点，供按句字幕用）
        utt_start = _ms(utt.get("start_time", 0)) + time_offset_s
        utt_end = _ms(utt.get("end_time", 0)) + time_offset_s
        utt_text = (utt.get("text") or "").strip()
        if utt_text:
            utts_out.append({
                "text": utt_text,
                "start": utt_start,
                "end": utt_end,
                "speaker_id": spk_id,
            })

        uw = utt.get("words")
        if uw:  # 词级时间戳
            for w in uw:
                words.append({
                    "type": "word",
                    "text": w.get("text", ""),
                    "start": _ms(w.get("start_time", 0)) + time_offset_s,
                    "end": _ms(w.get("end_time", 0)) + time_offset_s,
                    "speaker_id": spk_id,
                })
        elif utt_text:  # 仅句级，用 utterance 本身当一条 word
            words.append({
                "type": "word",
                "text": utt_text,
                "start": utt_start,
                "end": utt_end,
                "speaker_id": spk_id,
            })
    return words, utts_out


def synthesize_spacing(words: list[dict], gap_threshold_s: float = 0.05) -> list[dict]:
    """Insert `spacing` entries between non-adjacent words (silence gaps).

    pack_transcripts prefers spacing entries for silence detection but also
    falls back to word-to-word gap detection, so this keeps it robust.
    """
    out: list[dict] = []
    for w in words:
        if out and w["start"] - out[-1].get("end", w["start"]) >= gap_threshold_s:
            out.append({
                "type": "spacing", "text": "",
                "start": out[-1]["end"], "end": w["start"], "speaker_id": None,
            })
        out.append(w)
    return out


def call_volcengine(
    audio_path: Path,
    app_key: str,
    language: str | None = None,
) -> dict:
    """Single Volcengine flash request. Returns the raw response JSON."""
    wav_b64 = base64.b64encode(audio_path.read_bytes()).decode("ascii")
    headers = {
        "X-Api-Key": app_key,
        "X-Api-Resource-Id": RESOURCE_ID,
        "X-Api-Request-Id": str(uuid.uuid4()),
        "X-Api-Sequence": "-1",
        "Content-Type": "application/json",
    }
    request = {
        "model_name": "bigmodel",
        "enable_itn": True,
        "enable_punc": True,
        "show_utterances": True,       # 返回 utterances + words 词级时间戳
        "enable_speaker_info": True,   # 说话人分离
    }
    if language:
        request["language"] = language
    body = {
        "user": {"uid": app_key},
        "audio": {"data": wav_b64},
        "request": request,
    }
    resp = requests.post(FLASH_URL, headers=headers, json=body, timeout=1800)
    status = resp.headers.get("X-Api-Status-Code", "")
    msg = resp.headers.get("X-Api-Message", "")
    if status != CODE_OK:
        raise RuntimeError(
            f"Volcengine ASR failed: X-Api-Status-Code={status} "
            f"X-Api-Message={msg!r} body={resp.text[:500]}"
        )
    try:
        return resp.json()
    except Exception:
        raise RuntimeError(f"Volcengine ASR non-JSON response: {resp.text[:500]}")


def _probe_duration_s(video_path: Path) -> float:
    """Get media duration in seconds via ffprobe."""
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", str(video_path)],
            check=True, capture_output=True, text=True,
        )
        return float(out.stdout.strip())
    except Exception:
        return 0.0


def transcribe_one(
    video: Path,
    edit_dir: Path,
    app_key: str,
    language: str | None = None,
    num_speakers: int | None = None,   # accepted for API parity; Volcengine auto-clusters
    verbose: bool = True,
    max_chunk_minutes: int = DEFAULT_MAX_CHUNK_MINUTES,
    on_progress=None,   # callable(stage, percent, message) for UI updates
) -> Path:
    """Transcribe a single video. Returns path to transcript JSON (words[] contract).

    Cached: returns existing path immediately if the transcript already exists.
    Long videos are split into <=max_chunk_minutes chunks, transcribed separately,
    and merged back with absolute timestamps.
    """
    def _p(msg: str, pct: int):
        if verbose:
            print(msg, flush=True)
        if on_progress:
            try:
                on_progress("transcribe", pct, msg.lstrip())
            except Exception:
                pass

    transcripts_dir = edit_dir / "transcripts"
    transcripts_dir.mkdir(parents=True, exist_ok=True)
    out_path = transcripts_dir / f"{video.stem}.json"

    if out_path.exists():
        _p(f"cached: {out_path.name}", 100)
        return out_path

    _p(f"  extracting audio from {video.name}", 5)

    t0 = time.time()
    duration_s = _probe_duration_s(video)
    chunk_seconds = max_chunk_minutes * 60

    all_words: list[dict] = []
    all_utterances: list[dict] = []
    with tempfile.TemporaryDirectory() as tmp:
        tmpd = Path(tmp)
        if duration_s <= 0 or duration_s <= chunk_seconds:
            # 一段搞定
            audio = tmpd / f"{video.stem}.wav"
            extract_audio(video, audio)
            size_mb = audio.stat().st_size / 1024 / 1024
            _p(f"  transcribing {video.stem}.wav ({size_mb:.1f} MB)", 30)
            resp = call_volcengine(audio, app_key, language)
            w, u = normalize_response(resp, 0.0)
            all_words, all_utterances = w, u
        else:
            # 按时长切分逐段识别，每段结果按起始偏移拼回绝对时间
            n_chunks = int(duration_s // chunk_seconds) + 1
            for i in range(n_chunks):
                start = i * chunk_seconds
                seg = min(chunk_seconds, duration_s - start)
                if seg <= 0:
                    break
                audio = tmpd / f"{video.stem}_part{i}.wav"
                cmd = ["ffmpeg", "-y", "-ss", str(start), "-t", str(seg),
                       "-i", str(video), "-vn", "-ac", "1", "-ar", "16000",
                       "-c:a", "pcm_s16le", str(audio)]
                subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                size_mb = audio.stat().st_size / 1024 / 1024
                pct = 10 + int(80 * (i + 1) / n_chunks)
                _p(f"  transcribing part {i+1}/{n_chunks} "
                   f"({start:.0f}-{start+seg:.0f}s, {size_mb:.1f} MB)", pct)
                resp = call_volcengine(audio, app_key, language)
                w, u = normalize_response(resp, time_offset_s=float(start))
                all_words.extend(w)
                all_utterances.extend(u)

    # 分段拼接后排序，并合成 spacing
    all_words.sort(key=lambda w: w["start"])
    all_utterances.sort(key=lambda u: u["start"])
    payload = {
        "words": synthesize_spacing(all_words),
        "utterances": all_utterances,
    }

    _p("  saving transcript...", 95)
    out_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    dt = time.time() - t0

    kb = out_path.stat().st_size / 1024
    _p(f"  saved: {out_path.name} ({kb:.1f} KB) in {dt:.1f}s, words: {len(payload['words'])}", 100)

    return out_path


def main() -> None:
    from liveslicing.config import volc_app_key

    ap = argparse.ArgumentParser(description="Transcribe a video with Volcengine ASR (flash)")
    ap.add_argument("video", type=Path, help="Path to video file")
    ap.add_argument(
        "--edit-dir",
        type=Path,
        default=None,
        help="Edit output directory (default: <video_parent>/edit)",
    )
    ap.add_argument(
        "--language",
        type=str,
        default=None,
        help="Optional language code (e.g., 'zh-CN'). Omit to auto-detect.",
    )
    ap.add_argument(
        "--num-speakers",
        type=int,
        default=None,
        help="Accepted for parity; Volcengine auto-clusters speakers.",
    )
    ap.add_argument(
        "--max-chunk-minutes",
        type=int,
        default=DEFAULT_MAX_CHUNK_MINUTES,
        help="Max minutes per ASR chunk for long videos.",
    )
    args = ap.parse_args()

    video = args.video.resolve()
    if not video.exists():
        sys.exit(f"video not found: {video}")

    edit_dir = (args.edit_dir or (video.parent / "edit")).resolve()
    app_key = volc_app_key()

    transcribe_one(
        video=video,
        edit_dir=edit_dir,
        app_key=app_key,
        language=args.language,
        num_speakers=args.num_speakers,
        max_chunk_minutes=args.max_chunk_minutes,
    )


if __name__ == "__main__":
    main()
