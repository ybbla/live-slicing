"""转录文本打包模块：将ASR输出的转录JSON转换为LLM友好的短语级markdown文件。

核心功能：遍历<edit>/transcripts/目录下所有转录JSON文件，直接复用火山ASR返回的utterances字段（已经基于语义+自然停顿+说话人切换自动分好完整句子并加好标点），
每条语句带[开始-结束]时间戳前缀，最终输出为<edit>/takes_packed.md文件。

打包后文件大小约为原始JSON的1/10，大幅减少LLM token消耗，同时语句级结构让LLM仅通过文本即可获得精确的时间边界信息，
是豆包选段、看点提取阶段读取的核心输入文件。

使用示例：
    python -m liveslicing.pack_transcripts --edit-dir ./edit
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def format_time(seconds: float) -> str:
    """将秒级时间戳格式化为固定6字符宽度的"NNN.NN"字符串，保证markdown对齐。

    Args:
        seconds: 秒级时间戳

    Returns:
        格式化后的时间字符串
    """
    return f"{seconds:06.2f}"


def format_duration(seconds: float) -> str:
    """将秒级时长格式化为易读字符串（秒/分秒）。

    Args:
        seconds: 时长秒数

    Returns:
        格式化后的时长字符串，如"12.3s"或"3m 45.0s"
    """
    if seconds < 60:
        return f"{seconds:.1f}s"
    m = int(seconds // 60)
    s = seconds - m * 60
    return f"{m}m {s:04.1f}s"


def pack_one_file(json_path: Path) -> tuple[str, float, list[dict]]:
    """处理单个转录JSON文件，返回文件名、总时长、语句列表。

    Args:
        json_path: 转录JSON文件路径（由火山ASR接口返回，必须包含utterances字段）

    Returns:
        (文件名(不含后缀), 总时长秒数, 语句列表)元组
    """
    data = json.loads(json_path.read_text(encoding="utf-8"))
    utterances = data.get("utterances", [])

    if not utterances:
        raise ValueError(f"转录文件{json_path.name}格式无效：缺少utterances字段，可能是旧版本缓存，请重新转录")

    # 直接复用ASR返回的utterances：ASR已经基于语义+自然停顿+说话人切换自动分好句、加好标点，是最自然准确的结果
    # 不需要自己逐词拼接、硬加标点，避免出现生硬断句和错误标点
    phrases = []
    for utt in utterances:
        text = (utt.get("text") or "").strip()
        start = utt.get("start")
        end = utt.get("end")
        spk = utt.get("speaker_id")
        if not text or start is None or end is None or end <= start:
            continue
        phrases.append({
            "start": float(start),
            "end": float(end),
            "text": text,
            "speaker_id": spk,
        })

    if phrases:
        duration = phrases[-1]["end"] - phrases[0]["start"]
    else:
        duration = 0.0
    return json_path.stem, duration, phrases


def render_markdown(entries: list[tuple[str, float, list[dict]]]) -> str:
    """将多个文件的打包结果渲染为最终markdown文本。

    Args:
        entries: 多个文件的打包结果列表，每个元素为(文件名, 时长, 语句列表)

    Returns:
        渲染完成的markdown字符串
    """
    lines: list[str] = []
    lines.append("# Packed transcripts")
    lines.append("")
    lines.append("语句级转录，由ASR自动基于语义+停顿+说话人切换分句并添加标点。")
    lines.append("使用`[start-end]`格式的时间范围作为EDL切点依据。")
    lines.append("")
    for name, duration, phrases in entries:
        lines.append(f"## {name}  （时长: {format_duration(duration)}, {len(phrases)} 条语句）")
        if not phrases:
            lines.append("  _未检测到语音_")
            lines.append("")
            continue
        for p in phrases:
            spk = p.get("speaker_id")
            if spk is not None:
                # 说话人ID"speaker_0"简化为"S0"，提升可读性
                spk_str = str(spk)
                if spk_str.startswith("speaker_"):
                    spk_str = spk_str[len("speaker_"):]
                spk_tag = f" S{spk_str}"
            else:
                spk_tag = ""
            lines.append(f"  [{format_time(p['start'])}-{format_time(p['end'])}]{spk_tag} {p['text']}")
        lines.append("")
    return "\n".join(lines)


def main() -> None:
    ap = argparse.ArgumentParser(description="将转录JSON打包为LLM使用的takes_packed.md")
    ap.add_argument("--edit-dir", type=Path, required=True, help="包含transcripts/目录的工作目录")
    ap.add_argument(
        "-o", "--output",
        type=Path,
        default=None,
        help="输出文件路径（默认: <edit-dir>/takes_packed.md）",
    )
    args = ap.parse_args()

    edit_dir = args.edit_dir.resolve()
    transcripts_dir = edit_dir / "transcripts"
    if not transcripts_dir.is_dir():
        sys.exit(f"转录目录不存在: {transcripts_dir}")

    json_files = sorted(transcripts_dir.glob("*.json"))
    if not json_files:
        sys.exit(f"{transcripts_dir}目录下未找到JSON转录文件")

    entries = [pack_one_file(p) for p in json_files]
    markdown = render_markdown(entries)

    out_path = args.output or (edit_dir / "takes_packed.md")
    out_path.write_text(markdown, encoding="utf-8")

    total_phrases = sum(len(e[2]) for e in entries)
    total_duration = sum(e[1] for e in entries)
    kb = out_path.stat().st_size / 1024
    print(f"打包完成 {len(entries)} 个转录文件 → {out_path}")
    print(f"  共{total_phrases}条短语，总时长{format_duration(total_duration)}")
    print(f"  文件大小{kb:.1f} KB")


if __name__ == "__main__":
    main()