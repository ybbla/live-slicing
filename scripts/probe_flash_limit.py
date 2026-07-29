"""火山 ASR 极速版（flash）单次请求上限探针。

极速版是 base64 内联单请求同步接口。文档标注「≤2h / 100MB，建议 ≤20MB」。
本探针递增时长（从同一视频从头截取 N 分钟），逐个发 flash，定位真实上限：
在哪触发 1010（音频过长）/ 1011（音频过大）/ HTTP 错误，或一路成功到哪。

每档记录：wav 大小、base64 大小、HTTP 状态、X-Api-Status-Code、X-Api-Message、
耗时、识别词数、是否有词级时间戳、是否有说话人分离。

用法：
    python probe_flash_limit.py <video> [--minutes 15,30,45,52]
    python probe_flash_limit.py "7.29铁军团天吉促销（最终版）回放.mp4"
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

# Ensure scripts dir is on sys.path so we can import probe_volc_asr
_sys_dir = Path(__file__).resolve().parent
if str(_sys_dir) not in sys.path:
    sys.path.insert(0, str(_sys_dir))

import requests

# 复用正式探针的凭证读取与常量
import probe_volc_asr as base

FLASH_URL = base.FLASH_URL
RESOURCE_ID = base.VOLC_RESOURCE_ID
CODE_OK = base.CODE_OK


def extract_chunk(video: Path, dest: Path, minutes: float) -> float:
    """从 video 从头截 minutes 分钟，抽成 16k mono wav。返回实际时长(秒)。"""
    cmd = [
        "ffmpeg", "-y", "-ss", "0", "-t", f"{minutes*60:.0f}",
        "-i", str(video), "-vn", "-ac", "1", "-ar", "16000",
        "-c:a", "pcm_s16le", str(dest),
    ]
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    # ffprobe 取实际时长
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", str(dest)],
        capture_output=True, text=True, check=True,
    )
    return float(out.stdout.strip() or 0.0)


def call_flash(app_key: str, audio: Path) -> tuple[dict, float, dict]:
    """返回 (响应dict, 耗时, meta)。meta 含 http_status/api_status/message/logid。"""
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
            "model_name": "bigmodel",
            "enable_itn": True,
            "enable_punc": True,
            "show_utterances": True,
            "enable_speaker_info": True,
        },
    }
    t0 = time.time()
    try:
        r = requests.post(FLASH_URL, headers=headers, json=body, timeout=900)
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
    """从响应里数词数/时间戳/说话人，判断是否有效返回。"""
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
    ap = argparse.ArgumentParser(description="火山 ASR 极速版上限探针")
    ap.add_argument("video", type=Path, help="源视频（≥最长测试档）")
    ap.add_argument("--minutes", type=str, default="15,30,45,52",
                    help="递增分钟数逗号分隔（默认 15,30,45,52）")
    args = ap.parse_args()

    video = args.video.resolve()
    if not video.exists():
        sys.exit(f"文件不存在: {video}")
    app_key = base.load_app_key()

    minutes = [float(x) for x in args.minutes.split(",")]
    print(f"源视频: {video.name}")
    print(f"测试档位(分钟): {minutes}")
    print(f"flash URL: {FLASH_URL}")
    print("=" * 92)

    rows = []
    with tempfile.TemporaryDirectory() as tmp:
        for m in minutes:
            wav = Path(tmp) / f"chunk_{int(m)}.wav"
            print(f"\n[{int(m)} min] 截取音频…", flush=True)
            try:
                dur_s = extract_chunk(video, wav, m)
            except Exception as e:
                print(f"  截取失败: {e!r}")
                rows.append((m, 0, 0, "-", "-", "extract_fail", "-", "-", "-"))
                continue
            size_mb = wav.stat().st_size / (1024 * 1024)
            b64_mb = size_mb * 1.37
            print(f"  wav {size_mb:.1f} MB ({dur_s:.0f}s)  base64≈{b64_mb:.0f} MB  识别中…", flush=True)

            resp, dt, meta = call_flash(app_key, wav)
            ok = meta.get("api_status") == CODE_OK
            s = summarize(resp) if ok else {"words": 0, "with_ts": 0, "with_spk": 0}
            status_str = "OK" if ok else f"FAIL({meta.get('api_status')})"
            msg = (meta.get("message") or "")[:40]
            print(f"  HTTP {meta.get('http_status')}  api={meta.get('api_status')}  "
                  f"{dt:.1f}s  words={s['words']}  ts={s['with_ts']}  spk={s['with_spk']}  {msg}")
            if not ok:
                # 失败也打印响应片段，便于看 1010/1011 之类
                print(f"  resp: {str(resp)[:300]}")
            rows.append((m, size_mb, b64_mb, dur_s,
                         meta.get("api_status"), status_str,
                         f"{dt:.1f}s", s["words"], msg))

    print("\n" + "=" * 92)
    print(f"{'min':>4} {'wavMB':>6} {'b64MB':>6} {'dur':>5} {'api':>10} {'status':>12} "
          f"{'耗时':>6} {'words':>6}  message")
    print("-" * 92)
    for m, sz, b64, dur, api, st, dt, w, msg in rows:
        print(f"{int(m):>4} {sz:>6.1f} {b64:>6.0f} {dur:>5.0f} {str(api):>10} {st:>12} "
              f"{dt:>6} {w:>6}  {msg}")
    print("=" * 92)
    print("判读：OK=成功；FAIL(1010)=音频过长；FAIL(1011)=音频过大。")
    print("      最大 OK 档的 wav MB 即极速版单请求真实文件上限；对应分钟即时长上限。")


if __name__ == "__main__":
    main()
