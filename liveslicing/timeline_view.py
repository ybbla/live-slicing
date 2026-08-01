"""视频时间线可视化模块：为指定时间范围生成胶片条+波形合成PNG图。

这是唯一的可视化排查工具：给定视频和[start, end]时间范围，通过ffmpeg均匀抽取N帧合成为水平胶片条，
下方渲染音频波形带，如有转录文件则叠加词标签、静音区间阴影标记。

用于切点校验、模糊停顿排查、QC质检等决策场景，属于按需调用工具，不要在全量扫描循环中调用。

使用示例：
    python -m liveslicing.timeline_view 视频.mp4 12.34 20.00
    python -m liveslicing.timeline_view 视频.mp4 12.34 20.00 -o out.png
    python -m liveslicing.timeline_view 视频.mp4 12.34 20.00 --n-frames 12
    python -m liveslicing.timeline_view 视频.mp4 12.34 20.00 --transcript 转录.json
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont


# -------- 帧提取 ---------------------------------------------------


def extract_frames(video: Path, start: float, end: float, n: int, dest_dir: Path) -> list[Path]:
    """在[start, end]时间范围内均匀抽取N帧，返回按时间顺序排列的帧图片路径。

    Args:
        video: 视频文件路径
        start: 起始时间（秒）
        end: 结束时间（秒）
        n: 抽取帧数
        dest_dir: 帧图片保存目录

    Returns:
        抽取成功的帧图片路径列表，损坏片段自动跳过不中断流程
    """
    import subprocess as _sp
    dest_dir.mkdir(parents=True, exist_ok=True)
    if n < 1:
        n = 1

    # 获取视频真实时长，避免抽帧超出视频结尾
    dur = 0.0
    try:
        r = _sp.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", str(video)],
            capture_output=True, text=True, timeout=10,
            encoding="utf-8", errors="replace",
        )
        dur = float(r.stdout.strip() or 0)
    except Exception:
        dur = end + 1.0  # 探测失败兜底：假设end为有效时间

    # 限制结束时间不超过视频时长-0.05秒安全余量
    effective_end = min(end, max(0.0, dur - 0.05))
    effective_start = max(0.0, min(start, effective_end))

    if n == 1:
        times = [(effective_start + effective_end) / 2.0]
    else:
        step = (effective_end - effective_start) / (n - 1) if n > 1 else 0
        times = [effective_start + i * step for i in range(n)]

    paths: list[Path] = []
    for i, t in enumerate(times):
        # 确保时间在有效范围内
        t = max(0.0, min(t, max(0.0, dur - 0.05)))
        out = dest_dir / f"f_{i:03d}.jpg"
        cmd = [
            "ffmpeg", "-y",
            "-ss", f"{t:.3f}",
            "-i", str(video),
            "-frames:v", "1",
            "-q:v", "4",
            "-vf", "scale=320:-2",
            str(out),
        ]
        try:
            _sp.run(cmd, check=True, stdout=_sp.DEVNULL, stderr=_sp.DEVNULL)
            paths.append(out)
        except Exception:
            # 帧提取失败（如损坏片段）直接跳过，不中断QC流程
            continue
    return paths


# -------- 音频包络（优先使用ffmpeg，避免librosa硬依赖） ------------


def compute_envelope(video: Path, start: float, end: float, samples: int = 2000) -> np.ndarray:
    """提取指定范围音频，返回长度为samples的RMS音量包络。

    通过ffmpeg导出单声道16kHz PCM临时wav，手动解析计算加窗RMS，源文件无音频时返回全0数组。

    Args:
        video: 视频文件路径
        start: 起始时间（秒）
        end: 结束时间（秒）
        samples: 输出包络采样点数，默认2000

    Returns:
        归一化到[0,1]区间的RMS包络numpy数组
    """
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
        wav = Path(f.name)
    try:
        cmd = [
            "ffmpeg", "-y",
            "-ss", f"{start:.3f}",
            "-i", str(video),
            "-t", f"{(end - start):.3f}",
            "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le",
            str(wav),
        ]
        r = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if r.returncode != 0 or not wav.exists() or wav.stat().st_size == 0:
            return np.zeros(samples)

        # 手动解析WAV文件，避免librosa作为硬依赖
        import wave
        with wave.open(str(wav), "rb") as w:
            frames = w.readframes(w.getnframes())
        pcm = np.frombuffer(frames, dtype=np.int16).astype(np.float32) / 32768.0
        if pcm.size == 0:
            return np.zeros(samples)

        # 加窗计算RMS得到指定长度的包络
        n = pcm.size
        window = max(1, n // samples)
        usable = (n // window) * window
        reshaped = pcm[:usable].reshape(-1, window)
        env = np.sqrt(np.mean(reshaped ** 2, axis=1))
        if env.size < samples:
            env = np.pad(env, (0, samples - env.size))
        elif env.size > samples:
            env = env[:samples]
        # 归一化到[0, 1]
        if env.max() > 0:
            env = env / env.max()
        return env
    finally:
        wav.unlink(missing_ok=True)


# -------- 转录词叠加 ------------------------------------------


def words_in_range(transcript_path: Path, start: float, end: float) -> list[dict]:
    """获取转录文件中落在指定时间范围内的所有词条目。

    Args:
        transcript_path: 转录JSON文件路径
        start: 起始时间（秒）
        end: 结束时间（秒）

    Returns:
        范围内的词条目列表
    """
    if not transcript_path.exists():
        return []
    data = json.loads(transcript_path.read_text())
    out: list[dict] = []
    for w in data.get("words", []):
        t = w.get("type", "word")
        ws = w.get("start")
        we = w.get("end")
        if ws is None or we is None:
            continue
        if we <= start or ws >= end:
            continue
        out.append(w)
    return out


def find_silences(words: list[dict], start: float, end: float, threshold: float = 0.4) -> list[tuple[float, float]]:
    """查找[start, end]范围内时长≥threshold秒的静音区间。

    Args:
        words: 词条目列表
        start: 范围起始时间（秒）
        end: 范围结束时间（秒）
        threshold: 静音判定阈值（秒），默认0.4秒

    Returns:
        静音区间列表，每个元素为(开始秒, 结束秒)
    """
    gaps: list[tuple[float, float]] = []
    prev_end = start
    for w in words:
        if w.get("type") == "spacing":
            continue
        ws = max(start, w.get("start", start))
        if ws - prev_end >= threshold:
            gaps.append((prev_end, ws))
        prev_end = max(prev_end, w.get("end", ws))
    if end - prev_end >= threshold:
        gaps.append((prev_end, end))
    return gaps


# -------- 字体加载 -------------------------------------------------------


# 跨平台等宽字体候选列表
FONT_CANDIDATES = [
    "C:/Windows/Fonts/msyh.ttc",  # 微软雅黑（Windows中文字体）
    "C:/Windows/Fonts/simhei.ttf", # 黑体
    "/System/Library/Fonts/Menlo.ttc",
    "/System/Library/Fonts/Helvetica.ttc",
    "/System/Library/Fonts/SFNSMono.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationMono-Regular.ttf",
]


def load_font(size: int) -> ImageFont.ImageFont:
    """加载指定大小的字体，优先使用系统中文字体，失败回退到PIL默认字体。

    Args:
        size: 字体字号

    Returns:
        PIL字体对象
    """
    for fp in FONT_CANDIDATES:
        if Path(fp).exists():
            try:
                return ImageFont.truetype(fp, size)
            except Exception:
                continue
    return ImageFont.load_default()


# -------- 合成渲染 ----------------------------------------------------------

# 配色方案
BG = (18, 18, 22)       # 背景色
FG = (235, 235, 235)    # 前景文字色
DIM = (110, 110, 120)   # 次要文字/刻度色
ACCENT = (255, 140, 60) # 强调色
SILENCE = (50, 80, 120, 120)  # 静音阴影色（半透明淡蓝）
WAVE = (140, 180, 255)  # 波形色


def render_timeline(
    video: Path,
    start: float,
    end: float,
    out_path: Path,
    n_frames: int,
    transcript: Path | None,
) -> None:
    """渲染指定时间范围的胶片条+波形合成PNG。

    Args:
        video: 源视频路径
        start: 起始时间（秒）
        end: 结束时间（秒）
        out_path: 输出PNG路径
        n_frames: 胶片条帧数
        transcript: 转录JSON路径，None时不叠加词标签和静音标记
    """
    # 抽帧
    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)
        print(f"从{start:.2f}秒到{end:.2f}秒抽取{n_frames}帧")
        frame_paths = extract_frames(video, start, end, n_frames, tmp_dir)

        # 布局参数
        canvas_width = 1920
        frame_h = 180
        filmstrip_y = 50
        filmstrip_h = frame_h
        wave_y = filmstrip_y + filmstrip_h + 20
        wave_h = 220
        label_y = wave_y + wave_h + 10
        canvas_height = label_y + 60

        # 加载并统一帧高度，计算总宽度
        imgs: list[Image.Image] = []
        for fp in frame_paths:
            img = Image.open(fp).convert("RGB")
            aspect = img.width / img.height
            new_w = int(frame_h * aspect)
            imgs.append(img.resize((new_w, frame_h), Image.LANCZOS))

        total_frame_w = sum(img.width for img in imgs) + (len(imgs) - 1) * 4
        content_w = max(1400, total_frame_w)
        canvas_width = max(canvas_width, content_w + 100)

        canvas = Image.new("RGB", (canvas_width, canvas_height), BG)
        draw = ImageDraw.Draw(canvas, "RGBA")

        header_font = load_font(22)
        label_font = load_font(14)
        small_font = load_font(12)

        # 头部：时间范围信息
        draw.text(
            (50, 12),
            f"{video.name}   {start:.2f}秒 → {end:.2f}秒   ({(end - start):.2f}秒, {n_frames}帧)",
            fill=FG,
            font=header_font,
        )

        # 胶片条绘制
        x = 50
        strip_width = canvas_width - 100
        if total_frame_w <= strip_width:
            cursor = 50
            for img in imgs:
                canvas.paste(img, (cursor, filmstrip_y))
                cursor += img.width + 4
            draw_width = cursor - 50
        else:
            # 帧总宽度超过画布时等比缩放
            scale = strip_width / total_frame_w
            new_h = int(frame_h * scale)
            cursor = 50
            for img in imgs:
                new_w = int(img.width * scale)
                scaled = img.resize((new_w, new_h), Image.LANCZOS)
                canvas.paste(scaled, (cursor, filmstrip_y + (filmstrip_h - new_h) // 2))
                cursor += new_w + max(2, int(4 * scale))
            draw_width = cursor - 50

        strip_x0 = 50
        strip_x1 = 50 + draw_width
        strip_span = strip_x1 - strip_x0

        def time_to_x(t: float) -> int:
            """将秒级时间转换为画布X坐标"""
            frac = (t - start) / max(1e-6, (end - start))
            return int(strip_x0 + frac * strip_span)

        # 波形背景
        draw.rectangle((strip_x0, wave_y, strip_x1, wave_y + wave_h), fill=(28, 28, 34))

        # 静音区间阴影（波形下方）
        words = words_in_range(transcript, start, end) if transcript else []
        silences = find_silences(words, start, end, threshold=0.4) if words else []
        for a, b in silences:
            xa = time_to_x(a)
            xb = time_to_x(b)
            draw.rectangle((xa, wave_y, xb, wave_y + wave_h), fill=SILENCE)

        # 绘制波形包络
        env = compute_envelope(video, start, end, samples=max(strip_span, 200))
        mid_y = wave_y + wave_h // 2
        max_amp = wave_h // 2 - 8
        points_top: list[tuple[int, int]] = []
        points_bot: list[tuple[int, int]] = []
        for i, v in enumerate(env):
            xi = strip_x0 + int(i * strip_span / max(1, len(env) - 1))
            a = int(v * max_amp)
            points_top.append((xi, mid_y - a))
            points_bot.append((xi, mid_y + a))
        if points_top:
            draw.line(points_top, fill=WAVE, width=1, joint="curve")
            draw.line(points_bot, fill=WAVE, width=1, joint="curve")
            # 填充波形中间区域
            poly = points_top + list(reversed(points_bot))
            draw.polygon(poly, fill=(*WAVE, 60))

        # 波形上方叠加词标签（仅显示时长≥50ms的词，避免过密）
        last_label_x = -9999
        for w in words:
            if w.get("type") != "word":
                continue
            ws = w.get("start")
            we = w.get("end")
            text = (w.get("text") or "").strip()
            if not text or ws is None or we is None:
                continue
            if (we - ws) < 0.05:
                continue
            cx = (time_to_x(ws) + time_to_x(we)) // 2
            if cx - last_label_x < 28:
                continue
            # 波形上的小刻度
            draw.line((cx, wave_y - 4, cx, wave_y), fill=DIM, width=1)
            # 词文本
            draw.text((cx + 2, wave_y - 18), text, fill=FG, font=small_font)
            last_label_x = cx

        # 波形下方时间刻度
        ruler_y = wave_y + wave_h + 2
        n_ticks = 6
        for i in range(n_ticks + 1):
            frac = i / n_ticks
            t = start + frac * (end - start)
            xi = strip_x0 + int(frac * strip_span)
            draw.line((xi, ruler_y, xi, ruler_y + 6), fill=DIM, width=1)
            draw.text((xi - 20, ruler_y + 8), f"{t:.2f}s", fill=DIM, font=label_font)

        # 静音图例
        if silences:
            txt = f"阴影区域 = ≥400ms静音（共{len(silences)}个间隔）"
            draw.text((strip_x0, label_y + 30), txt, fill=DIM, font=label_font)

        out_path.parent.mkdir(parents=True, exist_ok=True)
        canvas.save(out_path, "PNG", optimize=True)
        print(f"已保存: {out_path} （{out_path.stat().st_size // 1024} KB）")


def main() -> None:
    ap = argparse.ArgumentParser(description="为视频指定范围生成胶片条+波形合成图")
    ap.add_argument("video", type=Path, nargs="?", help="源视频路径")
    ap.add_argument("start", type=float, nargs="?", help="起始时间（秒）")
    ap.add_argument("end", type=float, nargs="?", help="结束时间（秒）")
    ap.add_argument("-o", "--output", type=Path, default=None, help="输出PNG路径")
    ap.add_argument("--n-frames", type=int, default=10, help="胶片条帧数（默认10）")
    ap.add_argument(
        "--transcript",
        type=Path,
        default=None,
        help="转录JSON路径，用于叠加词标签和静音阴影，留空自动查找<视频目录>/edit/transcripts/<视频名>.json",
    )
    ap.add_argument(
        "--edl",
        type=Path,
        default=None,
        help="（暂未实现）从EDL渲染全项目时间线",
    )
    args = ap.parse_args()

    if args.edl:
        sys.exit("--edl模式暂未实现，请使用区间模式")

    if not args.video or args.start is None or args.end is None:
        ap.error("必须指定video、start、end参数")

    video = args.video.resolve()
    if not video.exists():
        sys.exit(f"视频不存在: {video}")

    if args.end <= args.start:
        sys.exit("结束时间必须大于起始时间")

    # 未指定转录路径时自动查找
    transcript = args.transcript
    if transcript is None:
        auto = video.parent / "edit" / "transcripts" / f"{video.stem}.json"
        if auto.exists():
            transcript = auto

    out_path = args.output
    if out_path is None:
        out_dir = video.parent / "edit" / "verify"
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = out_dir / f"{video.stem}_{args.start:.2f}-{args.end:.2f}.png"

    render_timeline(
        video=video,
        start=args.start,
        end=args.end,
        out_path=out_path,
        n_frames=args.n_frames,
        transcript=transcript,
    )


if __name__ == "__main__":
    main()