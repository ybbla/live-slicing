"""火山引擎 ASR 探针脚本（大模型录音文件识别 · 极速版）。

极速版：单次 HTTP 请求直接返回，无需 submit/query 轮询；音频 base64 直传，无需对象存储。
文档：https://docs.volcengine.com/docs/6561/1631584
限制：单音频 ≤2h / 100MB（建议 ≤20MB，即约 10 分钟 wav）。

目的：用一段短音频调通，确认
  1. 词级时间戳：result.utterances[].words[] 的 start_time/end_time（毫秒）
  2. 说话人分离：enable_speaker_info=true 后响应里 speaker 字段名与形态
  3. 认证/资源 ID 是否正确（X-Api-Status-Code=20000000 即成功）

用法：
  1. 火山控制台 → 语音技术 → 大模型录音文件识别 → 应用 APP Key
  2. 填 CONFIG 或 .env：VOLC_APP_KEY=...
  3. python probe_volc_asr.py <video_or_audio_path>

跑完把终端输出贴回来。
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

# Ensure project root is on sys.path so we can import liveslicing
_sys_root = Path(__file__).resolve().parent.parent
if str(_sys_root) not in sys.path:
    sys.path.insert(0, str(_sys_root))

import requests

# ──────────────────────────── CONFIG ────────────────────────────
VOLC_APP_KEY = "09dbf2ba-bbcf-428a-88bc-526b2e61e120"                                   # 新版控制台 APP Key（= X-Api-Key）
VOLC_RESOURCE_ID = "volc.bigasr.auc_turbo"          # 极速版资源 ID
FLASH_URL = "https://openspeech.bytedance.com/api/v3/auc/bigmodel/recognize/flash"
CODE_OK = "20000000"


def load_app_key() -> str:
    global VOLC_APP_KEY
    if VOLC_APP_KEY:
        return VOLC_APP_KEY
    env = {}
    for cand in (Path(__file__).resolve().parent / ".env", Path(".env")):
        if cand.exists():
            for line in cand.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip().strip('"').strip("'")
    env.update(os.environ)
    key = env.get("VOLC_APP_KEY", "")
    if not key:
        sys.exit(
            "缺少凭证：在脚本顶部 CONFIG 或 .env 填 VOLC_APP_KEY\n"
            "（火山控制台 → 语音技术 → 大模型录音文件识别 → 应用 APP Key）"
        )
    return key


def extract_audio(src: Path, dest: Path) -> None:
    """与正式工具同款：16k 单声道 pcm_s16le wav。"""
    cmd = [
        "ffmpeg", "-y", "-i", str(src),
        "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le",
        str(dest),
    ]
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _show(label: str, obj) -> None:
    s = obj if isinstance(obj, str) else json.dumps(obj, ensure_ascii=False, indent=2)
    print(f"\n===== {label} =====")
    print(s[:6000])
    if len(s) > 6000:
        print(f"...[truncated, total {len(s)} chars]")


def call_flash(app_key: str, audio: Path) -> dict:
    wav_b64 = base64.b64encode(audio.read_bytes()).decode("ascii")
    headers = {
        "X-Api-Key": app_key,
        "X-Api-Resource-Id": VOLC_RESOURCE_ID,
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
            "show_utterances": True,        # 返回 utterances + words（试探极速版是否支持）
            "enable_speaker_info": True,    # 说话人分离（试探响应字段名）
        },
    }
    _show("提交请求体（data 已省略）",
          {**body, "audio": {"data": f"<{len(wav_b64)} chars base64>"}})
    print(f"\n→ POST {FLASH_URL}")
    r = requests.post(FLASH_URL, headers=headers, json=body, timeout=300)
    print(f"  HTTP {r.status_code}")
    print(f"  X-Api-Status-Code: {r.headers.get('X-Api-Status-Code')}")
    print(f"  X-Api-Message: {r.headers.get('X-Api-Message')}")
    print(f"  X-Tt-Logid: {r.headers.get('X-Tt-Logid')}")
    _show("原始响应体", r.text)
    status = r.headers.get("X-Api-Status-Code", "")
    if status != CODE_OK:
        print(f"\n⚠️ X-Api-Status-Code={status}（非 {CODE_OK} 成功）。看上面的 X-Api-Message。")
    try:
        return r.json()
    except Exception:
        return {"_raw": r.text, "_status": r.status_code}


# ──────────────────── 归一化成 words[] 契约（试探） ────────────────────
def try_normalize(j: dict) -> list[dict]:
    """按文档结构归一化成下游 words[] 契约，仅用于核对。正式代码据真实响应写。

    目标契约：顶层 words[]，每条 {type∈word/spacing, text, start(秒), end(秒), speaker_id}
    火山时间戳单位：毫秒 → 除 1000 转秒。
    speaker 字段名文档未给，试探 speaker / speaker_id / additions.speaker。
    """
    result = j.get("result", {})
    if isinstance(result, list):
        utterances = result
    else:
        utterances = result.get("utterances") or []
    if not utterances:
        return []

    words: list[dict] = []
    for utt in utterances:
        spk = utt.get("speaker")
        if spk is None:
            spk = utt.get("speaker_id")
        if spk is None:
            add = utt.get("additions") or {}
            spk = add.get("speaker")
        spk_id = f"speaker_{spk}" if spk is not None else None

        uw = utt.get("words")
        if uw:  # 词级
            for w in uw:
                words.append({
                    "type": "word",
                    "text": w.get("text", ""),
                    "start": _ms(w.get("start_time", 0)),
                    "end": _ms(w.get("end_time", 0)),
                    "speaker_id": spk_id,
                })
        else:  # 仅句级
            words.append({
                "type": "word",
                "text": utt.get("text", ""),
                "start": _ms(utt.get("start_time", 0)),
                "end": _ms(utt.get("end_time", 0)),
                "speaker_id": spk_id,
            })
    # 合成 spacing（间隔 ≥0.05s）
    out: list[dict] = []
    for w in words:
        if out and w["start"] - out[-1].get("end", w["start"]) >= 0.05:
            out.append({"type": "spacing", "text": "",
                        "start": out[-1]["end"], "end": w["start"], "speaker_id": None})
        out.append(w)
    return out


def _ms(v) -> float:
    try:
        return float(v) / 1000.0
    except (TypeError, ValueError):
        return 0.0


def main() -> None:
    ap = argparse.ArgumentParser(description="火山引擎大模型录音文件识别（极速版）探针")
    ap.add_argument("audio_or_video", type=Path, help="短音频或视频文件路径（建议 ≤10 分钟）")
    args = ap.parse_args()
    src = args.audio_or_video.resolve()
    if not src.exists():
        sys.exit(f"文件不存在: {src}")

    app_key = load_app_key()
    if subprocess.run("ffmpeg -version", shell=True, capture_output=True).returncode != 0:
        sys.exit("ffmpeg 不在 PATH，请新开终端再跑。")

    with tempfile.TemporaryDirectory() as tmp:
        wav = Path(tmp) / "probe.wav"
        print(f"→ 抽音频 16k mono wav: {src.name}")
        extract_audio(src, wav)
        size_mb = wav.stat().st_size / (1024 * 1024)
        print(f"  wav 大小: {size_mb:.1f} MB")
        if size_mb > 20:
            sys.exit("⚠️ wav > 20MB，极速版建议 ≤20MB。换更短的测试音频（约 10 分钟以内）。")

        j = call_flash(app_key, wav)

        norm = try_normalize(j)
        if norm:
            _show("归一化成 words[] 契约（前 40 条）", norm[:40])
            word_count = sum(1 for w in norm if w["type"] == "word")
            with_ts = sum(1 for w in norm if w["type"] == "word" and w["end"] > w["start"])
            with_spk = sum(1 for w in norm if w.get("speaker_id"))
            print(f"\n归一化条数: {len(norm)} （含 spacing）")
            print(f"  word 条数: {word_count}")
            print(f"  有非零时间戳: {with_ts}  → {'词级时间戳可用 ✓' if with_ts else '仅句级/无时间戳 ✗'}")
            print(f"  有 speaker_id: {with_spk}  → {'分离可用 ✓' if with_spk else '分离未返回 ✗'}")
        else:
            print("\n无法按文档结构归一化——把上方「原始响应体」整段贴给我，我据真实字段写适配器。")


if __name__ == "__main__":
    main()
