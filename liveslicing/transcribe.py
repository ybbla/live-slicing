"""火山引擎大模型录音文件识别（极速版）转录模块。

核心功能：通过ffmpeg从视频中提取单声道16kHz音频，base64编码后单次HTTP请求提交给火山ASR极速版接口，
将返回结果归一化为下游pack/render/timeline模块统一使用的`words[]`+`utterances[]`数据契约，
最终写入<edit_dir>/transcripts/<视频文件名>.json缓存文件，重复运行直接读取缓存避免重复扣费。

数据契约说明：
- words[]：扁平词级条目，结构为{type: "word"|"spacing", text: 文本, start: 开始秒数, end: 结束秒数, speaker_id: 说话人ID}
  其中spacing条目是根据词间隔自动合成的静音标记，保证打包模块的静音检测逻辑稳定工作
- utterances[]：句级条目，结构为{text: 带标点完整文本, start: 开始秒数, end: 结束秒数, speaker_id: 说话人ID}
  保留火山返回的原始标点和大小写，供字幕生成模块按句输出字幕，避免旧版本2字一块的碎片字幕问题

火山ASR返回原始结构：result.utterances[]，每条包含{text, start_time/end_time(毫秒), additions:{speaker}, words:[...]}
words[]中每个词包含{text, start_time/end_time(毫秒), confidence}

使用示例：
    python -m liveslicing.transcribe 直播.mp4
    python -m liveslicing.transcribe 直播.mp4 --edit-dir ./custom_edit_dir
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
RESOURCE_ID = "volc.bigasr.auc_turbo"   # 极速版资源ID
CODE_OK = "20000000"  # 接口成功状态码（响应头X-Api-Status-Code）

# 极速版单请求安全阈值（来自实测）：
# 56分钟/102.5MB wav（POST base64约140MB）稳定通过；2小时/219.7MB（POST约301MB）会触发HTTP 413网关payload限制
# 文档标注的「≤2小时/≤100MB」中100MB是软建议（102.5MB实际可过），真实硬限制在POST 140~301MB之间
# 取60分钟作为安全分段值（对应wav约110MB/POST约150MB，距离413阈值有充足余量）
# 分段数大幅减少：1小时直播单段、2小时仅2段，顺带解决了跨分段说话人ID不一致问题
DEFAULT_MAX_CHUNK_MINUTES = 60


def extract_audio(video_path: Path, dest: Path) -> None:
    """从视频中提取转录用音频：单声道、16kHz采样率、16-bit PCM wav格式。

    Args:
        video_path: 源视频文件路径
        dest: 输出wav文件路径
    """
    cmd = [
        "ffmpeg", "-y", "-i", str(video_path),
        "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le",
        str(dest),
    ]
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _ms(v) -> float:
    """将火山返回的毫秒级时间戳转换为秒级浮点数，转换失败返回0.0。

    Args:
        v: 毫秒级时间戳值（字符串/数字均可）

    Returns:
        转换后的秒级时间戳
    """
    try:
        return float(v) / 1000.0
    except (TypeError, ValueError):
        return 0.0


def normalize_response(resp: dict, time_offset_s: float = 0.0) -> tuple[list[dict], list[dict]]:
    """将火山ASR极速版原始响应归一化为words[] + utterances[]标准契约。

    Args:
        resp: 火山接口返回的原始JSON响应
        time_offset_s: 时间偏移量（秒），长视频分段转录时用，将分段相对时间转为全局绝对时间

    Returns:
        (words列表, utterances列表)元组：
        - words: 词级条目，供打包模块做静音检测（保持原有契约不变）
        - utterances: 句级条目，保留火山原始标点，供字幕生成模块按句输出字幕
    """
    result = resp.get("result", {})
    utterances = result.get("utterances") if isinstance(result, dict) else None
    if not utterances:
        return [], []

    words: list[dict] = []
    utts_out: list[dict] = []
    for utt in utterances:
        # 说话人ID优先取additions.speaker字段（实测位置），回落尝试其他常见字段名
        spk = None
        add = utt.get("additions") or {}
        if isinstance(add, dict) and "speaker" in add:
            spk = add.get("speaker")
        if spk is None:
            spk = utt.get("speaker", utt.get("speaker_id"))
        spk_id = f"speaker_{spk}" if spk is not None else None

        # 处理句级utterance，保留原始标点供字幕使用
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
        if uw:  # 有词级时间戳时，逐词处理
            for w in uw:
                words.append({
                    "type": "word",
                    "text": w.get("text", ""),
                    "start": _ms(w.get("start_time", 0)) + time_offset_s,
                    "end": _ms(w.get("end_time", 0)) + time_offset_s,
                    "speaker_id": spk_id,
                })
        elif utt_text:  # 无词级时间戳时，将整句作为一个word条目
            words.append({
                "type": "word",
                "text": utt_text,
                "start": utt_start,
                "end": utt_end,
                "speaker_id": spk_id,
            })
    return words, utts_out


def synthesize_spacing(words: list[dict], gap_threshold_s: float = 0.05) -> list[dict]:
    """在相邻词间隔超过阈值时插入spacing类型的静音条目。

    打包模块优先使用spacing条目做静音检测，同时也保留了词间隔直接检测的兜底逻辑，双重保证稳定性。

    Args:
        words: 原始词级列表
        gap_threshold_s: 静音判定阈值（秒），相邻词间隔≥该值时插入spacing条目，默认0.05秒

    Returns:
        插入静音条目后的完整词列表
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
    """调用火山ASR极速版接口，单次请求直接返回识别结果（无需submit/query轮询）。

    Args:
        audio_path: 待识别wav音频路径
        app_key: 火山ASR应用APP Key
        language: 指定识别语言代码，默认None自动检测

    Returns:
        火山接口返回的原始JSON响应

    Raises:
        RuntimeError: 接口调用失败、状态码异常或返回非JSON格式时抛出
    """
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
        "enable_itn": True,          # 开启逆文本归一化（数字/日期等格式化）
        "enable_punc": True,         # 开启自动标点
        "show_utterances": True,     # 返回utterances和词级时间戳
        "enable_speaker_info": True, # 开启说话人分离
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
            f"火山ASR调用失败: X-Api-Status-Code={status} "
            f"X-Api-Message={msg!r} 响应内容={resp.text[:500]}"
        )
    try:
        return resp.json()
    except Exception:
        raise RuntimeError(f"火山ASR返回非JSON格式响应: {resp.text[:500]}")


def _probe_duration_s(video_path: Path) -> float:
    """通过ffprobe获取媒体文件总时长（秒），失败返回0.0。

    Args:
        video_path: 媒体文件路径

    Returns:
        时长秒数
    """
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", str(video_path)],
            check=True, capture_output=True, text=True,
            encoding="utf-8", errors="replace",
        )
        return float(out.stdout.strip())
    except Exception:
        return 0.0


def transcribe_one(
    video: Path,
    edit_dir: Path,
    app_key: str,
    language: str | None = None,
    num_speakers: int | None = None,   # 保留参数兼容接口，火山自动聚类说话人无需指定
    verbose: bool = True,
    max_chunk_minutes: int = DEFAULT_MAX_CHUNK_MINUTES,
    on_progress=None,   # 进度回调，签名为(stage: str, percent: int, message: str)，供UI更新进度
) -> Path:
    """转录单个视频文件，返回转录结果JSON路径，已存在缓存时直接返回。

    长视频自动按max_chunk_minutes分段转录，每段结果加上时间偏移后合并为全局时间线。

    Args:
        video: 待转录视频路径
        edit_dir: 工作输出目录
        app_key: 火山ASR APP Key
        language: 指定识别语言，默认None自动检测
        num_speakers: 预留参数，火山自动聚类说话人无需指定
        verbose: 是否打印进度日志
        max_chunk_minutes: 单段最大分钟数，默认60分钟（安全阈值）
        on_progress: 进度回调函数

    Returns:
        转录结果JSON文件路径
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

    # 缓存命中直接返回，避免重复调用ASR扣费
    if out_path.exists():
        _p(f"使用缓存转录结果: {out_path.name}", 100)
        return out_path

    _p(f"  从{video.name}提取音频中", 5)

    t0 = time.time()
    duration_s = _probe_duration_s(video)
    chunk_seconds = max_chunk_minutes * 60

    all_words: list[dict] = []
    all_utterances: list[dict] = []
    with tempfile.TemporaryDirectory() as tmp:
        tmpd = Path(tmp)
        if duration_s <= 0 or duration_s <= chunk_seconds:
            # 短视频单段转录
            audio = tmpd / f"{video.stem}.wav"
            extract_audio(video, audio)
            size_mb = audio.stat().st_size / 1024 / 1024
            _p(f"  转录{video.stem}.wav中（{size_mb:.1f} MB）", 30)
            resp = call_volcengine(audio, app_key, language)
            w, u = normalize_response(resp, 0.0)
            all_words, all_utterances = w, u
        else:
            # 长视频分段转录，每段结果按起始偏移拼回全局时间
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
                _p(f"  转录第{i+1}/{n_chunks}段 "
                   f"（{start:.0f}-{start+seg:.0f}秒, {size_mb:.1f} MB）", pct)
                resp = call_volcengine(audio, app_key, language)
                w, u = normalize_response(resp, time_offset_s=float(start))
                all_words.extend(w)
                all_utterances.extend(u)

    # 分段结果按起始时间排序，合成静音spacing条目
    all_words.sort(key=lambda w: w["start"])
    all_utterances.sort(key=lambda u: u["start"])
    payload = {
        "words": synthesize_spacing(all_words),
        "utterances": all_utterances,
    }

    _p("  保存转录结果...", 95)
    out_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    dt = time.time() - t0

    kb = out_path.stat().st_size / 1024
    _p(f"  已保存: {out_path.name}（{kb:.1f} KB），耗时{dt:.1f}秒，词数: {len(payload['words'])}", 100)

    return out_path


def main() -> None:
    from liveslicing.config import volc_app_key

    ap = argparse.ArgumentParser(description="使用火山ASR极速版转录视频")
    ap.add_argument("video", type=Path, help="视频文件路径")
    ap.add_argument(
        "--edit-dir",
        type=Path,
        default=None,
        help="工作输出目录（默认: <视频所在目录>/edit）",
    )
    ap.add_argument(
        "--language",
        type=str,
        default=None,
        help="可选语言代码（如zh-CN），留空自动检测",
    )
    ap.add_argument(
        "--num-speakers",
        type=int,
        default=None,
        help="预留参数，火山自动聚类说话人无需指定",
    )
    ap.add_argument(
        "--max-chunk-minutes",
        type=int,
        default=DEFAULT_MAX_CHUNK_MINUTES,
        help="长视频单段最大转录分钟数",
    )
    args = ap.parse_args()

    video = args.video.resolve()
    if not video.exists():
        sys.exit(f"视频不存在: {video}")

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