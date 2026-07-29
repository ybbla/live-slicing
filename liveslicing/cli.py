"""智能直播切片 CLI —— 一键从长直播抽 N 条高光切片。

流水线：
  transcribe (火山 ASR) → pack (takes_packed.md) → pick_clips (豆包选段) → render_clips (多片段渲染)

复用 liveslicing 包下的 transcribe / pack_transcripts / render，以及 pick_clips。

用法：
  python cli.py <video> [options]

智能默认（面向非技术用户）：
  - 切片条数由豆包根据内容密度自定（参考：每小时 5-8 条，3-16 条之间）
  - 单条时长 30秒–5分钟
  - 调色 auto（自动微调）
  - 默认不烧录硬字幕，出独立 .srt
  - 原画质正式渲染 + vision 自评 QC
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from liveslicing.config import volc_app_key
from liveslicing.transcribe import transcribe_one
from liveslicing.pack_transcripts import pack_one_file, render_markdown
from liveslicing import pick_clips
from liveslicing.pick_clips import select_clips
from liveslicing import render


def pack_transcripts(edit_dir: Path, silence_threshold: float = 0.5, on_progress=None) -> Path:
    """把所有 transcript JSON 打包成 takes_packed.md。"""
    def _p(pct, msg):
        print(msg, flush=True)
        if on_progress:
            try:
                on_progress("pack", pct, msg.lstrip())
            except Exception:
                pass

    transcripts_dir = edit_dir / "transcripts"
    json_files = sorted(transcripts_dir.glob("*.json"))
    if not json_files:
        sys.exit(f"no transcripts found in {transcripts_dir}")

    _p(10, f"  打包 {len(json_files)} 个转录文件…")
    entries = [pack_one_file(p, silence_threshold) for p in json_files]
    markdown = render_markdown(entries, silence_threshold)

    out_path = edit_dir / "takes_packed.md"
    out_path.write_text(markdown, encoding="utf-8")
    total_phrases = sum(len(phrases) for _, _, phrases in entries)
    total_dur = sum(dur for _, dur, _ in entries)
    _p(100, f"  packed → {out_path} ({total_phrases} phrases, {_fmt_dur(total_dur)}, {out_path.stat().st_size/1024:.1f} KB)")
    return out_path


def _fmt_dur(seconds: float) -> str:
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    if h:
        return f"{h}h{m}m{s}s"
    return f"{m}m{s}s"


def run(
    video: Path,
    edit_dir: Path,
    count: int = 0,
    min_duration: float = 30.0,
    max_duration: float = 300.0,
    chunk_minutes: int = 0,
    grade: str = "auto",
    subtitles: bool = False,
    preview: bool = False,
    from_stage: str = "all",
    on_progress=None,
) -> dict:
    """主流水线。返回 manifest dict。"""
    app_key = volc_app_key()

    def _p(stage, pct, msg):
        print(msg, flush=True)
        if on_progress:
            try:
                on_progress(stage, pct, msg.lstrip())
            except Exception:
                pass

    # 1. 转录
    if from_stage in ("all", "transcribe"):
        _p("transcribe", 0, f"\n[1/4] 转录 {video.name} (火山 ASR)")
        transcribe_one(video, edit_dir, app_key, language=None,
                       on_progress=on_progress)

    # 2. 打包
    if from_stage in ("all", "transcribe", "pack"):
        _p("pack", 0, "\n[2/4] 打包转录 takes_packed.md")
        pack_transcripts(edit_dir, on_progress=on_progress)

    # 3. 选段
    if from_stage in ("all", "transcribe", "pack", "select"):
        auto = (count <= 0)
        if auto:
            _p("select", 0, f"\n[3/4] 豆包选段（自动判断条数，单条 {min_duration:.0f}-{max_duration:.0f}s）")
        else:
            _p("select", 0, f"\n[3/4] 豆包选段 (count={count}, {min_duration:.0f}-{max_duration:.0f}s)")
        select_clips(video, edit_dir, count=count if count > 0 else 0,
                     min_duration=min_duration, max_duration=max_duration,
                     chunk_minutes=chunk_minutes, on_progress=on_progress)

    # 4. 渲染
    _p("render", 0, "\n[4/4] 渲染独立切片")
    edl_path = edit_dir / "clips" / "edl_multi.json"
    edl = json.loads(edl_path.read_text(encoding="utf-8"))
    edl["grade"] = grade
    clips_dir = edit_dir / "clips"
    _p("render", 1, "  准备渲染…")
    paths, qc_flags = render.render_clips(
        edl, edit_dir, clips_dir,
        subtitles=subtitles, preview=preview,
        self_eval=not preview,
        _self_eval_fn=pick_clips.self_eval_clip,
        on_progress=on_progress,
    )

    # 写 manifest
    edl_clips = edl.get("clips", [])
    manifest = {
        "video": str(video.resolve()),
        "clips": [
            {
                "index": i + 1,
                "segments": [
                    {"start": s["start"], "end": s["end"]}
                    for s in c.get("segments", [])
                ],
                "duration": round(sum(s["end"] - s["start"] for s in c.get("segments", [])), 2),
                "title": c.get("title", ""), "reason": c.get("reason", ""),
                "file": p.name,
                "srt": p.with_suffix(".srt").name if (p.with_suffix(".srt")).exists() else None,
                "qc_flag": qc_flags[i] if i < len(qc_flags) else None,
            }
            for i, (c, p) in enumerate(zip(edl_clips, paths))
        ],
    }
    (clips_dir / "manifest.json").write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")
    _p("done", 100, f"\n=== 完成：{len(paths)} 条切片 → {clips_dir} ===")
    for p in paths:
        print(f"  {p.name}  ({p.stat().st_size / 1024 / 1024:.1f} MB)")

    return manifest


def main() -> None:
    ap = argparse.ArgumentParser(
        description="智能直播切片：长直播 → 高光切片",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="示例：python cli.py 直播.mp4  (智能默认，豆包自动决定条数)",
    )
    ap.add_argument("video", type=Path, help="视频文件路径")
    ap.add_argument("--edit-dir", type=Path, default=None,
                    help="工作目录（默认 <video所在目录>/edit）")
    ap.add_argument("--count", type=int, default=0,
                    help="目标切片条数（默认 0 = 自动，豆包根据内容密度决定）")
    ap.add_argument("--min-duration", type=float, default=30.0,
                    help="切片最短秒数（默认 30）")
    ap.add_argument("--max-duration", type=float, default=300.0,
                    help="切片最长秒数（默认 300 = 5分钟）")
    ap.add_argument("--chunk-minutes", type=int, default=0,
                    help="选段分块分钟数；0=不分块(默认)，>0=超长流降级分块")
    ap.add_argument("--grade", type=str, default="auto",
                    help="调色：auto / none / subtle / warm_cinematic 等（默认 auto）")
    ap.add_argument("--subtitles", action="store_true",
                    help="烧录硬字幕（默认不烧，只生成 .srt）")
    ap.add_argument("--no-subtitles", action="store_true",
                    help="（兼容旧参数，默认行为）不烧录字幕")
    ap.add_argument("--preview", action="store_true", help="快速低质量预览（跳过 QC）")
    ap.add_argument("--from-stage", type=str, default="all",
                    choices=["all", "transcribe", "pack", "select"],
                    help="从某步续跑（复用缓存）")
    args = ap.parse_args()

    video = args.video.resolve()
    if not video.exists():
        sys.exit(f"视频不存在: {video}")
    edit_dir = (args.edit_dir or (video.parent / "edit")).resolve()

    subtitles = args.subtitles and not args.no_subtitles

    run(
        video=video, edit_dir=edit_dir,
        count=args.count, min_duration=args.min_duration, max_duration=args.max_duration,
        chunk_minutes=args.chunk_minutes, grade=args.grade,
        subtitles=subtitles, preview=args.preview,
        from_stage=args.from_stage,
    )


if __name__ == "__main__":
    main()
