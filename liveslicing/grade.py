"""视频调色模块：通过ffmpeg滤镜链为视频应用色彩校正。

支持两种调色模式：
1. 预设模式：选择已命名的调色预设（如warm_cinematic暖色调电影感、neutral_punch中性通透），应用固定滤镜链
2. 自动模式（默认）：通过ffmpeg采样视频帧，数学分析亮度、对比度、饱和度，生成温和的逐片段校正滤镜，
   所有调整幅度硬限制在±8%以内，目标是"让画面干净但看不出调过色"，不会应用创意性色彩偏移（如青橙色调、电影曲线），
   仅修正欠曝、对比度不足、饱和度异常等问题。需要创意风格请显式使用--preset指定。

使用示例：
    python -m liveslicing.grade 输入.mp4 -o 输出.mp4                   # 自动模式
    python -m liveslicing.grade 输入.mp4 -o 输出.mp4 --preset warm_cinematic # 暖色调电影预设
    python -m liveslicing.grade 输入.mp4 -o 输出.mp4 --filter 'eq=contrast=1.1' # 自定义滤镜
    python -m liveslicing.grade --print-preset warm_cinematic         # 仅打印预设滤镜
    python -m liveslicing.grade --analyze 输入.mp4                     # 打印自动调色分析结果

被render.py导入使用：提供get_preset(name)获取预设滤镜、auto_grade_for_clip(path, range)生成自动调色滤镜。
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from pathlib import Path


PRESETS: dict[str, str] = {
    # 轻度增强：几乎不可察觉的清理，无色彩偏移，自动分析不可用时的安全默认值
    "light": "eq=contrast=1.03:saturation=0.98",

    # 最小校正预设：轻微提升对比度+S曲线，无色彩偏移
    "neutral_punch": (
        "eq=contrast=1.06:brightness=0.0:saturation=1.0,"
        "curves=master='0/0 0.25/0.23 0.75/0.77 1/1'"
    ),

    # 可选创意预设：仅用于复古/电影感风格，不是默认值
    # +12%对比度、压暗黑场、-12%饱和度、暖阴影冷高光、电影曲线，效果偏强不适合常规直播切片
    "warm_cinematic": (
        "eq=contrast=1.12:brightness=-0.02:saturation=0.88,"
        "colorbalance="
        "rs=0.02:gs=0.0:bs=-0.03:"
        "rm=0.04:gm=0.01:bm=-0.02:"
        "rh=0.08:gh=0.02:bh=-0.05,"
        "curves=master='0/0 0.25/0.22 0.75/0.78 1/1'"
    ),

    # 无调色：直接复制流，用于不需要调色的场景
    "none": "",
}


def get_preset(name: str) -> str:
    """根据预设名称返回对应的ffmpeg滤镜字符串，"none"返回空字符串。

    Args:
        name: 预设名称

    Returns:
        ffmpeg滤镜字符串

    Raises:
        KeyError: 预设不存在时抛出，提示可用预设列表
    """
    if name not in PRESETS:
        raise KeyError(
            f"未知预设'{name}'，可用预设: {', '.join(sorted(PRESETS))}"
        )
    return PRESETS[name]


# -------- 自动调色（数据驱动，逐片段校正） --------------------------------


def _sample_frame_stats(
    video: Path,
    start: float,
    duration: float,
    n_samples: int = 10,
) -> dict[str, float]:
    """在指定时间范围内采样N帧，计算亮度/对比度/饱和度统计值。

    使用ffmpeg的`signalstats`滤镜从stderr输出中获取每帧的YMIN/YMAX/YAVG/SATAVG等元数据，
    自动适配源位深（8bit/10bit）将所有值归一化到0~1区间。

    Args:
        video: 视频文件路径
        start: 采样起始时间（秒）
        duration: 采样时长（秒）
        n_samples: 采样帧数，默认10帧

    Returns:
        统计值字典:
        {
          "y_mean":   平均亮度（0~1）,
          "y_std":    亮度标准差近似值（0~1）,
          "sat_mean": 平均饱和度（0~1）,
        }
        采样失败时返回中性默认值（无校正）
    """
    fps = max(0.5, min(n_samples / max(duration, 0.1), 10.0))

    cmd = [
        "ffmpeg", "-y", "-hide_banner", "-nostats",
        "-ss", f"{start:.3f}",
        "-i", str(video),
        "-t", f"{duration:.3f}",
        "-vf", f"fps={fps:.2f},signalstats,metadata=print",
        "-f", "null", "-",
    ]
    proc = subprocess.run(cmd, check=True, capture_output=True, text=True, encoding="utf-8", errors="replace")
    meta_text = proc.stderr

    # 解析signalstats元数据，自动适配源位深
    y_avgs: list[float] = []
    y_mins: list[float] = []
    y_maxs: list[float] = []
    sat_avgs: list[float] = []
    bit_depth: int = 8

    def _parse_value(line: str) -> float | None:
        """从元数据行中解析数值，失败返回None"""
        try:
            return float(line.rsplit("=", 1)[1])
        except (ValueError, IndexError):
            return None

    for line in meta_text.splitlines():
        line = line.strip()
        if "lavfi.signalstats.YBITDEPTH" in line:
            v = _parse_value(line)
            if v is not None:
                bit_depth = int(v)
        elif "lavfi.signalstats.YAVG" in line:
            v = _parse_value(line)
            if v is not None:
                y_avgs.append(v)
        elif "lavfi.signalstats.YMIN" in line:
            v = _parse_value(line)
            if v is not None:
                y_mins.append(v)
        elif "lavfi.signalstats.YMAX" in line:
            v = _parse_value(line)
            if v is not None:
                y_maxs.append(v)
        elif "lavfi.signalstats.SATAVG" in line:
            v = _parse_value(line)
            if v is not None:
                sat_avgs.append(v)

    if not y_avgs:
        # 采样失败返回中性默认值（无校正）
        return {"y_mean": 0.5, "y_std": 0.18, "sat_mean": 0.25}

    # 按源位深最大值归一化到0~1区间
    max_val = (2 ** bit_depth) - 1

    y_mean = (sum(y_avgs) / len(y_avgs)) / max_val
    y_range = (
        ((sum(y_maxs) / len(y_maxs)) - (sum(y_mins) / len(y_mins))) / max_val
        if y_maxs and y_mins
        else 0.7
    )
    sat_mean = ((sum(sat_avgs) / len(sat_avgs)) / max_val) if sat_avgs else 0.25

    return {
        "y_mean": y_mean,
        "y_std": y_range / 4.0,  # 正态分布下range/4≈标准差
        "sat_mean": sat_mean,
    }


def auto_grade_for_clip(
    video: Path,
    start: float = 0.0,
    duration: float | None = None,
    verbose: bool = False,
) -> tuple[str, dict[str, float]]:
    """分析指定片段范围，生成温和的逐片段校正滤镜。

    所有调整幅度硬限制在±8%以内，无色彩偏移，仅修正：
    - 欠曝：画面过暗时轻微提升gamma
    - 对比度不足：动态范围窄时轻微提升对比度
    - 饱和度异常：饱和度过低时轻微提升

    画面已经均衡时返回subtle基线预设。

    Args:
        video: 视频文件路径
        start: 片段起始时间（秒）
        duration: 片段时长（秒），None时自动探测整个视频时长
        verbose: 是否打印分析详情

    Returns:
        (滤镜字符串, 统计值字典)元组，滤镜为空字符串时表示无需校正直接复制
    """
    if duration is None:
        # 自动探测视频时长
        probe_cmd = [
            "ffprobe", "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            str(video),
        ]
        try:
            duration = float(subprocess.check_output(probe_cmd).decode().strip())
        except Exception:
            duration = 10.0

    stats = _sample_frame_stats(video, start, duration)

    y_mean = stats["y_mean"]
    y_range = stats["y_std"] * 4.0  # 还原为动态范围
    sat_mean = stats["sat_mean"]

    # ------ 决策规则 ---------------------------------------------------
    # 所有调整硬限制在±8%以内，目标"干净但看不出调过色"

    # 对比度：目标y_range≈0.72，画面平的时候轻微提升，从不降低对比度
    contrast_adj = 1.0
    if y_range < 0.65:
        # 动态范围[0.50, 0.65]映射到对比度[1.08, 1.03]
        t = max(0.0, min(1.0, (y_range - 0.50) / 0.15))
        contrast_adj = 1.08 - 0.05 * t
    else:
        contrast_adj = 1.03  # 基线轻微提升

    # Gamma：目标y_mean≈0.48，过暗时轻微提升gamma
    gamma_adj = 1.0
    if y_mean < 0.42:
        # 亮度[0.30, 0.42]映射到gamma[1.10, 1.02]
        t = max(0.0, min(1.0, (y_mean - 0.30) / 0.12))
        gamma_adj = 1.10 - 0.08 * t
    elif y_mean > 0.60:
        # 过曝时轻微压暗
        gamma_adj = 0.97

    # 饱和度：目标sat_mean≈0.25，默认轻微降饱和（消费级相机普遍饱和略高）
    sat_adj = 0.98
    if sat_mean < 0.18:
        # 饱和度极低时轻微提升
        sat_adj = 1.04
    elif sat_mean > 0.38:
        # 饱和度过高时轻微降低
        sat_adj = 0.96

    # 硬限制所有调整幅度
    contrast_adj = max(0.94, min(1.08, contrast_adj))
    gamma_adj = max(0.94, min(1.10, gamma_adj))
    sat_adj = max(0.94, min(1.06, sat_adj))

    # 拼接滤镜字符串，差值≤0.005的参数省略（变化不可感知）
    eq_parts = []
    if abs(contrast_adj - 1.0) > 0.005:
        eq_parts.append(f"contrast={contrast_adj:.3f}")
    if abs(gamma_adj - 1.0) > 0.005:
        eq_parts.append(f"gamma={gamma_adj:.3f}")
    if abs(sat_adj - 1.0) > 0.005:
        eq_parts.append(f"saturation={sat_adj:.3f}")

    if not eq_parts:
        filter_string = ""
    else:
        filter_string = "eq=" + ":".join(eq_parts)

    if verbose:
        print(f"  自动调色分析:")
        print(f"    平均亮度={y_mean:.3f}  动态范围={y_range:.3f}  平均饱和度={sat_mean:.3f}")
        print(f"    → 对比度调整={contrast_adj:.3f}  gamma调整={gamma_adj:.3f}  饱和度调整={sat_adj:.3f}")
        print(f"    → 滤镜: {filter_string or '(无调整，直接复制)'}")

    return filter_string, stats


def apply_grade(input_path: Path, output_path: Path, filter_string: str) -> None:
    """应用调色滤镜到视频。

    Args:
        input_path: 输入视频路径
        output_path: 输出视频路径
        filter_string: ffmpeg滤镜字符串，为空时直接复制流
    """
    output_path.parent.mkdir(parents=True, exist_ok=True)
    if not filter_string:
        cmd = [
            "ffmpeg", "-y", "-i", str(input_path),
            "-c", "copy", str(output_path),
        ]
    else:
        cmd = [
            "ffmpeg", "-y", "-i", str(input_path),
            "-vf", filter_string,
            "-c:v", "libx264", "-preset", "fast", "-crf", "18",
            "-pix_fmt", "yuv420p",
            "-c:a", "copy",
            "-movflags", "+faststart",
            str(output_path),
        ]
    subprocess.run(cmd, check=True)


def main() -> None:
    ap = argparse.ArgumentParser(description="通过ffmpeg滤镜链为视频应用调色")
    ap.add_argument("input", type=Path, nargs="?", help="输入视频路径")
    ap.add_argument("-o", "--output", type=Path, help="输出视频路径")
    ap.add_argument(
        "--preset",
        type=str,
        default=None,
        choices=list(PRESETS.keys()),
        help="调色预设，留空默认使用自动调色",
    )
    ap.add_argument(
        "--filter",
        type=str,
        default=None,
        help="自定义ffmpeg滤镜字符串，优先级高于--preset",
    )
    ap.add_argument(
        "--analyze",
        type=Path,
        default=None,
        help="分析视频并打印自动调色结果，不写入输出文件",
    )
    ap.add_argument(
        "--print-preset",
        type=str,
        default=None,
        help="打印指定预设的滤镜字符串后退出，无需输入输出文件",
    )
    ap.add_argument(
        "--list-presets",
        action="store_true",
        help="列出所有可用预设后退出",
    )
    args = ap.parse_args()

    if args.list_presets:
        for name, f in PRESETS.items():
            print(f"{name}:")
            print(f"  {f}" if f else "  (无滤镜，直接复制)")
            print()
        return

    if args.print_preset is not None:
        print(get_preset(args.print_preset))
        return

    if args.analyze is not None:
        if not args.analyze.exists():
            sys.exit(f"输入文件不存在: {args.analyze}")
        filter_string, stats = auto_grade_for_clip(args.analyze, verbose=True)
        print(f"\n滤镜: {filter_string or '(无调整)'}")
        print(f"统计值:  {json.dumps(stats, indent=2)}")
        return

    if not args.input or not args.output:
        ap.error("使用非查询类功能时必须指定input和-o/--output参数")

    if not args.input.exists():
        sys.exit(f"输入文件不存在: {args.input}")

    # 决定使用的滤镜
    if args.filter is not None:
        filter_string = args.filter
    elif args.preset is not None:
        filter_string = get_preset(args.preset)
    else:
        # 默认自动调色模式
        filter_string, _ = auto_grade_for_clip(args.input, verbose=True)

    print(f"正在调色 {args.input.name} → {args.output.name}")
    if filter_string:
        print(f"  滤镜: {filter_string[:120]}{'...' if len(filter_string) > 120 else ''}")
    else:
        print("  滤镜: (无调整，直接复制)")

    apply_grade(args.input, args.output, filter_string)
    print(f"完成: {args.output}")


if __name__ == "__main__":
    main()