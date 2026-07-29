"""Select highlight clips from a packed transcript via Doubao (火山方舟 Ark).

读 <edit>/takes_packed.md，调豆包 LLM 选出高吸引力片段，吸附到短语边界
（保证不句中切断），输出多片段 EDL <edit>/clips/edl_multi.json 给 render_clips 消费。

Ark 是 OpenAI 兼容接口；ARK_MODEL 留空时首运行自动调 models.list() 探测。

支持 auto count 模式（count=None/0）：豆包根据内容密度自定条数。

用法（一般由 cli.py 调用，也可直接跑）：
    python -m liveslicing.pick_clips <video> --edit-dir ./edit --count 6
"""

from __future__ import annotations

import argparse
import base64
import json
import re
import subprocess
import sys
from pathlib import Path

from openai import OpenAI

from liveslicing.config import ark_config


# ────────────────── 模型 ──────────────────

VISION_MODEL = "doubao-seed-1-6-vision-250815"


def discover_model(client: OpenAI) -> str:
    """ARK_MODEL 留空时，列模型挑一个能用的（优先 doubao-pro / 1.5 / 256k 等大上下文）。"""
    print("ARK_MODEL 未设置，自动探测可用模型…")
    try:
        models = client.models.list()
    except Exception as e:
        sys.exit(f"无法列出 Ark 模型：{e!r}\n请在 .env 手动设置 ARK_MODEL")
    ids = []
    for m in models.data:
        mid = getattr(m, "id", None) or (m.get("id") if isinstance(m, dict) else None)
        if mid:
            ids.append(mid)
    if not ids:
        sys.exit("Ark 未返回任何模型，请在 .env 手动设置 ARK_MODEL")
    def score(mid: str) -> int:
        s = mid.lower()
        pref = 0
        if "doubao" in s:
            pref += 10
        for k in ("1.5", "128k", "256k", "32k", "pro", "vision", "thinking"):
            if k in s:
                pref += 3
        return pref
    ids.sort(key=score, reverse=True)
    chosen = ids[0]
    print(f"  发现 {len(ids)} 个模型，选用：{chosen}")
    print(f"  （若不合适，在 .env 设 ARK_MODEL=<id> 覆盖）")
    return chosen


# ────────────────── 解析 packed.md 的短语边界 ──────────────────

_PHRASE_RE = re.compile(r"\[(\d+\.\d{2})-(\d+\.\d{2})\]\s*(?:S\d+\s*)?(.*)")


def parse_phrases(packed_md: str) -> list[dict]:
    """从 takes_packed.md 解析每条短语：{start, end, text}。"""
    phrases = []
    for line in packed_md.splitlines():
        m = _PHRASE_RE.search(line)
        if not m:
            continue
        try:
            s = float(m.group(1))
            e = float(m.group(2))
        except ValueError:
            continue
        text = m.group(3).strip()
        if text:
            phrases.append({"start": s, "end": e, "text": text})
    return phrases


def snap_to_phrases(start: float, end: float, phrases: list[dict]) -> tuple[float, float] | None:
    """把候选 [start,end] 吸附到短语边界，保证不句中切断。"""
    snap_start = None
    for p in phrases:
        if p["start"] >= start - 0.5:
            snap_start = p["start"]
            break
    snap_end = None
    for p in reversed(phrases):
        if p["end"] <= end + 0.5:
            snap_end = p["end"]
            break
    if snap_start is None or snap_end is None or snap_end <= snap_start:
        return None
    return snap_start, snap_end


def _probe_video_duration(video: Path) -> float:
    """Get source video duration via ffprobe (for auto-count prompt)."""
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", str(video)],
            capture_output=True, text=True, check=True,
        )
        return float(out.stdout.strip() or 0.0)
    except Exception:
        return 0.0


def _fmt_dur(seconds: float) -> str:
    if seconds <= 0:
        return "未知"
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    if h > 0:
        return f"{h}小时{m}分"
    return f"{m}分钟"


# ────────────────── 调豆包选段 ──────────────────

SYSTEM_PROMPT_FIXED = """\
你是直播切片编辑。给定带时间戳的台词文本（短语级，每行格式 [start-end] 台词），
你要从中挑出 N 条对观众最有吸引力、信息密度高的片段，用于发布短视频。

选段标准（按重要性）：
1. **开头必须是钩子**：每条切片前3秒必须能抓住观众，开头必须是金句/冲突点/反常识观点/情绪爆点，**禁止以铺垫、承接、无意义发语词开头**（如"啊"、"那个"、"就是"、"然后呢"、"对吧"、"其实吧"等口癖和无信息量发语词开头），若候选段开头有这类词，自动往后吸附到第一个有实际信息量的短语边界作为segment起点
2. 金句 / 结论性观点 / 干货总结
3. 冲突 / 反转 / 悬念 / 情绪高点
4. 内容完整可独立成段，不要选半截话

切点工艺（提升成片质量）：
- 文本里相邻短语之间的时间间隔代表静音。优先把切点落在间隔 ≥400ms 的静音处（最干净）；
  150-400ms 的间隔可用；避免在 <150ms 的间隔切（很可能切在句中）。
- 文本里出现的 (laughs)/(applause)/(sighs) 等括号标记是音频事件，代表情绪高点。
  优先保留这些片段，并把切片末尾往后延一点以包含反应（笑声、掌声本身就是 beat）。
- 多段拼接时，优先选同说话人、且段间有静音间隔处切，拼接更自然。

一条切片可以由 1 个或多个 segment 拼成：
- 当一个完整论点/故事在原视频里是连续的一段，用 1 个 segment。
- 当一个完整论点分散在原视频几处不连续的地方（中间夹着无关内容），
  把这几段作为同一条切片的多个 segment 拼在一起，使成片逻辑连贯。

硬约束：
- 每个 segment 的 start/end 必须落在文本里出现的某个 [start-end] 边界上，不得自己造时间戳
- **第一条segment的开头必须是有信息量的内容，不能以无意义发语词/口癖/铺垫句开头**，确保观众点开前3秒就被抓住
- 同一条切片的多个 segment 不得互相重叠，且按时间顺序排列
- 每条切片的总时长（各 segment 时长之和）在 [MIN_DUR, MAX_DUR] 秒之间
- 不同切片之间不得重叠
- 选够 N 条（若素材确实不足，可少选，但不要凑数）

只输出 JSON，格式：
{"clips":[{"segments":[{"start":12.34,"end":20.00},{"start":35.50,"end":48.00}],
          "title":"一句话标题","reason":"为什么选这条（可说明为何拼接）"}]}
不要输出任何其它内容、不要 markdown 围栏。"""


SYSTEM_PROMPT_AUTO = """\
你是直播切片编辑。给定带时间戳的台词文本（短语级，每行格式 [start-end] 台词），
你要从中挑出若干条对观众最有吸引力、信息密度高的高光片段，用于发布短视频。

选段标准（按重要性）：
1. **开头必须是钩子**：每条切片前3秒必须能抓住观众，开头必须是金句/冲突点/反常识观点/情绪爆点，**禁止以铺垫、承接、无意义发语词开头**（如"啊"、"那个"、"就是"、"然后呢"、"对吧"、"其实吧"等口癖和无信息量发语词开头），若候选段开头有这类词，自动往后吸附到第一个有实际信息量的短语边界作为segment起点
2. 金句 / 结论性观点 / 干货总结
3. 冲突 / 反转 / 悬念 / 情绪高点
4. 内容完整可独立成段，不要选半截话

参考密度指引：
- 视频总时长已经告诉你，请据此判断合理条数。一般每小时直播约 5-8 条，金句密集/干货多可多切，内容平淡可少切。
- 总数不少于 3 条、不多于 MAX_CLIPS 条（超过 2 小时的长视频可放宽到 16 条）。
- 不要为凑数切水货片段；素材不足就少切，但每条都必须是真正的高光。
- 不要把同一论点拆成多条。

切点工艺（提升成片质量）：
- 文本里相邻短语之间的时间间隔代表静音。优先把切点落在间隔 ≥400ms 的静音处（最干净）；
  150-400ms 的间隔可用；避免在 <150ms 的间隔切（很可能切在句中）。
- 文本里出现的 (laughs)/(applause)/(sighs) 等括号标记是音频事件，代表情绪高点。
  优先保留这些片段，并把切片末尾往后延一点以包含反应（笑声、掌声本身就是 beat）。
- 多段拼接时，优先选同说话人、且段间有静音间隔处切，拼接更自然。

一条切片可以由 1 个或多个 segment 拼成：
- 当一个完整论点/故事在原视频里是连续的一段，用 1 个 segment。
- 当一个完整论点分散在原视频几处不连续的地方（中间夹着无关内容），
  把这几段作为同一条切片的多个 segment 拼在一起，使成片逻辑连贯。

硬约束：
- 每个 segment 的 start/end 必须落在文本里出现的某个 [start-end] 边界上，不得自己造时间戳
- **第一条segment的开头必须是有信息量的内容，不能以无意义发语词/口癖/铺垫句开头**
- 同一条切片的多个 segment 不得互相重叠，且按时间顺序排列
- 每条切片的总时长（各 segment 时长之和）在 [MIN_DUR, MAX_DUR] 秒之间（30秒-5分钟）
- 不同切片之间不得重叠

只输出 JSON，格式：
{"clips":[{"segments":[{"start":12.34,"end":20.00},{"start":35.50,"end":48.00}],
          "title":"一句话标题","reason":"为什么选这条（可说明为何拼接）"}]}
不要输出任何其它内容、不要 markdown 围栏。"""


def build_user_prompt_fixed(packed_md: str, count: int, min_dur: float, max_dur: float) -> str:
    return (
        f"请选出 {count} 条高吸引力切片。每条切片总时长 {min_dur:.0f}-{max_dur:.0f} 秒。\n\n"
        f"以下是直播台词（时间戳为秒）：\n\n{packed_md}"
    )


def build_user_prompt_auto(packed_md: str, video_dur_s: float, min_dur: float, max_dur: float) -> str:
    dur_hint = _fmt_dur(video_dur_s)
    max_clips = 16 if video_dur_s >= 7200 else 12
    return (
        f"视频总时长约 {dur_hint}。请自主判断合理条数（参考：每小时 5-8 条，"
        f"不少于 3 条，不多于 {max_clips} 条），每条切片总时长 {min_dur:.0f}-{max_dur:.0f} 秒。\n\n"
        f"以下是直播台词（时间戳为秒）：\n\n{packed_md}"
    )


def safe_json_loads(text: str) -> dict:
    """豆包可能偶尔带围栏或多余文字，容错解析。"""
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\n?", "", text)
        text = re.sub(r"\n?```$", "", text)
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if m:
        text = m.group(0)
    return json.loads(text)


def call_doubao_fixed(
    client: OpenAI,
    model: str,
    packed_md: str,
    count: int,
    min_dur: float,
    max_dur: float,
) -> list[dict]:
    """固定条数模式。返回 clips 列表。"""
    resp = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT_FIXED},
            {"role": "user", "content": build_user_prompt_fixed(packed_md, count, min_dur, max_dur)},
        ],
        response_format={"type": "json_object"},
        temperature=0.4,
    )
    content = resp.choices[0].message.content or ""
    try:
        data = safe_json_loads(content)
    except json.JSONDecodeError:
        resp = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT_FIXED + "\n务必只输出纯 JSON。"},
                {"role": "user", "content": build_user_prompt_fixed(packed_md, count, min_dur, max_dur)},
                {"role": "assistant", "content": content},
                {"role": "user", "content": "上面不是合法 JSON，请只输出 JSON 对象。"},
            ],
            temperature=0.2,
        )
        data = safe_json_loads(resp.choices[0].message.content or "")
    return data.get("clips", [])


def call_doubao_auto(
    client: OpenAI,
    model: str,
    packed_md: str,
    video_dur_s: float,
    min_dur: float,
    max_dur: float,
) -> list[dict]:
    """Auto count 模式：豆包根据内容密度自定条数。"""
    max_clips = 16 if video_dur_s >= 7200 else 12
    sys_prompt = SYSTEM_PROMPT_AUTO.replace("MAX_CLIPS", str(max_clips))
    resp = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": sys_prompt},
            {"role": "user", "content": build_user_prompt_auto(packed_md, video_dur_s, min_dur, max_dur)},
        ],
        response_format={"type": "json_object"},
        temperature=0.4,
    )
    content = resp.choices[0].message.content or ""
    try:
        data = safe_json_loads(content)
    except json.JSONDecodeError:
        resp = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": sys_prompt + "\n务必只输出纯 JSON。"},
                {"role": "user", "content": build_user_prompt_auto(packed_md, video_dur_s, min_dur, max_dur)},
                {"role": "assistant", "content": content},
                {"role": "user", "content": "上面不是合法 JSON，请只输出 JSON 对象。"},
            ],
            temperature=0.2,
        )
        data = safe_json_loads(resp.choices[0].message.content or "")
    return data.get("clips", [])


# ────────────────── 自评（vision 切点质检） ──────────────────

EVAL_PROMPT_CUT = (
    "这是一段已渲染视频在某个切点附近(±1.5秒)的胶片条+波形图合成。"
    "请判断切点处是否存在问题："
    "1) 画面跳变/闪烁/突兀切换；"
    "2) 波形出现尖峰(可能是爆音,30ms淡变未消住)；"
    "3) 字幕被遮挡或错位。"
    "只输出JSON: {\"ok\": true/false, \"issues\": [\"问题描述\"]}，无问题则 issues 为空数组。"
)

EVAL_PROMPT_SAMPLE = (
    "这是一段已渲染视频的片段胶片条+波形图合成。"
    "请判断是否存在质量问题："
    "1) 开头/结尾黑场、花屏、画面异常；"
    "2) 波形出现尖峰(爆音)或完全静音(音频丢失)；"
    "3) 字幕被遮挡、错位或不可读；"
    "4) 明显调色异常(过曝/过暗/偏色严重)。"
    "如果有问题，同时给出修复建议，调整切点前后的padding和音频淡入淡出时长："
    "- pad_before: 切点前缓冲时间(秒)，默认0.05"
    "- pad_after: 切点后缓冲时间(秒)，默认0.08"
    "- fade_duration: 音频淡入淡出时长(秒)，默认0.03"
    "- 切点偏移：对问题切点的起止时间做±0.2s以内的微调，避开跳变帧/爆音点"
    "只输出JSON: {\"ok\": true/false, \"issues\": [\"问题描述\"], \"adjustments\": {\"pad_before\": float, \"pad_after\": float, \"fade_duration\": float, \"segment_offsets\": [ {\"index\": int, \"start_offset\": float, \"end_offset\": float} ] }}，无问题则 adjustments 为null。"
    "注意：pad_before/pad_after最大不要超过0.2，fade_duration最大不要超过0.08，偏移量在-0.2到+0.2之间。"
)

QC_FIX_PROMPT = (
    "你是视频剪辑QC专家，以下是刚渲染的切片发现的质量问题：{issues}"
    "当前参数：pad_before={pad_before}s, pad_after={pad_after}s, fade_duration={fade_duration}s，共{seg_count}个片段。"
    "请给出具体的参数调整方案，用于重新渲染修复问题："
    "- 画面跳变/闪烁：增大切点前后padding，或微调切点位置避开跳变帧"
    "- 音频爆音/啵声：增大fade_duration和交叉淡入淡出时长，增大切点padding"
    "- 黑场/花屏：偏移切点位置，跳过异常帧"
    "字幕遮挡/调色异常这类难以自动修复的问题，ok设为false即可，不需要调整。"
    "只输出JSON: {\"ok\": true/false, \"adjustments\": {\"pad_before\": float, \"pad_after\": float, \"fade_duration\": float, \"segment_offsets\": [{\"index\": int, \"start_offset\": float, \"end_offset\": float}]}}"
    "如果认为无法自动修复，返回ok: false。"
)


def self_eval_clip(
    clip_path, segments: list[dict], edit_dir, preview: bool = False,
    get_adjustments: bool = False, issues: str = "",
    pad_before: float = 0.05, pad_after: float = 0.08, fade_duration: float = 0.03,
) -> str | dict | None:
    """对渲染好的 clip 做 vision 质检，或给出QC修复参数建议。
    - 正常模式：返回问题描述字符串或 None
    - get_adjustments模式：返回参数字典 {"pad_before": float, ...} 用于重渲染
    """
    # If get_adjustments is True, call LLM to get fix suggestions
    if get_adjustments:
        api_key, base_url, model = ark_config()
        client = OpenAI(api_key=api_key, base_url=base_url, timeout=60.0)
        if not model:
            model = "doubao-seed-2-0-mini-260428"
        try:
            seg_count = len(segments)
            prompt = QC_FIX_PROMPT.format(
                issues=issues, pad_before=pad_before, pad_after=pad_after,
                fade_duration=fade_duration, seg_count=seg_count,
            )
            resp = client.chat.completions.create(
                model=model,
                messages=[{"role": "user", "content": prompt}],
                response_format={"type": "json_object"},
                temperature=0.1, max_tokens=300,
            )
            import json as json_mod
            data = json_mod.loads(resp.choices[0].message.content or "{}")
            if not data.get("ok", True):
                return None  # Can't auto-fix
            adj = data.get("adjustments") or {}
            # Validate and clamp parameters
            result = {
                "pad_before": max(0.02, min(0.2, float(adj.get("pad_before", pad_before)))),
                "pad_after": max(0.02, min(0.2, float(adj.get("pad_after", pad_after)))),
                "fade_duration": max(0.01, min(0.1, float(adj.get("fade_duration", fade_duration)))),
            }
            return result
        except Exception as e:
            print(f"        生成QC修复建议失败: {e!r}")
            return None

    if not segments or clip_path is None:
        return None
    try:
        from liveslicing import timeline_view as tv
    except Exception:
        return None
    api_key, base_url, _ = ark_config()
    client = OpenAI(api_key=api_key, base_url=base_url, timeout=600.0)

    clip_dur = _probe_clip_duration(clip_path)
    verify_dir = Path(clip_path).parent / "verify"
    verify_dir.mkdir(parents=True, exist_ok=True)
    issues: list[str] = []

    check_windows: list[tuple[float, float, str, str]] = []
    if len(segments) > 1:
        seg_durs = [float(s["end"]) - float(s["start"]) for s in segments]
        acc = 0.0
        for d in seg_durs[:-1]:
            acc += d
            cp = acc
            win_s = max(0.0, cp - 1.5)
            win_e = min(clip_dur, cp + 1.5) if clip_dur > 0 else cp + 1.5
            check_windows.append((win_s, win_e, f"cut{len(check_windows)}", EVAL_PROMPT_CUT))

    if clip_dur >= 1.0:
        check_windows.append((0.0, min(3.0, clip_dur), "head", EVAL_PROMPT_SAMPLE))
    if clip_dur >= 6.0:
        check_windows.append((max(0.0, clip_dur - 3.0), clip_dur, "tail", EVAL_PROMPT_SAMPLE))
    if clip_dur >= 30.0:
        mid = clip_dur / 2.0
        check_windows.append((mid - 1.5, mid + 1.5, "mid", EVAL_PROMPT_SAMPLE))

    for win_s, win_e, label, prompt in check_windows:
        if win_e <= win_s:
            continue
        png = verify_dir / f"{Path(clip_path).stem}_{label}.png"
        # Retry timeline generation up to 2 times on failure
        timeline_ok = False
        for retry in range(3):
            try:
                if png.exists():
                    png.unlink()
                tv.render_timeline(
                    Path(clip_path), win_s, win_e, png, 10, None,
                )
                if png.exists() and png.stat().st_size > 0:
                    timeline_ok = True
                    break
            except Exception as e:
                if retry < 2:
                    print(f"        timeline_view {label} 失败，重试 {retry+1}/2: {e!r}")
                else:
                    print(f"        timeline_view {label} 失败，跳过: {e!r}")
        if not timeline_ok:
            continue
        # Retry vision API call up to 2 times on failure
        verdict = None
        for retry in range(3):
            verdict = _vision_judge(client, png, prompt=prompt)
            if verdict is not None:
                break
            if retry < 2:
                print(f"        vision 判定 {label} 失败，重试 {retry+1}/2")
        if verdict and not verdict.get("ok", True):
            issues.extend(verdict.get("issues", []))

    if not issues:
        return None
    return "；".join(issues)[:300]


def _vision_judge(client: OpenAI, png: Path, prompt: str = EVAL_PROMPT_CUT) -> dict | None:
    try:
        b64 = base64.b64encode(png.read_bytes()).decode()
        resp = client.chat.completions.create(
            model=VISION_MODEL,
            messages=[{"role": "user", "content": [
                {"type": "text", "text": prompt},
                {"type": "image_url",
                 "image_url": {"url": f"data:image/png;base64,{b64}"}},
            ]}],
            response_format={"type": "json_object"},
            temperature=0.1, max_tokens=200,
        )
        return safe_json_loads(resp.choices[0].message.content or "")
    except Exception as e:
        print(f"        vision 判失败: {e!r}")
        return None


def _probe_clip_duration(clip_path: Path) -> float:
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", str(clip_path)],
            capture_output=True, text=True, check=True,
        )
        return float(out.stdout.strip() or 0.0)
    except Exception:
        return 0.0


# ────────────────── 合并 + 吸附 ──────────────────

def merge_and_snap(
    clips: list[dict],
    phrases: list[dict],
    min_dur: float,
    max_dur: float,
    max_count: int,
) -> list[dict]:
    """去重、对每个 segment 吸附短语边界、强制时长约束、限制条数。"""
    def _first_start(c: dict) -> float:
        segs = c.get("segments") or []
        if not segs:
            return float(c.get("start", 0) or 0)
        try:
            return float(segs[0].get("start", 0) or 0)
        except (TypeError, ValueError):
            return 0.0

    clips = sorted(clips, key=_first_start)
    snapped: list[dict] = []
    for c in clips:
        raw_segs = c.get("segments")
        if not raw_segs:
            if "start" in c and "end" in c:
                raw_segs = [{"start": c["start"], "end": c["end"]}]
            else:
                continue

        segs_out: list[dict] = []
        for seg in raw_segs:
            try:
                s = float(seg["start"]); e = float(seg["end"])
            except (KeyError, ValueError, TypeError):
                continue
            if e <= s:
                continue
            snap = snap_to_phrases(s, e, phrases)
            if snap is None:
                continue
            ss, ee = snap
            segs_out.append({"start": round(ss, 2), "end": round(ee, 2)})

        if not segs_out:
            continue
        segs_out.sort(key=lambda x: x["start"])
        dedup_segs: list[dict] = []
        for s in segs_out:
            if dedup_segs and s["start"] < dedup_segs[-1]["end"]:
                dedup_segs[-1]["end"] = max(dedup_segs[-1]["end"], s["end"])
            else:
                dedup_segs.append(s)
        segs_out = dedup_segs

        total_dur = sum(s["end"] - s["start"] for s in segs_out)
        if total_dur < min_dur or total_dur > max_dur:
            continue

        def _overlap(a_segs, b_segs) -> float:
            ov = 0.0
            for a in a_segs:
                for b in b_segs:
                    ov += max(0.0, min(a["end"], b["end"]) - max(a["start"], b["start"]))
            return ov

        overlap = False
        for ex in snapped:
            ov = _overlap(segs_out, ex["segments"])
            if ov > 0 and ov >= 0.5 * min(total_dur, sum(s["end"]-s["start"] for s in ex["segments"])):
                overlap = True
                break
        if overlap:
            continue

        snapped.append({
            "segments": segs_out,
            "title": c.get("title", ""), "reason": c.get("reason", ""),
        })
        if len(snapped) >= max_count:
            break
    return snapped


# ────────────────── 主入口 ──────────────────

def select_clips(
    video: Path,
    edit_dir: Path,
    count: int = 0,
    min_duration: float = 30.0,
    max_duration: float = 300.0,
    chunk_minutes: int = 0,
    on_progress=None,
) -> Path:
    """主入口：读 packed.md → 豆包选段 → 写 edl_multi.json。返回 EDL 路径。

    count=0/None：auto 模式，豆包按内容密度自定条数。
    count>0：固定条数模式。
    """
    def _p(stage: str, pct: int, msg: str):
        print(msg, flush=True)
        if on_progress:
            try:
                on_progress(stage, pct, msg.lstrip())
            except Exception:
                pass

    packed_path = edit_dir / "takes_packed.md"
    if not packed_path.exists():
        sys.exit(f"找不到 {packed_path}，请先运行 transcribe + pack")
    packed_md = packed_path.read_text(encoding="utf-8")
    phrases = parse_phrases(packed_md)
    if not phrases:
        sys.exit("packed.md 里没解析到短语")

    api_key, base_url, model = ark_config()
    client = OpenAI(api_key=api_key, base_url=base_url, timeout=600.0)
    if not model:
        model = discover_model(client)

    auto = (count is None or count <= 0)
    video_dur = _probe_video_duration(video) if auto else 0.0
    # auto 模式下的条数上限（用于 merge_and_snap 截断）
    max_count = 16 if video_dur >= 7200 else 12

    if chunk_minutes <= 0:
        _p("select", 20, "  豆包读完整转录，正在选段…（约 30-90 秒）")
        if auto:
            raw_clips = call_doubao_auto(client, model, packed_md, video_dur, min_duration, max_duration)
        else:
            raw_clips = call_doubao_fixed(client, model, packed_md, count, min_duration, max_duration)
    else:
        _p("select", 10, "  超长视频分块选段（跨块关联会丢失）")
        raw_clips = []
        stream_dur = phrases[-1]["end"] if phrases else 0
        chunk_s = chunk_minutes * 60
        n = int(stream_dur // chunk_s) + 1
        for i in range(n):
            w_start = i * chunk_s
            w_end = min(stream_dur, w_start + chunk_s)
            window_md = "\n".join(
                f"[{p['start']:06.2f}-{p['end']:06.2f}] {p['text']}"
                for p in phrases if w_start <= p["start"] < w_end
            )
            if not window_md.strip():
                continue
            _p("select", 10 + int(80 * (i+1) / n), f"  选段 窗口 {i+1}/{n}")
            if auto:
                raw_clips.extend(call_doubao_auto(client, model, window_md, (w_end-w_start), min_duration, max_duration))
            else:
                raw_clips.extend(call_doubao_fixed(client, model, window_md, count, min_duration, max_duration))

    _p("select", 90, "  吸附短语边界、去重…")
    limit = (count if count and count > 0 else max_count)
    final = merge_and_snap(raw_clips, phrases, min_duration, max_duration, limit)
    print(f"豆包选出 {len(final)} 条切片（去重+吸附后）")
    for i, c in enumerate(final):
        segs = c["segments"]
        dur = sum(s["end"] - s["start"] for s in segs)
        segs_str = " + ".join(f"[{s['start']:7.2f}-{s['end']:7.2f}]" for s in segs)
        print(f"  {i+1}. {segs_str}  ({dur:5.1f}s)  {c['title']}")
    _p("select", 100, f"  选出 {len(final)} 条切片")

    clips_dir = edit_dir / "clips"
    clips_dir.mkdir(parents=True, exist_ok=True)
    edl = {
        "version": 3,
        "mode": "multi_clip",
        "sources": {video.stem: str(video.resolve())},
        "clips": [
            {
                "source": video.stem,
                "segments": c["segments"],
                "title": c["title"],
                "reason": c["reason"],
            }
            for c in final
        ],
        "grade": "auto",
        "overlays": [],
        "subtitles": None,
        "total_duration_s": round(
            sum(s["end"] - s["start"] for c in final for s in c["segments"]), 2),
    }
    edl_path = clips_dir / "edl_multi.json"
    edl_path.write_text(json.dumps(edl, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"saved: {edl_path}")
    return edl_path


def main() -> None:
    ap = argparse.ArgumentParser(description="Select highlight clips via Doubao")
    ap.add_argument("video", type=Path, help="Path to source video")
    ap.add_argument("--edit-dir", type=Path, default=None, help="Edit dir (default <video_parent>/edit)")
    ap.add_argument("--count", type=int, default=0, help="Target number of clips (0=auto, LLM decides)")
    ap.add_argument("--min-duration", type=float, default=30.0)
    ap.add_argument("--max-duration", type=float, default=300.0)
    ap.add_argument("--chunk-minutes", type=int, default=0,
                    help="分块分钟数；0=不分块(默认)，>0=超长流降级分块")
    args = ap.parse_args()

    video = args.video.resolve()
    edit_dir = (args.edit_dir or (video.parent / "edit")).resolve()
    select_clips(video, edit_dir, args.count, args.min_duration, args.max_duration, args.chunk_minutes)


if __name__ == "__main__":
    main()
