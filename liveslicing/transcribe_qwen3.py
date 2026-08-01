"""Qwen3-ASR 本地开源模型转录模块（备用ASR方案）。

核心功能：完全复用现有火山ASR模块的接口和数据契约，可无缝替换默认火山云ASR，
适合无网络、隐私要求高、超长音频（火山单段限60分钟）、带BGM/歌声/方言直播的识别场景。

数据契约与火山ASR完全一致：
- words[]：扁平词级条目，结构为{type: "word"|"spacing", text: 文本, start: 开始秒数, end: 结束秒数, speaker_id: 说话人ID}
- utterances[]：句级条目，结构为{text: 带标点完整文本, start: 开始秒数, end: 结束秒数, speaker_id: 说话人ID}

使用前置依赖：
    pip install qwen-asr torch
    （首次运行会自动下载模型权重，0.6B版本约4GB，1.7B版本约10GB）

使用示例：
    1. 单独运行测试：
       python -m liveslicing.transcribe_qwen3 直播.mp4
    2. 主程序切换：将cli.py/render.py等模块中`from liveslicing.transcribe import transcribe_one`
       改为`from liveslicing.transcribe_qwen3 import transcribe_one`即可无缝切换，无需修改其他代码
    3. 长音频无长度限制，无需分段，自动支持中文方言、带BGM/歌声识别

注意：Qwen3-ASR本身不内置说话人分离（diarization）功能，speaker_id字段默认返回None，
如需说话人分离可后续接入pyannote.audio做二次聚类。
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Callable

# 全局模型实例缓存，避免重复加载模型
_model = None


def extract_audio(video_path: Path, dest: Path) -> None:
    """从视频中提取转录用音频：单声道、16kHz采样率、16-bit PCM wav格式（与火山版完全一致）。

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


def _load_model(model_size: str = "0.6B", device: str = "cuda"):
    """加载Qwen3-ASR模型，全局缓存避免重复加载。

    Args:
        model_size: 模型规模，可选"0.6B"（高速轻量）或"1.7B"（高精度）
        device: 推理设备，"cuda"（GPU）或"cpu"（CPU推理速度较慢）

    Returns:
        加载好的Qwen3-ASR模型实例

    Raises:
        ImportError: 未安装qwen-asr依赖时抛出
        RuntimeError: 模型加载失败时抛出
    """
    global _model
    if _model is not None:
        return _model
    try:
        from qwen_asr import Qwen3ASR
    except ImportError:
        raise ImportError(
            "使用Qwen3-ASR备用方案需先安装依赖：pip install qwen-asr torch"
        ) from None
    try:
        _model = Qwen3ASR.from_pretrained(
            f"Qwen/Qwen3-ASR-{model_size}",
            device=device,
            torch_dtype="bfloat16" if device == "cuda" else "float32",
            attn_implementation="flash_attention_2" if device == "cuda" else "eager",
        )
        return _model
    except Exception as e:
        # FlashAttention2安装失败时回退到普通注意力实现
        if "flash_attention" in str(e).lower():
            _model = Qwen3ASR.from_pretrained(
                f"Qwen/Qwen3-ASR-{model_size}",
                device=device,
                torch_dtype="bfloat16" if device == "cuda" else "float32",
            )
            return _model
        raise RuntimeError(f"Qwen3-ASR模型加载失败: {str(e)}") from e


def synthesize_spacing(words: list[dict], gap_threshold_s: float = 0.05) -> list[dict]:
    """在相邻词间隔超过阈值时插入spacing类型的静音条目（与火山版完全一致）。

    Args:
        words: 原始词级列表
        gap_threshold_s: 静音判定阈值（秒），默认0.05秒

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


def normalize_response(result: dict) -> tuple[list[dict], list[dict]]:
    """将Qwen3-ASR返回结果归一化为words[] + utterances[]标准契约（与火山版接口完全对齐）。

    Args:
        result: Qwen3-ASR返回的识别结果字典，包含text、words（带timestamps）等字段

    Returns:
        (words列表, utterances列表)元组
    """
    words: list[dict] = []
    utts_out: list[dict] = []
    if not result:
        return words, utts_out

    # 提取句级utterance
    full_text = (result.get("text") or "").strip()
    word_list = result.get("words") or []
    if not word_list and full_text:
        # 无词级时间戳时整句作为一个条目
        words.append({
            "type": "word",
            "text": full_text,
            "start": 0.0,
            "end": result.get("duration", 0.0),
            "speaker_id": None,
        })
        utts_out.append({
            "text": full_text,
            "start": 0.0,
            "end": result.get("duration", 0.0),
            "speaker_id": None,
        })
        return words, utts_out

    # 处理词级结果，按标点切分句子
    current_utt_text = []
    current_utt_start = None
    current_utt_end = None
    for w in word_list:
        text = w.get("text", "").strip()
        if not text:
            continue
        start = float(w.get("start", 0.0)) / 1000.0 if w.get("start", 0) > 10 else float(w.get("start", 0.0))
        end = float(w.get("end", 0.0)) / 1000.0 if w.get("end", 0) > 10 else float(w.get("end", 0.0))

        words.append({
            "type": "word",
            "text": text,
            "start": start,
            "end": end,
            "speaker_id": None,
        })

        if current_utt_start is None:
            current_utt_start = start
        current_utt_end = end
        current_utt_text.append(text)

        # 遇到句末标点切分utterance
        if text.endswith(("。", "！", "？", ".", "!", "?", "；", ";")):
            utt_text = "".join(current_utt_text).strip()
            if utt_text:
                utts_out.append({
                    "text": utt_text,
                    "start": current_utt_start,
                    "end": current_utt_end,
                    "speaker_id": None,
                })
            current_utt_text = []
            current_utt_start = None
            current_utt_end = None

    # 处理最后剩余的未切分文本
    if current_utt_text:
        utt_text = "".join(current_utt_text).strip()
        if utt_text:
            utts_out.append({
                "text": utt_text,
                "start": current_utt_start or 0.0,
                "end": current_utt_end or 0.0,
                "speaker_id": None,
            })

    return words, utts_out


def transcribe_one(
    video: Path,
    edit_dir: Path,
    app_key: str = "",  # 兼容原有接口，本地模型不需要app_key
    language: str | None = None,
    num_speakers: int | None = None,
    verbose: bool = True,
    max_chunk_minutes: int = 9999,  # 本地模型无长度限制，默认不分段
    on_progress: Callable | None = None,
    model_size: str = "0.6B",
    device: str = "cuda",
) -> Path:
    """转录单个视频文件，接口与火山版transcribe.transcribe_one完全一致，可无缝替换。

    Args:
        video: 待转录视频路径
        edit_dir: 工作输出目录
        app_key: 兼容原接口参数，本地模型无需使用
        language: 指定识别语言代码，默认None自动检测
        num_speakers: 预留参数，暂不支持说话人分离
        verbose: 是否打印进度日志
        max_chunk_minutes: 单段最大分钟数，本地模型无限制默认不分段
        on_progress: 进度回调函数，签名为(stage: str, percent: int, message: str)
        model_size: 模型规模，"0.6B"（高速）或"1.7B"（高精度）
        device: 推理设备，"cuda"或"cpu"

    Returns:
        转录结果JSON文件路径，格式与火山版完全一致
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

    # 缓存命中直接返回，避免重复转录
    if out_path.exists():
        _p(f"使用缓存转录结果: {out_path.name}", 100)
        return out_path

    _p(f"  从{video.name}提取音频中", 5)
    t0 = time.time()

    all_words: list[dict] = []
    all_utterances: list[dict] = []
    with tempfile.TemporaryDirectory() as tmp:
        tmpd = Path(tmp)
        audio = tmpd / f"{video.stem}.wav"
        extract_audio(video, audio)
        size_mb = audio.stat().st_size / 1024 / 1024
        _p(f"  加载Qwen3-ASR-{model_size}模型中", 20)
        model = _load_model(model_size=model_size, device=device)
        _p(f"  转录{video.stem}.wav中（{size_mb:.1f} MB）", 40)
        # 调用模型识别，开启时间戳、自动标点、逆文本归一化
        result = model.transcribe(
            str(audio),
            language=language,
            return_timestamps=True,
            enable_punctuation=True,
            enable_itn=True,
        )
        w, u = normalize_response(result)
        all_words, all_utterances = w, u

    # 排序并合成静音spacing条目
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
    ap = argparse.ArgumentParser(description="使用Qwen3-ASR本地开源模型转录视频（备用ASR方案）")
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
        help="可选语言代码（如zh、en、yue），留空自动检测",
    )
    ap.add_argument(
        "--model-size",
        type=str,
        default="0.6B",
        choices=["0.6B", "1.7B"],
        help="模型规模：0.6B（高速轻量，约4G显存）/1.7B（高精度，约8G显存）",
    )
    ap.add_argument(
        "--device",
        type=str,
        default="cuda",
        choices=["cuda", "cpu"],
        help="推理设备，cuda为GPU，cpu为CPU（速度较慢）",
    )
    args = ap.parse_args()

    video = args.video.resolve()
    if not video.exists():
        sys.exit(f"视频不存在: {video}")

    edit_dir = (args.edit_dir or (video.parent / "edit")).resolve()

    transcribe_one(
        video=video,
        edit_dir=edit_dir,
        language=args.language,
        model_size=args.model_size,
        device=args.device,
    )


if __name__ == "__main__":
    main()