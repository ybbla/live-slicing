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
import subprocess
import sys
from pathlib import Path

from liveslicing.config import volc_app_key
from liveslicing.transcribe import transcribe_one
from liveslicing.pack_transcripts import pack_one_file, render_markdown
from liveslicing import pick_clips
from liveslicing.pick_clips import select_clips, parse_phrases, _probe_video_duration
from liveslicing import render
from liveslicing import propositions as props_mod

# 项目根目录，与Web UI共享配置，保证输出路径、历史记录完全统一
PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"
UPLOAD_DIR = DATA_DIR / "uploads"
OUTPUT_DIR = DATA_DIR / "output"
# 初始化确保目录存在
for _d in (DATA_DIR, UPLOAD_DIR, OUTPUT_DIR):
    _d.mkdir(parents=True, exist_ok=True)


def _unique_path(parent: Path, name: str) -> Path:
    """返回parent下唯一的路径，重名时自动追加 (1)/(2) 后缀（Windows资源管理器默认风格），避免覆盖旧结果。

    与Web UI逻辑完全一致，保证CLI和Web端输出目录命名规则统一。
    """
    target = parent / name
    if not target.exists():
        return target
    stem = target.stem
    suffix = target.suffix
    i = 1
    while True:
        target = parent / f"{stem} ({i}){suffix}"
        if not target.exists():
            return target
        i += 1


def _find_latest_edit_dir(video_stem: str) -> Path | None:
    """查找对应视频名的最新工作目录（命题模式第二阶段自动复用第一阶段缓存用）。

    规则：
    1. 匹配所有以视频名开头的目录（包括带(1)/(2)后缀的重名目录）
    2. 目录下必须存在propositions.json缓存文件（第一阶段已完成）
    3. 按目录修改时间倒序，返回最新的一个
    4. 找不到返回None

    Args:
        video_stem: 视频文件名（不含后缀）

    Returns:
        最新的有效工作目录路径，找不到返回None
    """
    if not OUTPUT_DIR.exists():
        return None
    candidates = []
    for d in OUTPUT_DIR.iterdir():
        if not d.is_dir():
            continue
        # 匹配以视频名开头的目录（包括重名后缀如"xxx (1)"）
        if d.name.startswith(video_stem):
            # 必须存在命题缓存文件，才是有效的第一阶段完成目录
            if (d / "propositions.json").exists():
                candidates.append(d)
    if not candidates:
        return None
    # 按修改时间排序，最新的在前面
    candidates.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return candidates[0]


def pack_transcripts(edit_dir: Path, on_progress=None) -> Path:
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
    entries = [pack_one_file(p) for p in json_files]
    markdown = render_markdown(entries)

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
    min_duration: float = 30.0,
    max_duration: float = 300.0,
    grade: str = "auto",
    subtitles: bool = False,
    preview: bool = False,
    from_stage: str = "all",
    on_progress=None,
) -> dict:
    """主流水线：完整跑通「转录→打包→自动选高光→渲染」四阶段，返回manifest字典。

    Args:
        video: 输入直播视频路径
        edit_dir: 工作输出目录（存放转录缓存、打包文本、EDL、切片结果）
        min_duration: 单条切片最短时长（秒）
        max_duration: 单条切片最长时长（秒），多段拼接时为总时长
        grade: 调色模式：auto(智能自然微调，默认)/none(不调色)/light(轻度增强)/warm_cinematic(暖调电影感)
        subtitles: 是否烧录硬字幕，默认False仅生成独立srt文件
        preview: 是否快速预览模式（低质量快速渲染，跳过vision质检，用于先看选段效果）
        from_stage: 从指定阶段续跑，复用已有缓存：all(从头)/transcribe(跳过转录)/pack(跳过转录+打包)/select(只跑选段+渲染)
        on_progress: 进度回调函数，签名为(stage: str, percent: int, message: str)，供Web UI更新进度条

    Returns:
        manifest字典，包含视频路径、每条切片的分段时间码、时长、标题、选段理由、文件名、QC标记
    """
    app_key = volc_app_key()

    def _p(stage, pct, msg):
        print(msg, flush=True)
        if on_progress:
            try:
                on_progress(stage, pct, msg.lstrip())
            except Exception:
                pass

    # 自动降级起始阶段：如果指定了from_stage但前置缓存文件不存在，自动回退到更早的阶段，避免报错
    from_stage = _auto_downgrade_stage(edit_dir, from_stage, _p)

    # 提前生成带时间戳的clips目录，复用公共逻辑
    clips_dir = _prepare_clips_dir(edit_dir)

    # 1. 转录 + 2. 打包（复用公共逻辑）
    _run_transcribe_pack(edit_dir, video, app_key, from_stage, on_progress=on_progress, total_stages=4)

    # 3. 选段（edl会写入clips目录，软链接自动指向最新时间戳目录）
    if from_stage in ("all", "transcribe", "pack", "select"):
        _p("select", 0, f"\n[3/4] 豆包自动选高光片段（自动判断条数，单条 {min_duration:.0f}-{max_duration:.0f}s）")
        select_clips(video, edit_dir,
                     min_duration=min_duration, max_duration=max_duration,
                     chunk_minutes=0, on_progress=on_progress,
                     clips_dir=clips_dir)

    # 4. 渲染
    _p("render", 0, f"\n[4/4] 渲染独立切片 → {clips_dir.name}")
    edl_path = clips_dir / "edl_multi.json"
    # 选段已将EDL直接写入最终时间戳目录，直接读取
    edl = json.loads(edl_path.read_text(encoding="utf-8"))
    edl["grade"] = grade
    _p("render", 1, "  准备渲染…")
    paths, qc_flags = render.render_clips(
        edl, edit_dir, clips_dir,
        subtitles=subtitles, preview=preview,
        self_eval=not preview,
        _self_eval_fn=pick_clips.self_eval_clip,
        on_progress=on_progress,
    )

    # 写 manifest，包含任务配置用于重剪功能
    edl_clips = edl.get("clips", [])
    from datetime import datetime
    manifest = {
        "video": str(video.resolve()),
        "output_dir": str(edit_dir.resolve()),
        "clips_dir": str(clips_dir.resolve()),
        "mode": "auto",
        "created_at": datetime.now().isoformat(),
        "config": {
            "mode": "auto",
            "subtitles": subtitles,
            "preview": preview,
            "grade": grade,
            "min_duration": min_duration,
            "max_duration": max_duration,
        },
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


# ────────────────── 命题模式：拆分的流水线两阶段 ──────────────────

def _prepare_clips_dir(edit_dir: Path) -> Path:
    """创建带时间戳的clips真实目录，不创建软连接/联接，避免历史版本重复显示。

    抽取出来复用，run/run_to_propositions/run_from_selection都需要用。

    Args:
        edit_dir: 工作输出目录

    Returns:
        新创建的clips时间戳真实目录路径
    """
    from datetime import datetime
    time_suffix = datetime.now().strftime("%Y%m%d_%H%M%S")
    clips_dir = edit_dir / f"clips_{time_suffix}"
    clips_dir.mkdir(parents=True, exist_ok=True)
    # 清理旧版本遗留的clips软链接/联接和空目录
    legacy_clips = edit_dir / "clips"
    try:
        if legacy_clips.exists():
            if legacy_clips.is_symlink() or (legacy_clips.is_dir() and not any(legacy_clips.iterdir())):
                legacy_clips.unlink()
    except Exception:
        pass
    return clips_dir


def _auto_downgrade_stage(edit_dir: Path, from_stage: str, on_log=None) -> str:
    """自动降级起始阶段：如果指定了from_stage但前置缓存文件不存在，自动回退到更早的阶段，避免报错。

    抽取公共逻辑，run和run_to_propositions复用。

    Args:
        edit_dir: 工作目录
        from_stage: 用户指定的起始阶段
        on_log: 日志回调函数，签名(stage, pct, msg)

    Returns:
        降级后的实际起始阶段
    """
    def _log(stage, pct, msg):
        if on_log:
            try:
                on_log(stage, pct, msg)
            except Exception:
                pass
    transcripts_dir_check = edit_dir / "transcripts"
    packed_path_check = edit_dir / "takes_packed.md"
    has_transcripts = transcripts_dir_check.exists() and any(transcripts_dir_check.glob("*.json"))
    has_packed = packed_path_check.exists()
    # 兼容两种模式的阶段名
    if from_stage in ("pack", "select", "propose") and not has_transcripts:
        _log("pack", 0, "⚠️  转录缓存不存在，自动降级为全量运行")
        from_stage = "all"
    elif from_stage in ("select", "propose") and not has_packed:
        _log("pack", 0, "⚠️  打包缓存不存在，自动从打包阶段开始")
        from_stage = "pack"
    return from_stage


def _run_transcribe_pack(edit_dir: Path, video: Path, app_key: str, from_stage: str, on_progress=None, total_stages: int = 4) -> None:
    """公共逻辑：执行转录+打包阶段，run和run_to_propositions复用。

    Args:
        edit_dir: 工作目录
        video: 输入视频路径
        app_key: 火山ASR密钥
        from_stage: 起始阶段
        on_progress: 进度回调
        total_stages: 总阶段数（全自动模式4阶段，命题模式5阶段），用于日志序号
    """
    def _p(stage, pct, msg):
        print(msg, flush=True)
        if on_progress:
            try:
                on_progress(stage, pct, msg.lstrip())
            except Exception:
                pass
    # 1. 转录（永远是第1阶段，不管总阶段数）
    if from_stage in ("all", "transcribe"):
        _p("transcribe", 0, f"\n[1/{total_stages}] 转录 {video.name} (火山 ASR)")
        transcribe_one(video, edit_dir, app_key, language=None, on_progress=on_progress)

    # 2. 打包（永远是第2阶段，不管总阶段数）
    packed_path = edit_dir / "takes_packed.md"
    transcripts_dir = edit_dir / "transcripts"
    need_pack = True
    if packed_path.exists() and transcripts_dir.exists():
        pack_mtime = packed_path.stat().st_mtime
        need_pack = False
        for json_file in transcripts_dir.glob("*.json"):
            if json_file.stat().st_mtime > pack_mtime:
                need_pack = True
                break
    if need_pack and from_stage in ("all", "transcribe", "pack"):
        _p("pack", 0, f"\n[2/{total_stages}] 打包转录 takes_packed.md")
        pack_transcripts(edit_dir, on_progress=on_progress)
    elif packed_path.exists():
        _p("pack", 100, f"\n[2/{total_stages}] 使用缓存打包结果，跳过")


def run_to_propositions(
    video: Path,
    edit_dir: Path,
    from_stage: str = "all",
    on_progress=None,
) -> list[dict]:
    """命题模式第一阶段：运行转录→打包→命题提取，返回命题列表（暂停等待用户选择）。

    Args:
        video: 输入视频路径
        edit_dir: 工作输出目录
        from_stage: 起始阶段（all/pack），用于缓存复用
        on_progress: 进度回调

    Returns:
        提取到的命题列表
    """
    app_key = volc_app_key()

    def _p(stage, pct, msg):
        print(msg, flush=True)
        if on_progress:
            try:
                on_progress(stage, pct, msg.lstrip())
            except Exception:
                pass

    # 自动降级起始阶段（复用公共逻辑）
    from_stage = _auto_downgrade_stage(edit_dir, from_stage, _p)

    # 1. 转录 + 2. 打包（复用公共逻辑，命题模式共5个阶段）
    _run_transcribe_pack(edit_dir, video, app_key, from_stage, on_progress=on_progress, total_stages=5)

    # 3. 命题提取（检查缓存）
    propositions = props_mod.load_propositions(edit_dir)
    packed_path = edit_dir / "takes_packed.md"
    if propositions:
        _p("propose", 100, f"\n[3/5] 使用缓存命题列表，共 {len(propositions)} 个看点")
    else:
        _p("propose", 0, f"\n[3/5] 🔍 AI正在通读全文识别精彩看点（约1-3分钟，请耐心等待…）")
        packed_md = packed_path.read_text(encoding="utf-8")
        video_dur = _probe_video_duration(video)

        def _prop_pct(pct: int, msg: str):
            _p("propose", pct, msg)

        _p("propose", 10, "  通读全文、识别精彩命题…")
        propositions = props_mod.extract_propositions(
            packed_md, video_dur,
            on_log=lambda msg: _p("propose", 50, msg),
        )
        if propositions:
            props_mod.save_propositions(edit_dir, propositions, video, video_dur)
            _p("propose", 100, f"  共发现 {len(propositions)} 个精彩看点")
            for p in propositions:
                dur = p["end"] - p["start"]
                print(f"    {p['id']}. {p['title']} (~{dur:.0f}s)")
        else:
            _p("propose", 100, "  ⚠️ 未提取到命题，请切换到全自动模式重试")

    return propositions


def run_from_selection(
    video: Path,
    edit_dir: Path,
    selected_prop_ids: list[int],
    grade: str = "auto",
    subtitles: bool = False,
    preview: bool = False,
    min_duration: float = 30.0,
    max_duration: float = 300.0,
    max_concurrency: int = 3,
    merge: bool = False,
    on_progress=None,
) -> dict:
    """命题模式第二阶段：根据用户选中的命题ID，精修选段并渲染。

    Args:
        video: 输入视频路径
        edit_dir: 工作输出目录
        selected_prop_ids: 用户选中的命题ID列表
        grade: 调色模式
        subtitles: 是否烧录硬字幕
        preview: 是否快速预览模式
        min_duration: 单条最小时长
        max_duration: 单条最大时长
        on_progress: 进度回调

    Returns:
        manifest字典
    """
    def _p(stage, pct, msg):
        print(msg, flush=True)
        if on_progress:
            try:
                on_progress(stage, pct, msg.lstrip())
            except Exception:
                pass

    # 每次渲染都创建新的带时间戳的真实目录，保证重剪版本独立，不覆盖旧版本
    clips_dir = _prepare_clips_dir(edit_dir)

    # 加载命题列表
    propositions = props_mod.load_propositions(edit_dir)
    if not propositions:
        raise RuntimeError("找不到命题缓存，请重新提取")
    selected_props = [p for p in propositions if p["id"] in selected_prop_ids]
    if not selected_props:
        raise RuntimeError("未选中任何有效命题")

    print(f"DEBUG: merge={merge}, 选中命题数={len(selected_props)}", flush=True)
    if merge:
        _p("refine", 0, f"\n[4/5] 智能合并 {len(selected_props)} 个选中命题…")
        # 合并模式进度固定提示，不需要计算分段进度
        def _refine_progress(_done, _total):
            _p("refine", 50, f"  整合总结内容并精修切点…")
    else:
        _p("refine", 0, f"\n[4/5] 精修 {len(selected_props)} 个选中命题的切点…")
        def _refine_progress(done: int, total: int):
            pct = int(80 * done / total) + 10
            _p("refine", pct, f"  精修进度 {done}/{total}")

    # 读取phrases用于精修和吸附
    packed_path = edit_dir / "takes_packed.md"
    packed_md = packed_path.read_text(encoding="utf-8")
    phrases = parse_phrases(packed_md)

    failed_props = []  # 记录精修失败的命题标题

    final_clips = props_mod.refine_propositions(
        phrases, selected_props,
        min_dur=min_duration, max_dur=max_duration,
        max_concurrency=max_concurrency,
        on_progress=_refine_progress,
        on_log=lambda msg: _p("refine", 50, msg),
        failed_props=failed_props,  # 传入列表收集失败项
        merge=merge,
    )

    if not final_clips:
        raise RuntimeError("所有命题精修失败，无法生成切片")

    # 最终成品阶段统一校验时长（精修阶段不卡最大时长）
    max_allowed = max_duration * 1.2
    valid_clips = []
    for c in final_clips:
        total_dur = sum(s["end"] - s["start"] for s in c["segments"])
        if total_dur > max_allowed:
            _p("refine", 50, f"  跳过\"{c['title']}\"：时长{total_dur:.1f}s超出最长限制{max_allowed:.0f}s")
            continue
        valid_clips.append(c)
    final_clips = valid_clips
    if not final_clips:
        raise RuntimeError(f"所有切片时长超出最长限制{max_allowed:.0f}s，请调大单条最长时长或减少选中内容")

    _p("refine", 100, f"  精修完成，共 {len(final_clips)} 条有效切片" + (f"，{len(failed_props)}个命题精修失败" if failed_props else ""))

    # 打印最终切片信息
    for i, c in enumerate(final_clips):
            segs = c["segments"]
            dur = sum(s["end"] - s["start"] for s in segs)
            segs_str = " + ".join(f"[{s['start']:7.2f}-{s['end']:7.2f}]" for s in segs)
            print(f"  {i+1}. {segs_str}  ({dur:5.1f}s)  {c['title']}")

    # 写EDL
    edl = {
        "version": 3,
        "mode": "multi_clip",
        "sources": {video.stem: str(video.resolve())},
        "clips": [
            {
                "source": video.stem,
                "segments": c["segments"],
                "title": c["title"],
                "reason": c.get("reason", ""),
            }
            for c in final_clips
        ],
        "grade": grade,
        "overlays": [],
        "subtitles": None,
        "total_duration_s": round(
            sum(s["end"] - s["start"] for c in final_clips for s in c["segments"]), 2),
    }
    edl_path = clips_dir / "edl_multi.json"
    edl_path.write_text(json.dumps(edl, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"saved: {edl_path}")

    # 5. 渲染
    _p("render", 0, f"\n[5/5] 渲染独立切片 → {clips_dir.name}")
    _p("render", 1, "  准备渲染…")
    paths, qc_flags = render.render_clips(
        edl, edit_dir, clips_dir,
        subtitles=subtitles, preview=preview,
        self_eval=not preview,
        _self_eval_fn=pick_clips.self_eval_clip,
        on_progress=on_progress,
    )

    # 写manifest
    edl_clips = edl.get("clips", [])
    from datetime import datetime
    manifest = {
        "video": str(video.resolve()),
        "output_dir": str(edit_dir.resolve()),
        "clips_dir": str(clips_dir.resolve()),
        "mode": "propose",
        "created_at": datetime.now().isoformat(),
        "failed_props": failed_props,
        "config": {
            "mode": "propose",
            "subtitles": subtitles,
            "preview": preview,
            "grade": grade,
            "min_duration": min_duration,
            "max_duration": max_duration,
            "merge": merge,
        },
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
    ap.add_argument("video", type=Path, help="输入的MP4直播/长视频文件路径")
    ap.add_argument("--edit-dir", type=Path, default=None,
                    help="工作目录（默认 data/output/<视频名>/，与Web UI历史记录共享，重名自动加(1)/(2)后缀；手动指定时使用自定义路径）")
    ap.add_argument("--min-duration", type=float, default=30.0,
                    help="单条切片最短时长（秒，默认30秒，适配短视频平台最低时长要求）")
    ap.add_argument("--max-duration", type=float, default=300.0,
                    help="单条切片最长时长（秒，默认300秒=5分钟，适配短视频平台发布限制）")
    ap.add_argument("--grade", type=str, default="auto",
                    help="调色模式：auto=智能自然微调(默认，幅度≤8%%无痕迹)/none=原色调无修改/light=轻度对比度/饱和度增强/warm_cinematic=暖调电影感风格")
    ap.add_argument("--subtitles", action="store_true",
                    help="烧录硬字幕到视频画面（默认关闭，仅生成独立 .srt 字幕文件，可自行选择是否挂载）")
    ap.add_argument("--preview", action="store_true",
                    help="快速预览模式：低质量快速渲染、跳过Vision质检，速度快3-5倍，仅用于验证选段效果，不适合最终发布")
    ap.add_argument("--from-stage", type=str, default="all",
                    choices=["all", "transcribe", "pack", "select", "propose"],
                    help="从指定阶段续跑复用缓存：all=从头完整跑(默认)/transcribe=跳过转录/pack=跳过转录+打包/select=仅重新选段和渲染/propose=跳过转录打包直接提取命题")
    ap.add_argument("--concurrent", type=int, default=3,
                    help="精修看点时的大模型并发数，默认3，网络好/API限额充足可调高加快速度，网络差可调低更稳定")
    # 命题模式专属参数
    ap.add_argument("--propose", action="store_true",
                    help="命题模式：运行到提取看点命题后暂停，展示所有精彩看点列表等待选择")
    ap.add_argument("--select-ids", type=str, default=None,
                    help="命题模式第二阶段：传入逗号分隔的选中命题ID，例如 --select-ids 1,3,5 精修渲染选中的看点")
    args = ap.parse_args()

    # 并发数参数校验：1~8之间
    args.concurrent = max(1, min(8, int(args.concurrent)))

    # 时长参数校验：10秒~10分钟，最小间隔1秒（与Web端规则一致）
    args.min_duration = max(10.0, min(float(args.min_duration), 599.0))
    if args.max_duration < args.min_duration + 1:
        args.max_duration = args.min_duration + 1
    args.max_duration = max(args.min_duration + 1.0, min(float(args.max_duration), 600.0))

    video = args.video.resolve()
    if not video.exists():
        sys.exit(f"视频不存在: {video}")

    if args.edit_dir:
        # 用户手动指定工作目录，使用自定义路径并确保存在（向后兼容旧用法）
        edit_dir = args.edit_dir.resolve()
        edit_dir.mkdir(parents=True, exist_ok=True)
    else:
        # 命题模式第二阶段（select-ids）：优先查找已有对应视频名的工作目录，复用第一阶段缓存
        if args.select_ids:
            edit_dir = _find_latest_edit_dir(video.stem)
            if edit_dir:
                print(f"📂 自动找到已有工作目录: {edit_dir}")
            else:
                print(f"⚠️  未找到对应视频的已有工作目录，将创建新目录")
                edit_dir = _unique_path(OUTPUT_DIR, video.stem)
        else:
            # 全自动模式/命题模式第一阶段：默认输出到统一目录，与Web UI历史记录共享，自动处理重名避免覆盖
            edit_dir = _unique_path(OUTPUT_DIR, video.stem)

    print(f"📂 输出工作目录: {edit_dir}")

    # 参数互斥校验
    if args.propose and args.select_ids:
        sys.exit("❌ --propose 和 --select-ids 不能同时使用：--propose 用于第一阶段提取命题，--select-ids 用于第二阶段精修渲染")

    if args.propose:
        # 命题模式第一阶段：提取看点命题
        print("\n🚀 启动命题模式：第一阶段（转录→打包→提取看点）")
        propositions = run_to_propositions(
            video=video, edit_dir=edit_dir,
            from_stage=args.from_stage,
        )
        if propositions:
            print("\n✅ 看点提取完成！请选择你想要生成切片的看点ID，直接运行第二阶段即可，不需要手动指定工作目录：")
            print(f'   python cli.py "{video.name}" --select-ids <逗号分隔的ID列表> [其他参数]')
            print(f"   示例：python cli.py \"{video.name}\" --select-ids 1,3,5 --subtitles")
    elif args.select_ids:
        # 命题模式第二阶段：根据选中ID精修渲染
        try:
            selected_ids = [int(x.strip()) for x in args.select_ids.split(",") if x.strip()]
        except ValueError:
            sys.exit(f"❌ 无效的命题ID格式：{args.select_ids}，请传入逗号分隔的整数ID，例如 1,3,5")
        if not selected_ids:
            sys.exit("❌ 未传入任何有效的命题ID")
        print(f"\n🚀 启动命题模式：第二阶段（精修选段→渲染），选中命题ID：{selected_ids}")
        run_from_selection(
            video=video, edit_dir=edit_dir,
            selected_prop_ids=selected_ids,
            grade=args.grade,
            subtitles=args.subtitles, preview=args.preview,
            min_duration=args.min_duration, max_duration=args.max_duration,
            max_concurrency=args.concurrent,
        )
    else:
        # 默认全自动模式
        run(
            video=video, edit_dir=edit_dir,
            min_duration=args.min_duration, max_duration=args.max_duration,
            grade=args.grade,
            subtitles=args.subtitles, preview=args.preview,
            from_stage=args.from_stage,
        )


if __name__ == "__main__":
    main()
