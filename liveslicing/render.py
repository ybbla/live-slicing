"""EDL视频渲染模块。

核心功能：按照固定渲染流水线从EDL（编辑决策列表）生成最终视频文件，支持单片段和多片段拼接、色彩分级、字幕烧录、音频响度标准化等完整生产级能力。

渲染流水线严格按以下顺序执行：
  1. 逐片段提取：内置色彩分级+30ms音频淡入淡出，避免硬切爆音
  2. 片段拼接：单片段优先无损复制，多片段优先交叉淡入淡出（失败降级为硬拼接）
  3. 字幕处理：字幕滤镜始终放在滤镜链最后，保证不被其他滤镜覆盖
  4. 音频处理：两遍响度标准化，目标为国内短视频平台通用标准-16 LUFS

支持EDL版本：
  - v1版本：单源片段数组ranges[]格式
  - v3版本：多片段数组clips[].segments[]格式

字幕处理规则：
  - 使用微软雅黑字体保证CJK字符正常显示，带2px黑色描边保证任意背景可读性
  - 直接复用ASR返回的已断句完整句子作为字幕条目，不额外自动拆分，长句由渲染器自动换行
  - 竖屏视频自动调整底部边距，避开TikTok/Reels/视频号等平台底部UI控件区域

使用示例：
    python -m liveslicing.render <edl.json> -o final.mp4
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

# 项目根目录，用于解析静态资源路径（背景图等）
PROJECT_ROOT = Path(__file__).resolve().parent.parent

try:
    from liveslicing.grade import get_preset, auto_grade_for_clip
except Exception:
    def get_preset(name: str) -> str:
        """获取色彩分级预设，导入失败时的降级实现。

        Args:
            name: 预设名称
        Returns:
            空字符串，降级为无色彩调整
        """
        return ""

    def auto_grade_for_clip(video, start=0.0, duration=None, verbose=False):  # type: ignore
        """自动色彩分级，导入失败时的降级实现。

        Args:
            video: 视频路径
            start: 片段起始时间，单位秒
            duration: 片段时长，单位秒
            verbose: 是否输出详细日志
        Returns:
            温和的默认对比度/饱和度调整参数，空统计信息字典
        """
        return "eq=contrast=1.03:saturation=0.98", {}


try:
    from liveslicing.timeline_view import render_timeline  # 用于自评估质量检查
except Exception:
    render_timeline = None  # type: ignore


# -------- 字幕样式（粗体叠加方案，已在1920×1080横屏和1080×1920竖屏验证） --
#
# MarginV不是审美选择，而是平台安全区强制规则：
# TikTok/IG Reels/Shorts等短视频平台的UI（标题、用户名、音乐信息、右侧操作栏）
# 会覆盖竖屏画面底部约25%-30%的区域，放在靠近底部的字幕会被UI遮挡。
# libass会基于PlayResY=288自动缩放渲染画布，因此MarginV=90在任意比例下
# 都会让字幕基线位于距离底部约30%的位置，避开所有主流竖屏平台的UI区域。
# 无特殊原因不要将该值设置到75以下。
# 字幕基础样式（MarginV会根据视频比例/方向动态替换）：
# FontName=Microsoft YaHei是Windows内置CJK字体，粗体字形完整
# Outline=2提供2px黑色描边，任意背景下都可读
SUB_FORCE_STYLE_BASE = (
    "FontName=Microsoft YaHei,FontSize=18,Bold=1,"
    "PrimaryColour=&H00FFFFFF,OutlineColour=&H00000000,BackColour=&H00000000,"
    "BorderStyle=1,Outline=2,Shadow=0,"
    "Alignment=2"
)


def get_subtitle_style(video_path: Path) -> str:
    """根据视频方向返回调整了MarginV的字幕样式字符串。

    Args:
        video_path: 视频文件路径，用于探测分辨率
    Returns:
        完整的force_style字符串，包含适配方向的MarginV值

    MarginV是libass中字幕基线到画面底部的距离（单位为PlayResY=288的画布单位）：
    - 横屏：1080p下距离底部约150px → MarginV=40
    - 竖屏（短视频）：1920高度下距离底部约330px → MarginV=50
      避开TikTok/Reels/视频号等平台底部覆盖约25%屏幕的UI控件
    """
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=width,height",
             "-of", "csv=p=0", str(video_path)],
            capture_output=True, text=True, check=True,
            encoding="utf-8", errors="replace",
        )
        w, h = map(int, out.stdout.strip().split(","))
        margin_v = 50 if h > w else 40  # 竖屏需要更大的底部安全边距
    except Exception:
        margin_v = 45  # 探测失败时的安全默认值
    return f"{SUB_FORCE_STYLE_BASE},MarginV={margin_v}"

# -------- 竖屏背景+标题合成常量（适配国内短视频平台9:16标准） --
#
# -------- 竖屏背景+标题合成常量 --
#
# 画布宽度固定为1080px，高度由背景图等比缩放后的高度动态决定
# 默认背景图general.png原始尺寸941x1672≈9:16，缩放至1080宽时高度≈1920px
# 符合抖音/视频号/小红书/快手平台9:16标准；更换其他比例背景图时高度自动适配
BG_CANVAS_W = 1080
# 标题默认样式配置，位置完全动态计算无固定偏移
BG_TITLE_FONT_SIZE = 42
BG_TITLE_OUTLINE = 2
# 默认通用背景图路径（竖屏）
DEFAULT_BG_PATH = PROJECT_ROOT / "background" / "general.png"

# -------- 工具函数 ------------------------------------------------------------


def run(cmd: list[str], quiet: bool = False) -> None:
    """执行ffmpeg等外部命令，默认打印简化的命令行。

    Args:
        cmd: 要执行的命令及参数列表
        quiet: 是否静默执行，不打印命令行
    """
    if not quiet:
        print(f"  $ {' '.join(str(c) for c in cmd[:6])}{' …' if len(cmd) > 6 else ''}")
    subprocess.run(cmd, check=True)


def resolve_grade_filter(grade_field: str | None) -> str:
    """解析EDL中的grade字段，支持预设名、原生ffmpeg滤镜或自动分级模式。

    Args:
        grade_field: EDL中的grade字段值，可以是预设名、ffmpeg滤镜字符串或"auto"
    Returns:
        要嵌入到逐片段-vf链中的滤镜字符串；如果是自动模式返回哨兵值"__AUTO__"，
        会在逐片段处理时动态解析
    """
    if not grade_field:
        return ""
    if grade_field == "auto":
        return "__AUTO__"
    # 预设名是短标识符，滤镜字符串包含'='或','
    if re.fullmatch(r"[a-zA-Z0-9_\-]+", grade_field):
        try:
            return get_preset(grade_field)
        except KeyError:
            print(f"warning: unknown preset '{grade_field}', using as raw filter")
            return grade_field
    return grade_field


def resolve_path(maybe_path: str, base: Path) -> Path:
    """解析路径，支持绝对路径或相对于base目录的相对路径。

    Args:
        maybe_path: 待解析的路径字符串
        base: 相对路径的基准目录
    Returns:
        解析后的绝对Path对象
    """
    p = Path(maybe_path)
    if p.is_absolute():
        return p
    return (base / p).resolve()


# -------- HDR → SDR色调映射（处理HLG/PQ格式源） --------------------------
#
# iPhone默认使用Rec.2020色域的HLG HDR格式录制，很多微单相机使用PQ HDR格式。
# 如果源是HDR格式，我们仅做位深下转换（yuv420p10le → yuv420p）而不做色调映射，
# 输出虽然是8bit但仍然携带HLG/PQ传输元数据。遵守元数据的播放器（录屏软件、
# 大多数社交平台上传重编码）会将8bit值按HDR容器解释，结果会出现过饱和/过曝。
# macOS的QuickTime本地播放可能隐藏该问题，但录屏和上传后的渲染结果无法避免。
#
# 解决方案：通过color_transfer检测HDR源，在vf链最前面添加zscale+tonemap链，
# 保证输出是干净的Rec.709 SDR格式。

HDR_TRANSFERS = {"smpte2084", "arib-std-b67"}  # PQ (HDR10) 和 HLG 两种HDR传输函数

TONEMAP_CHAIN = (
    "zscale=t=linear:npl=100,"
    "format=gbrpf32le,"
    "zscale=p=bt709,"
    "tonemap=tonemap=hable:desat=0,"
    "zscale=t=bt709:m=bt709:r=tv,"
    "format=yuv420p"
)


def is_hdr_source(video: Path) -> bool:
    """检测视频源是否使用PQ或HLG HDR传输函数。

    Args:
        video: 视频文件路径
    Returns:
        是HDR源返回True，否则返回False（探测失败默认返回False）
    """
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=color_transfer",
             "-of", "default=noprint_wrappers=1:nokey=1", str(video)],
            capture_output=True, text=True, check=True,
            encoding="utf-8", errors="replace",
        )
        return out.stdout.strip() in HDR_TRANSFERS
    except subprocess.CalledProcessError:
        return False


def is_portrait_source(video: Path) -> bool:
    """检测视频是否为竖屏（高度大于宽度）。

    Args:
        video: 视频文件路径
    Returns:
        竖屏返回True，横屏或探测失败返回False
    """
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=width,height",
             "-of", "csv=p=0", str(video)],
            capture_output=True, text=True, check=True,
            encoding="utf-8", errors="replace",
        )
        w, h = map(int, out.stdout.strip().split(","))
        return h > w
    except Exception:
        return False


# -------- 逐片段提取（规则2 + 规则3） --------------------------


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
    """提取单个剪辑片段为独立MP4文件，内置色彩分级和音频淡入淡出。

    Args:
        source: 源视频路径
        seg_start: 片段在源视频中的起始时间，单位秒
        duration: 片段时长，单位秒
        grade_filter: 要应用的色彩分级滤镜字符串，空字符串表示不应用
        out_path: 输出片段文件路径
        preview: 是否为预览模式，使用平衡的质量/速度参数
        draft: 是否为草稿模式，使用最快的编码参数仅用于检查剪辑点
        fade_duration: 音频淡入淡出时长，单位秒，默认30ms

    实现说明：
    - `-ss`放在`-i`之前实现快速精准seek
    - 4K源自动缩放至1080p，竖屏源按高度缩放保持方向
    质量阶梯：
      - 最终输出（默认）：1080p libx264 fast CRF 20
      - 预览模式：1080p libx264 medium CRF 22，可用于质量检查，速度快于最终输出
      - 草稿模式：720p libx264 ultrafast CRF 28，仅用于剪辑点检查
    """
    out_path.parent.mkdir(parents=True, exist_ok=True)

    portrait = is_portrait_source(source)
    # 探测原视频分辨率：只做向下缩放，不做无意义放大避免画质劣化
    src_w, src_h = 1920, 1080  # 默认值，探测失败时使用
    try:
        probe_out = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=width,height",
             "-of", "csv=p=0", str(source)],
            capture_output=True, text=True, check=True, encoding="utf-8", errors="replace",
        )
        src_w, src_h = map(int, probe_out.stdout.strip().split(","))
    except Exception:
        pass  # 探测失败时使用默认值

    if draft:
        # 草稿模式统一缩放到720p预览
        scale = "scale=-2:1280" if portrait else "scale=1280:-2"
    else:
        if portrait:
            # 竖屏：高度超过1920才缩放到1920，否则保持原分辨率不放大
            target_h = 1920 if src_h > 1920 else src_h
            scale = f"scale=-2:{target_h}"
        else:
            # 横屏：宽度超过1920才缩放到1920，否则保持原分辨率不放大
            target_w = 1920 if src_w > 1920 else src_w
            scale = f"scale={target_w}:-2"

    vf_parts: list[str] = []
    if is_hdr_source(source):
        vf_parts.append(TONEMAP_CHAIN)
    # 只有需要缩放时才添加scale滤镜，原分辨率≤1080p时无缩放直接处理
    need_scale = (draft or (portrait and src_h > 1920) or (not portrait and src_w > 1920))
    if need_scale:
        vf_parts.append(scale)
    if grade_filter:
        vf_parts.append(grade_filter)
    vf = ",".join(vf_parts)

    # 片段首尾添加音频淡入淡出，避免硬切产生爆音
    fade_dur = max(0.01, min(0.1, fade_duration))  # 限制在10-100ms区间
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
    ]
    # 只有有视频滤镜时才添加-vf参数（HDR转换/缩放/调色）
    if vf:
        cmd += ["-vf", vf]
    cmd += [
        "-af", af,
        "-c:v", "libx264", "-preset", preset, "-crf", crf,
        "-pix_fmt", "yuv420p",
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
    """提取EDL中所有片段到edit_dir/clips_graded/seg_NN.mp4。

    Args:
        edl: EDL字典对象
        edit_dir: 编辑工作目录，EDL文件所在目录
        preview: 是否为预览模式
        draft: 是否为草稿模式
        fade_duration: 音频淡入淡出时长，单位秒
    Returns:
        按顺序排列的片段文件路径列表

    实现说明：
    - 如果EDL的grade字段为"auto"，会对每个片段单独调用auto_grade_for_clip分析，
      应用针对性的细微色彩校正
    - 否则所有片段应用相同的预设或原生滤镜
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


# -------- 片段拼接：内部剪辑点使用交叉淡入淡出 ---------------------------
# 内部片段衔接处使用短交叉淡入淡出（视频xfade + 音频acrossfade），
# 消除逐片段30ms淡入淡出导致的硬切感和音频爆音凹陷。
# 第一个和最后一个片段的边缘仍然保留30ms淡入淡出作为爆音保护。

XFADE_DURATION = 0.15   # 150ms视频交叉淡入淡出
ACROSSFADE_DURATION = 0.10  # 100ms音频交叉淡入淡出


def _probe_duration(video: Path) -> float:
    """探测视频文件总时长。

    Args:
        video: 视频文件路径
    Returns:
        视频时长，单位秒；探测失败返回0.0
    """
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", str(video)],
            capture_output=True, text=True, check=True,
            encoding="utf-8", errors="replace",
        )
        return float(out.stdout.strip() or 0.0)
    except Exception:
        return 0.0


def concat_segments(segment_paths: list[Path], out_path: Path, edit_dir: Path,
                    xfade_duration: float = XFADE_DURATION, acrossfade_duration: float = ACROSSFADE_DURATION) -> list[float]:
    """拼接多个片段为单个视频文件。

    Args:
        segment_paths: 按顺序排列的片段文件路径列表
        out_path: 输出拼接后视频路径
        edit_dir: 编辑工作目录
        xfade_duration: 视频交叉淡入淡出时长，单位秒
        acrossfade_duration: 音频交叉淡入淡出时长，单位秒

    Returns:
        每个片段在最终输出视频中的开始时间偏移列表（单位秒），用于字幕时间轴精确对齐

    实现说明：
    - 单片段直接复制（提取阶段已经添加了边缘淡入淡出）
    - 多片段使用交叉淡入淡出实现平滑衔接，消除硬切和音频凹陷
    - 交叉淡入淡出失败时降级为普通滤镜拼接（如片段过短不足以支持淡入淡出时长）
    - 返回的偏移量精确匹配最终输出时间轴，避免字幕不同步问题
    """
    out_path.parent.mkdir(parents=True, exist_ok=True)
    n = len(segment_paths)

    if n == 1:
        # 单片段：直接复制即可，提取阶段已经添加了边缘淡入淡出
        import shutil
        shutil.copyfile(segment_paths[0], out_path)
        return [0.0]

    # 多片段：使用xfade + acrossfade实现内部衔接平滑过渡
    xfade_dur = max(0.05, min(0.3, xfade_duration))
    acrossfade_dur = max(0.03, min(0.2, acrossfade_duration))
    print(f"concat (with {xfade_dur*1000:.0f}ms crossfades) → {out_path.name}")

    durations = [_probe_duration(p) for p in segment_paths]
    inputs: list[str] = []
    for p in segment_paths:
        inputs += ["-i", str(p)]

    # 预计算交叉淡入淡出模式下的每个片段起始偏移
    xfade_offsets = [0.0]
    cur_offset = 0.0
    for i in range(n - 1):
        cur_offset += durations[i] - xfade_dur
        xfade_offsets.append(round(cur_offset, 3))

    filter_parts: list[str] = []
    # 从第一个片段开始
    cur_v = "[0:v]"
    cur_a = "[0:a]"
    offset = durations[0] - xfade_dur  # 第一个交叉淡入淡出的偏移量

    for i in range(1, n):
        next_v = f"[{i}:v]"
        next_a = f"[{i}:a]"
        out_v_label = f"[v{i}]"
        out_a_label = f"[a{i}]"
        # 视频交叉淡入淡出
        filter_parts.append(
            f"{cur_v}{next_v}xfade=transition=fade:duration={xfade_dur}:offset={offset:.3f}{out_v_label}"
        )
        # 音频交叉淡入淡出，使用三角淡入淡出曲线
        filter_parts.append(
            f"{cur_a}{next_a}acrossfade=d={acrossfade_dur}:c1=tri:c2=tri{out_a_label}"
        )
        cur_v = out_v_label
        cur_a = out_a_label
        # 更新下一个交叉淡入淡出的偏移量：加上当前片段交叉淡入淡出后的剩余时长
        offset += durations[i] - xfade_dur

    filter_complex = ";".join(filter_parts)

    cmd = [
        "ffmpeg", "-y",
        *inputs,
        "-filter_complex", filter_complex,
        "-map", cur_v, "-map", cur_a,
        "-c:v", "libx264", "-preset", "fast", "-crf", "20",
        "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "192k", "-ar", "48000",
        "-movflags", "+faststart",
        str(out_path),
    ]
    try:
        subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        return xfade_offsets
    except subprocess.CalledProcessError as e:
        # 交叉淡入淡出失败时降级为普通拼接（如片段过短不足以支持淡入淡出时长）
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
            "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "192k", "-ar", "48000",
            "-movflags", "+faststart",
            str(out_path),
        ]
        subprocess.run(cmd_fb, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        # 普通concat无重叠，偏移量按片段时长累加
        plain_offsets = [0.0]
        cur_offset = 0.0
        for i in range(n - 1):
            cur_offset += durations[i]
            plain_offsets.append(round(cur_offset, 3))
        return plain_offsets


# -------- 主SRT字幕生成（规则5） ------------------------------------------------


PUNCT_BREAK = set(".,!?;:")


def _srt_timestamp(seconds: float) -> str:
    """将秒数转换为SRT格式时间戳（HH:MM:SS,mmm）。

    Args:
        seconds: 时间，单位秒
    Returns:
        SRT格式时间戳字符串
    """
    total_ms = int(round(seconds * 1000))
    h, rem = divmod(total_ms, 3600_000)
    m, rem = divmod(rem, 60_000)
    s, ms = divmod(rem, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def _utterances_in_range(transcript: dict, t_start: float, t_end: float) -> list[dict]:
    """获取转录结果中时间范围与[t_start, t_end]重叠的所有句子（带标点）。

    Args:
        transcript: 转录结果字典
        t_start: 起始时间，单位秒
        t_end: 结束时间，单位秒
    Returns:
        重叠的句子对象列表
    """
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


def build_master_srt(edl: dict, edit_dir: Path, out_path: Path, seg_offsets: list[float] | None = None) -> None:
    """根据每个源文件的转录结果生成输出时间线的SRT字幕文件。

    Args:
        edl: EDL字典对象
        edit_dir: 编辑工作目录
        out_path: 输出SRT文件路径
        seg_offsets: 每个片段在最终输出视频中的真实起始偏移量列表（由concat_segments返回），
            不传则 fallback 到按原始片段时长累加（单片段场景准确，多片段场景会有xfade偏移误差）

    实现说明：
    - 直接使用火山ASR返回的utterances作为字幕条目（每个utterance对应一条字幕），ASR已经完成自然断句并带有正确标点，无需额外自动断句/拆分
    - 长句子由libass渲染时自动按屏幕宽度换行，不手动控制换行位置
    - 文本保留原始大小写、标点和说话人表达习惯，可读性最好
    - 优先使用传入的seg_offsets精确对齐时间轴，多段拼接时完美匹配交叉淡入淡出后的实际时长，无字幕偏移
    """
    sources = edl["sources"]

    entries: list[tuple[float, float, str]] = []

    # 优先使用传入的明确转录文件路径（解决文件名匹配问题），否则自动查找
    tr_path = None
    if "_transcript_path" in edl:
        tr_path = Path(edl["_transcript_path"])
    else:
        # 自动查找转录文件：兼容任意文件名，一个任务通常只有一个转录文件
        for search_dir in (edit_dir / "transcripts", edit_dir.parent / "transcripts"):
            if search_dir.exists() and search_dir.is_dir():
                json_files = list(search_dir.glob("*.json"))
                if json_files:
                    tr_path = json_files[0]
                    break

    if not tr_path or not tr_path.exists():
        print(f"  no transcript file found, skipping subtitles")
        out_path.write_text("", encoding="utf-8-sig")
        return

    transcript = json.loads(tr_path.read_text(encoding="utf-8"))

    # 兼容两种EDL格式：完整EDL用"ranges"字段，单条切片子EDL用"segments"字段
    ranges = edl.get("ranges") or edl.get("segments") or []

    # 如果没有传入真实偏移量，fallback到按原始时长累加（单片段场景完全准确）
    if seg_offsets is None:
        seg_offsets = []
        cum_offset = 0.0
        for r in ranges:
            seg_offsets.append(cum_offset)
            cum_offset += float(r["end"]) - float(r["start"])

    for seg_idx, r in enumerate(ranges):
        seg_start = float(r["start"])
        seg_end = float(r["end"])
        seg_offset = seg_offsets[seg_idx] if seg_idx < len(seg_offsets) else 0.0
        utts = _utterances_in_range(transcript, seg_start, seg_end)

        if utts:
            # 直接复用ASR返回的已断句utterances：每个完整句子对应一条字幕条目，不再额外自动断句/拆分
            # 长句子由libass渲染时自动按宽度换行，无需手动拆分控制换行位置
            for u in utts:
                u_start = max(seg_start, float(u["start"]))
                u_end = min(seg_end, float(u["end"]))
                sub_text = (u.get("text", "") or "").strip()
                if not sub_text:
                    continue
                # 处理跨边界字幕：utterance可见时长小于0.5秒的直接丢弃，避免字幕闪一下
                visible_dur = u_end - u_start
                if visible_dur < 0.5:
                    continue
                out_start = max(0.0, u_start - seg_start) + seg_offset
                out_end = max(0.0, u_end - seg_start) + seg_offset
                if out_end <= out_start:
                    out_end = out_start + 0.4
                entries.append((out_start, out_end, sub_text))

    # 排序并写入SRT文件
    entries.sort(key=lambda e: e[0])
    lines: list[str] = []
    for i, (a, b, t) in enumerate(entries, start=1):
        lines.append(str(i))
        lines.append(f"{_srt_timestamp(a)} --> {_srt_timestamp(b)}")
        lines.append(t)
        lines.append("")
    out_path.write_text("\n".join(lines), encoding="utf-8-sig")
    print(f"master SRT → {out_path.name} ({len(entries)} cues)")


# -------- 响度标准化（适配国内短视频平台标准） -----------------------


# 国内短视频平台通用标准：-16 LUFS集成响度，-1.5 dBTP真实峰值，响度范围LRA 11 LU
# 符合抖音/视频号/小红书/B站等平台响度规范，避免平台二次压缩导致音质损失
# 符合YouTube/Instagram/TikTok/X/LinkedIn等所有主流平台的响度目标
LOUDNORM_I = -16.0  # 国内短视频平台（抖音/视频号/小红书/B站）通用标准，-14 LUFS为YouTube标准
LOUDNORM_TP = -1.5  # 真实峰值上限-1.5dBTP，避免平台转码时爆音
LOUDNORM_LRA = 11.0


def measure_loudness(video_path: Path) -> dict[str, str] | None:
    """执行ffmpeg loudnorm第一遍测量，解析JSON格式的测量结果。

    Args:
        video_path: 待测量的视频文件路径
    Returns:
        包含measured_i、measured_tp、measured_lra、measured_thresh、target_offset的字典；
        测量失败返回None
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
    proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    # loudnorm滤镜在运行结束时将JSON结果输出到stderr
    stderr = proc.stderr

    # 查找JSON块：loudnorm输出中包含一个`{ ... }`块
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
    """对视频执行两遍响度标准化，输出标准化后的文件。

    Args:
        input_path: 输入视频路径
        output_path: 输出标准化后视频路径
        preview: 是否为预览模式，预览模式使用单遍近似加快速度
    Returns:
        成功返回True，测量失败返回False（调用方应降级为直接复制输入文件）

    实现说明：
    - 预览模式跳过测量遍，使用单遍近似，速度更快，精度略低
    - 最终输出模式始终执行标准两遍处理，保证响度精度符合平台标准
    """
    if preview:
        # 单遍近似：速度更快，精度略低
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

    # 完整两遍处理
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


# -------- 最终合成（规则1 + 规则4） -------------------------------


def build_final_composite(
    base_path: Path,
    overlays: list[dict],
    subtitles_path: Path | None,
    out_path: Path,
    edit_dir: Path,
    source_video: Path | None = None,
) -> None:
    """最终合成 pass：基础视频 → 叠加层（PTS偏移）→ 最后添加字幕 → 输出。

    Args:
        base_path: 基础拼接后视频路径
        overlays: 叠加层配置列表，每个包含file、start_in_output、duration字段
        subtitles_path: 字幕文件路径，None表示不添加字幕
        out_path: 输出最终合成视频路径
        edit_dir: 编辑工作目录
        source_video: 源视频路径，用于探测分辨率适配字幕样式

    实现说明：
    - 如果没有叠加层和字幕，直接复制基础视频到输出
    - 字幕滤镜始终放在滤镜链最后，避免被其他滤镜覆盖或变形
    - Windows路径特殊处理：冒号和反斜杠需要转义，否则会被ffmpeg subtitles滤镜解析为特殊字符
    """
    has_overlays = bool(overlays)
    has_subs = subtitles_path is not None and subtitles_path.exists()

    if not has_overlays and not has_subs:
        # 没有需要合成的内容，直接复制基础视频到最终文件名
        run(["ffmpeg", "-y", "-i", str(base_path), "-c", "copy", str(out_path)], quiet=True)
        return

    inputs: list[str] = ["-i", str(base_path)]
    for ov in overlays:
        ov_path = resolve_path(ov["file"], edit_dir)
        inputs += ["-i", str(ov_path)]

    filter_parts: list[str] = []
    # 对每个叠加层做PTS偏移，使其第0帧对应输出中的start_in_output时间点
    for idx, ov in enumerate(overlays, start=1):
        t = float(ov["start_in_output"])
        filter_parts.append(f"[{idx}:v]setpts=PTS-STARTPTS+{t}/TB[a{idx}]")

    # 在基础视频上依次叠加所有叠加层
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

    # 字幕最后处理（规则1）
    # ffmpeg的subtitles滤镜解析文件名时会特殊处理':'和'\'，
    # Windows下必须转义盘符冒号，使用正斜杠避免反斜杠被当作转义符吃掉
    if has_subs:
        # Windows下字幕路径处理：使用绝对路径，转义特殊字符，优先使用微软雅黑字体确保中文显示
        subs_abs = str(subtitles_path.resolve()).replace("\\", "/").replace(":", r"\:").replace("'", r"\'").replace("[", r"\[").replace("]", r"\]")
        sub_style = get_subtitle_style(source_video or base_path) if source_video else SUB_FORCE_STYLE_BASE + ",MarginV=45"
        filter_parts.append(
            f"{current}subtitles='{subs_abs}':charenc=UTF-8:force_style='{sub_style}'[outv]"
        )
        out_label = "[outv]"
    else:
        # 没有字幕时将最后一个叠加层输出重命名为[outv]保持一致性
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
    try:
        result = subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, encoding="utf-8", errors="replace")
        if result.stderr and ("error" in result.stderr.lower() or "cannot" in result.stderr.lower() or "failed" in result.stderr.lower()):
            print(f"  ffmpeg warning: {result.stderr[-500:]}")
    except subprocess.CalledProcessError as e:
        print(f"  compositing failed (subtitles may not be rendered): {e.stderr[-500:] if e.stderr else str(e)}")
        # 降级：如果字幕合成失败，直接复制原视频，不中断整个流程
        run(["ffmpeg", "-y", "-i", str(base_path), "-c", "copy", str(out_path)], quiet=True)


def apply_background_title(
    input_path: Path,
    title: str,
    output_path: Path,
    preview: bool = False,
) -> bool:
    """将视频贴到竖屏背景图上，并在顶部绘制标题。

    画布宽度固定为BG_CANVAS_W(1080px)，高度由背景图等比缩放后的高度动态决定，默认9:16下为1920px：
    1. 背景图等比缩放到1080宽（预览960宽），高度按比例自适应，完整显示不裁剪
    2. 原视频等比缩放到1080宽（预览960宽），高度按比例自适应，不裁剪拉伸，保持原始宽高比
    3. 视频在背景图上水平+垂直完全居中放置
    4. 标题水平居中，垂直居中于背景顶部(y=0)到视频顶部之间的空白区域
    5. 标题自动折行最多2行，特殊字符自动转义

    Args:
        input_path: 输入视频路径（已完成调色、字幕烧录的中间视频）
        title: 顶部显示的标题文本，为空时仅贴背景不显示标题
        output_path: 输出合成后视频路径
        preview: 是否为预览模式，预览模式宽度960使用更快的编码参数

    Returns:
        合成成功返回True，失败（背景图不存在/ffmpeg执行错误）返回False，调用方应降级为直接使用原视频
    """
    if not DEFAULT_BG_PATH.exists():
        print(f"  warning: background image not found at {DEFAULT_BG_PATH}, skipping background compositing")
        return False

    output_path.parent.mkdir(parents=True, exist_ok=True)

    # 探测视频总时长
    video_dur = _probe_duration(input_path)
    if video_dur <= 0:
        print(f"  warning: failed to probe video duration, skipping background compositing")
        return False

    # 探测输入视频原始宽高，用于计算缩放后视频尺寸和标题位置
    src_fg_w, src_fg_h = 1920, 1080  # 探测失败时的安全默认值（横屏16:9）
    try:
        probe_out = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=width,height",
             "-of", "csv=p=0", str(input_path)],
            capture_output=True, text=True, check=True,
            encoding="utf-8", errors="replace",
        )
        src_fg_w, src_fg_h = map(int, probe_out.stdout.strip().split(","))
    except Exception:
        print(f"  warning: failed to probe video dimensions, using defaults")

    # 探测背景图原始宽高，用于计算画布高度
    src_bg_w, src_bg_h = 941, 1672  # 默认背景图尺寸
    try:
        probe_bg = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=width,height",
             "-of", "csv=p=0", str(DEFAULT_BG_PATH)],
            capture_output=True, text=True, check=True,
            encoding="utf-8", errors="replace",
        )
        src_bg_w, src_bg_h = map(int, probe_bg.stdout.strip().split(","))
    except Exception:
        print(f"  warning: failed to probe background image dimensions, using defaults")

    # 处理标题：自动折行为最多两行，转义特殊字符直接传入drawtext，避免临时文件编码问题
    title_text = ""
    if title and title.strip():
        title_clean = title.strip()
        if len(title_clean) <= 12:
            # 短标题单行显示
            title_text = title_clean
        else:
            # 长标题强制折为两行，每行不超过12字，保证在1080宽度内不自动折行
            mid = len(title_clean) // 2
            # 优先在标点/空格位置折行
            for i in range(mid-3, mid+3):
                if i < len(title_clean) and title_clean[i] in ("，", "。", "！", "？", "、", " ", "："):
                    mid = i + 1
                    break
            line1 = title_clean[:mid][:12]
            line2 = title_clean[mid:][:12]
            title_text = f"{line1}\n{line2}"
        # 转义ffmpeg drawtext特殊字符：冒号、单引号、反斜杠、换行等
        title_text = title_text.replace("\\", "\\\\").replace(":", r"\:").replace("'", r"\'").replace("[", r"\[").replace("]", r"\]").replace("\n", r"\n")

    try:
        # 预计算所有尺寸常量
        scale_w = 960 if preview else BG_CANVAS_W  # 预览模式960宽，正式1080宽
        # 背景图等比缩放到scale_w宽，高度按比例自适应，取偶数（yuv420p编码要求）
        bg_scaled_h = int(round(scale_w * src_bg_h / src_bg_w / 2) * 2)
        bg_scaled_h = max(bg_scaled_h, 2)  # 保证最小高度
        # 前景视频等比缩放到scale_w宽，高度按比例自适应，取偶数
        fg_h = int(round(scale_w * src_fg_h / src_fg_w / 2) * 2)
        fg_h = max(fg_h, 2)  # 保证最小高度
        # 视频在背景上垂直居中时，视频顶部到画布顶部的y坐标（水平居中时x=0，因为宽度相同）
        fg_top_y = (bg_scaled_h - fg_h) // 2

        # 构建滤镜链
        filter_parts = []
        # 1. 背景图等比缩放到目标宽度，高度按比例自适应，不裁剪，完整显示
        filter_parts.append(
            f"[0:v]scale={scale_w}:-2[bg]"
        )
        # 2. 输入视频缩放：等比缩放到满宽（1080/960），高度按比例自适应，-2保证偶数符合yuv420p要求
        filter_parts.append(f"[1:v]scale={scale_w}:-2[fg]")
        # 3. 视频在背景图上水平垂直居中：宽度相同x=0，y为预计算的fg_top_y
        filter_parts.append(f"[bg][fg]overlay=x=0:y={fg_top_y}[bg_vid]")
        # 4. 标题：水平居中，垂直居中在背景顶部(y=0)到视频顶部(y=fg_top_y)之间的空白区域
        out_label = "[bg_vid]"
        if title_text:
            filter_parts.append(
                "[bg_vid]drawtext=fontfile='C\\:/Windows/Fonts/msyhbd.ttc':"
                f"text='{title_text}':"
                f"fontsize={BG_TITLE_FONT_SIZE}:fontcolor=white:borderw={BG_TITLE_OUTLINE}:bordercolor=black:"
                f"x=(w-text_w)/2:y=({fg_top_y}-text_h)/2:line_spacing=10[outv]"
            )
            out_label = "[outv]"

        filter_complex = ";".join(filter_parts)

        # 编码参数与现有合成参数保持一致
        preset, crf = ("medium", "22") if preview else ("fast", "18")

        cmd = [
            "ffmpeg", "-y",
            "-loop", "1", "-framerate", "25", "-i", str(DEFAULT_BG_PATH),
            "-i", str(input_path),
            "-filter_complex", filter_complex,
            "-map", out_label,
            "-map", "1:a?",  # 复制原视频音频流，无音频时不报错
            "-t", f"{video_dur:.3f}",  # 明确指定输出时长，避免ffmpeg因背景图loop一直运行不退出
            "-c:v", "libx264", "-preset", preset, "-crf", crf,
            "-pix_fmt", "yuv420p",
            "-c:a", "copy",
            "-movflags", "+faststart",
            str(output_path),
        ]

        print(f"  compositing background + title → {output_path.name}")
        subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, encoding="utf-8", errors="replace")
        # 验证输出文件有效（大于100KB），避免编码异常生成损坏文件
        if output_path.exists() and output_path.stat().st_size > 100 * 1024:
            return True
        print(f"  warning: background compositing produced invalid file, falling back")
        return False
    except subprocess.CalledProcessError as e:
        print(f"  warning: background compositing failed, falling back to original video: {e.stderr[-200:] if e.stderr else str(e)}")
        return False
    finally:
        pass


# -------- 主入口 ---------------------------------------------------------------


def main() -> None:
    """命令行入口：解析参数，执行完整渲染流水线。"""
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
        help="Skip audio loudness normalization. Default is on (-16 LUFS, -1.5 dBTP, LRA 11, 国内短视频平台标准).",
    )
    args = ap.parse_args()

    edl_path = args.edl.resolve()
    if not edl_path.exists():
        sys.exit(f"edl not found: {edl_path}")

    edl = json.loads(edl_path.read_text())
    edit_dir = edl_path.parent
    out_path = args.output.resolve()

    # 1. 逐片段提取（EDL grade为"auto"时每个范围自动分级）
    segment_paths = extract_all_segments(
        edl, edit_dir, preview=args.preview, draft=args.draft
    )

    # 2. 片段拼接 → 基础视频
    if args.draft:
        base_name = "base_draft.mp4"
    elif args.preview:
        base_name = "base_preview.mp4"
    else:
        base_name = "base.mp4"
    base_path = edit_dir / base_name
    concat_segments(segment_paths, base_path, edit_dir)

    # 3. 字幕处理：按需生成，解析最终路径
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

    # 4. 合成（叠加层 + 最后字幕）→ 中间文件（响度标准化前）
    overlays = edl.get("overlays") or []
    if args.no_loudnorm:
        # 跳过响度标准化时直接合成到最终输出
        build_final_composite(base_path, overlays, subs_path, out_path, edit_dir)
    else:
        # 先合成到临时文件，再执行响度标准化 → 最终输出
        tmp_composite = out_path.with_suffix(".prenorm.mp4")
        build_final_composite(base_path, overlays, subs_path, tmp_composite, edit_dir)
        print("loudness normalization → social-ready (-16 LUFS / -1.5 dBTP / LRA 11, 国内平台标准)")
        apply_loudnorm_two_pass(tmp_composite, out_path, preview=args.draft)
        tmp_composite.unlink(missing_ok=True)

    size_mb = out_path.stat().st_size / (1024 * 1024)
    print(f"\ndone: {out_path} ({size_mb:.1f} MB)")


def _probe_stream_duration(sources: dict, src_name: str) -> float:
    """探测源视频时长，单位秒，未知返回0.0。

    Args:
        sources: EDL中的sources字典
        src_name: 源名称
    Returns:
        视频时长，单位秒，探测失败返回0.0
    """
    src_path = resolve_path(sources[src_name], Path("."))
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", str(src_path)],
            check=True, capture_output=True, text=True,
            encoding="utf-8", errors="replace",
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
    background: bool = False,
    preview: bool = False,
    self_eval: bool = True,
    _self_eval_fn=None,
    on_progress=None,
) -> tuple[list[Path], list[str | None]]:
    """将EDL中的每个剪辑渲染为独立的clip_NNN.mp4文件。

    Args:
        edl: EDL字典对象
        edit_dir: 编辑工作目录
        out_dir: 输出目录，默认是edit_dir/clips
        subtitles: 是否烧录字幕，默认True
        background: 是否启用竖屏背景+顶部标题模式，默认False，启用后输出1080×1920 9:16竖屏尺寸，适配国内短视频平台
        preview: 是否为预览模式
        self_eval: 是否启用自评估质量检查
        _self_eval_fn: 自评估函数，用于质量检查和自动修复
        on_progress: 进度回调函数，接收参数(stage, percent, message)
    Returns:
        (最终输出文件路径列表, 质量检查标记列表)，每个标记为None表示无问题，否则为问题描述字符串

    实现说明：
    - 每个剪辑可包含1+个片段（不连续的范围会被拼接为一个剪辑）
    - 对每个剪辑构建子EDL，使用与render.main完全相同的经过验证的流水线：
      extract_all_segments（每个片段分级+30ms淡入淡出）
      → concat_segments（将该剪辑的片段拼接为一个基础视频）
      → build_master_srt（单剪辑SRT，seg_offset保证时间从0开始）
      → build_final_composite（最后烧录字幕）
      → apply_loudnorm_two_pass → clip_NNN.mp4 + clip_NNN.srt
    - 支持最多3次自动重试修复质量问题
    """
    def _p(pct: int, msg: str):
        """内部进度上报函数。"""
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
        # 兼容v1版本EDL：将每个ranges项转换为单片段剪辑
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
        """使用给定的边距/淡入淡出参数渲染单个剪辑。

        Args:
            clip_idx: 剪辑索引
            clip: 剪辑配置字典
            pad_before: 片段前预留边距，单位秒，默认50ms
            pad_after: 片段后预留边距，单位秒，默认80ms
            fade_duration: 音频淡入淡出时长，单位秒，默认30ms
            segment_offsets: 片段偏移量配置列表，用于质量修复时调整剪辑点
        Returns:
            (最终输出路径, 未加边距的片段列表, 质量检查标记)
        """
        src_name = clip["source"]
        segs = clip.get("segments") or []
        if not segs:
            raise ValueError(f"clip {clip_idx} has no segments")

        # 如果提供了片段偏移量则应用
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

        # 计算剪辑总时长
        src_path = resolve_path(sources[src_name], edit_dir)
        clip_start = float(padded_ranges[0]["start"])
        clip_end = float(padded_ranges[-1]["end"])
        clip_total_dur = clip_end - clip_start

        # 自动色彩分级：对整个剪辑统一分析，避免片段间分级不一致
        if sub_edl["grade"] == "auto":
            try:
                unified_filter, _ = auto_grade_for_clip(
                    src_path, start=clip_start, duration=clip_total_dur, verbose=False,
                )
                sub_edl["grade"] = unified_filter or "light"
            except Exception:
                sub_edl["grade"] = "light"

        # 始终生成独立SRT字幕文件，方便用户后期编辑、挂载使用
        srt_path = clips_dir / f"clip_{clip_idx:03d}.srt"
        transcript_path = None
        # 查找转录文件：优先在edit_dir/transcripts下找，兼容任意文件名（一个任务通常只有一个转录文件）
        for search_dir in (edit_dir / "transcripts", edit_dir.parent / "transcripts"):
            if search_dir.exists() and search_dir.is_dir():
                json_files = list(search_dir.glob("*.json"))
                if json_files:
                    transcript_path = json_files[0]
                    break

        # ────────── 单片段快速路径：一次编码完成截取+调色+淡入淡出+字幕，减少一次重编码 ──────────
        is_single_segment = len(padded_ranges) == 1
        seg_paths = []
        base = clips_dir / f"clip_{clip_idx:03d}_base.mp4"
        subbed = clips_dir / f"clip_{clip_idx:03d}_sub.mp4"
        seg_offsets = [0.0]

        if is_single_segment:
            # 单片段快速路径：跳过extract_all_segments单独编码，直接一次ffmpeg处理完成所有操作
            print(f"  [001] {src_name}  {padded_ranges[0]['start']:7.2f}-{padded_ranges[0]['end']:7.2f}  ({clip_total_dur:5.2f}s)  单片段快速渲染")

            # 先生成字幕文件（单片段偏移量固定0）
            if transcript_path and transcript_path.exists():
                try:
                    sub_edl["_transcript_path"] = str(transcript_path)
                    build_master_srt(sub_edl, edit_dir, srt_path, seg_offsets=seg_offsets)
                    print(f"  generated subtitles: {srt_path.name} ({srt_path.stat().st_size / 1024:.1f} KB)")
                except Exception as e:
                    import traceback
                    traceback.print_exc()
                    print(f"  warning: failed to generate subtitles: {e}")
                    srt_path = None
            else:
                print(f"  warning: transcript not found, skipping subtitles")
                srt_path = None

            # 构建滤镜链：缩放（仅向下缩放）+ HDR转换 + 调色 + 字幕 + 音频淡入淡出
            # 先探测原视频分辨率
            src_w, src_h = 1920, 1080
            portrait = False
            try:
                probe_out = subprocess.run(
                    ["ffprobe", "-v", "error", "-select_streams", "v:0",
                     "-show_entries", "stream=width,height",
                     "-of", "csv=p=0", str(src_path)],
                    capture_output=True, text=True, check=True, encoding="utf-8", errors="replace",
                )
                src_w, src_h = map(int, probe_out.stdout.strip().split(","))
                portrait = src_h > src_w
            except Exception:
                pass

            vf_parts = []
            # HDR转SDR
            if is_hdr_source(src_path):
                vf_parts.append(TONEMAP_CHAIN)
            # 分辨率缩放：仅向下缩放，不放大
            if not preview:
                if portrait:
                    target_h = 1920 if src_h > 1920 else src_h
                    if target_h != src_h:
                        vf_parts.append(f"scale=-2:{target_h}")
                else:
                    target_w = 1920 if src_w > 1920 else src_w
                    if target_w != src_w:
                        vf_parts.append(f"scale={target_w}:-2")
            else:
                # 预览模式统一缩放到1080p
                vf_parts.append("scale=-2:1080" if portrait else "scale=1920:-2")
            # 调色滤镜
            grade_filter = resolve_grade_filter(sub_edl["grade"])
            if grade_filter and grade_filter != "__AUTO__":
                vf_parts.append(grade_filter)
            # 字幕滤镜（需要烧录时添加）
            need_burn_subs = subtitles and srt_path and srt_path.exists()
            if need_burn_subs:
                subs_abs = str(srt_path.resolve()).replace("\\", "/").replace(":", r"\:").replace("'", r"\'").replace("[", r"\[").replace("]", r"\]")
                sub_style = get_subtitle_style(src_path)
                vf_parts.append(f"subtitles='{subs_abs}':charenc=UTF-8:force_style='{sub_style}'")

            # 音频淡入淡出
            fade_dur = max(0.01, min(0.1, fade_duration))
            fade_out_start = max(0.0, clip_total_dur - fade_dur)
            af = f"afade=t=in:st=0:d={fade_dur:.3f},afade=t=out:st={fade_out_start:.3f}:d={fade_dur:.3f}"

            # 编码参数
            if preview:
                preset, crf = "medium", "22"
            else:
                preset, crf = "fast", "20"

            # 决定输出文件：需要烧字幕就直接输出到subbed，否则输出到base
            output_for_loudnorm = subbed if need_burn_subs else base
            cmd = [
                "ffmpeg", "-y",
                "-ss", f"{padded_ranges[0]['start']:.3f}",
                "-i", str(src_path),
                "-t", f"{clip_total_dur:.3f}",
            ]
            if vf_parts:
                cmd += ["-vf", ",".join(vf_parts)]
            cmd += [
                "-af", af,
                "-c:v", "libx264", "-preset", preset, "-crf", crf,
                "-pix_fmt", "yuv420p",
                "-c:a", "aac", "-b:a", "192k", "-ar", "48000",
                "-movflags", "+faststart",
                str(output_for_loudnorm),
            ]
            subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
            sub_base = output_for_loudnorm
        else:
            # ────────── 多片段路径：原有逻辑，先提取各片段再拼接 ──────────
            # 提取片段，使用自定义淡入淡出时长
            seg_paths = extract_all_segments(sub_edl, edit_dir, preview=preview, draft=False, fade_duration=fade_duration)

            # 片段拼接，同时获取每个片段的真实起始偏移量（用于字幕精确对齐）
            seg_offsets = concat_segments(seg_paths, base, edit_dir, xfade_duration=max(0.05, fade_duration*2), acrossfade_duration=max(0.03, fade_duration*1.5))

            # 生成字幕
            if transcript_path and transcript_path.exists():
                try:
                    sub_edl["_transcript_path"] = str(transcript_path)
                    build_master_srt(sub_edl, edit_dir, srt_path, seg_offsets=seg_offsets)
                    print(f"  generated subtitles: {srt_path.name} ({srt_path.stat().st_size / 1024:.1f} KB)")
                except Exception as e:
                    import traceback
                    traceback.print_exc()
                    print(f"  warning: failed to generate subtitles: {e}")
                    srt_path = None
            else:
                print(f"  warning: transcript not found, skipping subtitles")
                srt_path = None

            # 烧录字幕：用户勾选时将字幕硬压到视频画面上
            sub_base = base
            if subtitles and srt_path and srt_path.exists():
                build_final_composite(base, [], srt_path, subbed, edit_dir, source_video=src_path)
                sub_base = subbed

        # 竖屏背景+标题合成（单片段/多片段路径汇合后统一处理）
        bg_tmp = None
        if background:
            clip_title = clip.get("title") or clip.get("reason") or ""
            bg_tmp = clips_dir / f"clip_{clip_idx:03d}_bg.mp4"
            ok = apply_background_title(sub_base, clip_title, bg_tmp, preview=preview)
            if ok:
                sub_base = bg_tmp

        # 响度标准化
        final = clips_dir / f"clip_{clip_idx:03d}.mp4"
        ok = apply_loudnorm_two_pass(sub_base, final, preview=preview)
        if not ok:
            run(["ffmpeg", "-y", "-i", str(sub_base), "-c", "copy", str(final)], quiet=True)

        # 清理临时文件
        for sp in seg_paths:
            sp.unlink(missing_ok=True)
        base.unlink(missing_ok=True)
        subbed.unlink(missing_ok=True)
        if bg_tmp is not None:
            bg_tmp.unlink(missing_ok=True)
        final.with_suffix(".prenorm.mp4").unlink(missing_ok=True)

        # 质量检查
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

        # 渲染并支持自动修复重试（仅 critical + fixable 的问题才重渲染）
        final = None
        qc_flag = None
        pad_before, pad_after, fade_duration = 0.10, 0.15, 0.03
        segment_offsets = None  # 切点偏移调整量，QC自动修复时使用
        max_retries = 3
        for qc_attempt in range(max_retries + 1):  # 初始渲染 + 最多3次重试
            try:
                final, unpadded, qc_flag = _render_single_clip(
                    i, clip,
                    pad_before=pad_before,
                    pad_after=pad_after,
                    fade_duration=fade_duration,
                    segment_offsets=segment_offsets,
                )
                if qc_flag is None:
                    break  # 无问题，完成

                # 解析 qc_flag JSON，判断严重级别和可修复性
                import json as _json
                try:
                    qc_data = _json.loads(qc_flag) if isinstance(qc_flag, str) else {}
                    qc_level = qc_data.get("level", "critical")
                    qc_fixable = qc_data.get("fixable", False)
                    qc_issues_text = "；".join(qc_data.get("issues", []))
                except Exception:
                    qc_level = "critical"
                    qc_fixable = False
                    qc_issues_text = str(qc_flag)

                # 🔴 严重 + 可修复 → 重渲染；其他情况 → 记录不重试
                if qc_level != "critical" or not qc_fixable:
                    level_label = {"critical": "🔴", "warning": "🟡", "info": "🟢"}.get(qc_level, "")
                    if not qc_fixable:
                        print(f"        [QC] {level_label} {qc_level} 问题（不可自动修复），已记录到 manifest：{qc_issues_text}")
                    else:
                        print(f"        [QC] {level_label} {qc_level} 以下问题，已记录到 manifest（不阻塞流程）：{qc_issues_text}")
                    break

                # 只有 critical + fixable 才走到这里
                if qc_attempt >= max_retries:
                    print(f"        [QC WARN] 🔴 critical 问题已重试{max_retries}次仍未修复：{qc_issues_text}")
                    break

                # 从QC函数获取调整建议
                print(f"        [QC] 🔴 critical+fixable 问题 (attempt {qc_attempt+1}/{max_retries})：{qc_issues_text}，尝试自动修复…")
                adjustments = _self_eval_fn(
                    None, unpadded, edit_dir, get_adjustments=True,
                    issues=qc_issues_text, pad_before=pad_before, pad_after=pad_after, fade_duration=fade_duration,
                ) if _self_eval_fn else None
                if not adjustments:
                    print(f"        [QC] 无法生成修复方案，放弃重试")
                    break
                pad_before = float(adjustments.get("pad_before", pad_before))
                pad_after = float(adjustments.get("pad_after", pad_after))
                fade_duration = float(adjustments.get("fade_duration", fade_duration))
                # 应用切点偏移调整
                if adjustments.get("segment_offsets"):
                    segment_offsets = adjustments["segment_offsets"]
            except Exception as e:
                print(f"        [QC] Render attempt {qc_attempt+1} failed: {e}")
                if qc_attempt >= max_retries:
                    raise
                continue

        if final is None:
            final = clips_dir / f"clip_{i:03d}.mp4"

        size_mb = final.stat().st_size / (1024 * 1024)
        pct = 5 + int(95 * i / n_total)
        _p(pct, f"  [{i}/{n_total}] → {final.name} ({size_mb:.1f} MB)")
        final_paths.append(final)
        qc_flags.append(qc_flag)

    # 渲染完成后自动清理临时分段目录
    import shutil
    for tmp_dir_name in ("clips_graded", "clips_preview", "clips_draft"):
        tmp_dir = clips_dir.parent / tmp_dir_name
        if tmp_dir.exists() and tmp_dir.is_dir():
            try:
                shutil.rmtree(tmp_dir)
            except Exception:
                pass  # 清理失败不影响主流程

    _p(100, f"  渲染完成，共 {len(final_paths)} 条")
    return final_paths, qc_flags


if __name__ == "__main__":
    main()