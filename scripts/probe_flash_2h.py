"""火山 ASR 极速版 flash 2h 触顶探针。

目的：确认 2h（120 分钟）时长限是否为真天花板，以及该体积（wav ~228MB /
base64 ~310MB 的单个 POST）是否稳定可过。从多个源视频顺序拼接出 120 分钟。

用法：
    python probe_flash_2h.py
（自动用项目根目录下的 7.29/7.30/飞书 三个视频拼接）
"""
from __future__ import annotations

import base64
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

# Ensure scripts dir is on sys.path so we can import probe_volc_asr
_sys_dir = Path(__file__).resolve().parent
if str(_sys_dir) not in sys.path:
    sys.path.insert(0, str(_sys_dir))
# Also ensure project root is on path (probe runs from project root)
_sys_root = _sys_dir.parent
if str(_sys_root) not in sys.path:
    sys.path.insert(0, str(_sys_root))

import requests

import probe_volc_asr as base

FLASH_URL = base.FLASH_URL
RESOURCE_ID = base.VOLC_RESOURCE_ID
CODE_OK = base.CODE_OK

# 拼接顺序（按时长从长到短，凑够 120 分钟）
SOURCES = [
    "7.29铁军团天吉促销（最终版）回放.mp4",
    "7.30铁军团天吉促销（最终版）回放.mp4",
    "飞书20260728-221221.mp4",
]


def probe_dur(video: Path) -> float:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", str(video)],
        capture_output=True, text=True, check=True,
    )
    return float(out.stdout.strip() or 0.0)


def extract_chunk(video: Path, dest: Path, minutes: float) -> float:
    cmd = ["ffmpeg", "-y", "-ss", "0", "-t", f"{minutes*60:.0f}",
           "-i", str(video), "-vn", "-ac", "1", "-ar", "16000",
           "-c:a", "pcm_s16le", str(dest)]
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return probe_dur(dest)


def concat_wavs(parts: list[Path], out: Path) -> float:
    """无损拼接多段 wav 成一个。返回总时长。"""
    lst = out.parent / "_concat.txt"
    lst.write_text("".join(f"file '{p.resolve().as_posix()}'\n" for p in parts),
                   encoding="utf-8")
    subprocess.run(
        ["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(lst),
         "-c", "copy", str(out)],
        check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    return probe_dur(out)


def call_flash(app_key: str, audio: Path) -> tuple[dict, float, dict]:
    wav_b64 = base64.b64encode(audio.read_bytes()).decode("ascii")
    headers = {
        "X-Api-Key": app_key,
        "X-Api-Resource-Id": RESOURCE_ID,
        "X-Api-Request-Id": str(uuid.uuid4()),
        "X-Api-Sequence": "-1",
        "Content-Type": "application/json",
    }
    body = {
        "user": {"uid": app_key},
        "audio": {"data": wav_b64},
        "request": {
            "model_name": "bigmodel", "enable_itn": True, "enable_punc": True,
            "show_utterances": True, "enable_speaker_info": True,
        },
    }
    t0 = time.time()
    try:
        r = requests.post(FLASH_URL, headers=headers, json=body, timeout=1200)
    except Exception as e:
        return {"_error": repr(e)}, time.time() - t0, {"http_status": None}
    dt = time.time() - t0
    meta = {
        "http_status": r.status_code,
        "api_status": r.headers.get("X-Api-Status-Code", ""),
        "message": r.headers.get("X-Api-Message", ""),
        "logid": r.headers.get("X-Tt-Logid", ""),
    }
    try:
        return r.json(), dt, meta
    except Exception:
        return {"_raw": r.text[:800]}, dt, meta


def summarize(resp: dict) -> dict:
    result = resp.get("result", {})
    utts = result.get("utterances") if isinstance(result, dict) else result
    if not utts:
        return {"words": 0, "with_ts": 0, "with_spk": 0}
    words = sum(len(u.get("words") or []) for u in utts)
    with_ts = sum(1 for u in utts for w in (u.get("words") or [])
                  if (w.get("end_time", 0) or 0) > (w.get("start_time", 0) or 0))
    with_spk = sum(1 for u in utts
                   if (u.get("additions") or {}).get("speaker") or u.get("speaker"))
    return {"words": words, "with_ts": with_ts, "with_spk": with_spk}


def main() -> None:
    app_key = base.load_app_key()
    root = Path(__file__).resolve().parent
    videos = [root / s for s in SOURCES]
    for v in videos:
        if not v.exists():
            sys.exit(f"缺源视频: {v}")

    target_min = 120.0
    print(f"目标: 拼接 {target_min} 分钟音频测 2h 触顶")
    print("=" * 88)

    with tempfile.TemporaryDirectory() as tmp:
        tmpd = Path(tmp)
        parts: list[Path] = []
        acc_min = 0.0
        for v in videos:
            need = target_min - acc_min
            if need <= 0:
                break
            have = probe_dur(v) / 60.0
            take = min(need, have)
            part = tmpd / f"part_{len(parts)}.wav"
            print(f"  + {v.name[:30]} 取 {take:.1f} 分钟", flush=True)
            extract_chunk(v, part, take)
            parts.append(part)
            acc_min += take
        print(f"  拼接 {len(parts)} 段 → big.wav", flush=True)
        big = tmpd / "big.wav"
        dur_s = concat_wavs(parts, big)
        size_mb = big.stat().st_size / (1024 * 1024)
        b64_mb = size_mb * 1.37
        print(f"  big.wav {size_mb:.1f} MB ({dur_s/60:.1f} min)  base64≈{b64_mb:.0f} MB")
        print(f"  识别中（最长可能 ~130s）…", flush=True)

        resp, dt, meta = call_flash(app_key, big)
        ok = meta.get("api_status") == CODE_OK
        s = summarize(resp) if ok else {"words": 0, "with_ts": 0, "with_spk": 0}
        print()
        print(f"  HTTP {meta.get('http_status')}  api={meta.get('api_status')}  "
              f"{dt:.1f}s  words={s['words']}  ts={s['with_ts']}  spk={s['with_spk']}")
        print(f"  message: {meta.get('message')}")
        if not ok:
            print(f"  resp: {str(resp)[:400]}")
        print()
        print("=" * 88)
        if ok:
            print(f"✓ 2h（{dur_s/60:.0f}min / {size_mb:.0f}MB / POST {b64_mb:.0f}MB）成功。")
            print(f"  证实 2h 时长限可触达且不拒，文件大小无硬上限坐实。")
        else:
            print(f"✗ 2h 档失败：api={meta.get('api_status')} {meta.get('message')}")
            print(f"  → 2h 是真天花板，该体积/时长被拒。分段上限应留余量 < 2h。")


if __name__ == "__main__":
    main()
