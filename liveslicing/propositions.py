"""命题提取与精修模块：实现命题驱动切片的两个核心 LLM 阶段。

核心功能：
1. extract_propositions: 从完整转录文本全文穷举所有值得切片的精彩命题（观点/金句/冲突/故事等），
   保证不遗漏，全文一次性输入给LLM
2. refine_propositions: 对用户选中的每个命题，在局部上下文窗口内精修切点（找钩子、多段拼接、时长控制），
   输出与现有 EDL v3 兼容的 clip 定义，render 模块无需改动
3. 支持 propositions.json 缓存读写（mtime 校验自动失效）

设计原则：
- 命题提取阶段必须全文输入，保证全局视角不遗漏
- 精修阶段使用局部上下文窗口（命题范围前后各120秒padding），快速精准
- 精修结果复用现有 snap_to_phrases 逻辑，保证切点质量
"""

from __future__ import annotations

import json
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Iterator

from openai import OpenAI

from liveslicing.config import ark_config
from liveslicing.pick_clips import (
    discover_model,
    parse_phrases,
    safe_json_loads,
    snap_to_phrases,
)


# ────────────────── 命题提取系统提示词 ──────────────────

PROPOSE_SYSTEM_PROMPT = """\
你是资深直播内容分析师，核心任务是**穷举所有值得剪成独立短视频的精彩内容，宁多勿漏，完整性优先**。
请通读完整份直播台词后，列出所有有传播价值的内容命题。用户会自己筛选最终要剪的内容，你只负责找全、找完整，不要自行过滤你觉得"不够好"的内容。

⚠️ 核心原则：此阶段仅负责识别内容和圈定大致范围，**不需要追求切点精准、不需要卡时长、不需要过滤铺垫、不需要裁剪冗余**。后续会有专门的精修阶段来收窄切点、优化开头结尾、裁剪冗余、控制时长、对齐语句边界，所以你圈的时间范围宽一点完全没关系，窄了漏核心内容才是致命问题。

【命题分类（选最贴切的主分类即可，跨分类内容选核心属性对应的分类，不需要严格互斥）】
- 金句：反常识观点/犀利结论/情绪价值表达/直击痛点的总结（带货直播中高转化的痛点戳中、报价福利钩子也归入此类）
- 冲突：观点争论/情绪爆发/连麦对峙/质疑回应
- 干货：实用方法/教程攻略/经验总结/行业内幕/避坑指南/产品核心卖点讲解
- 故事：亲身经历/真实案例/用户见证/八卦幕后/创业故事
- 反转：神回应/剧情转折/悬念揭晓/打脸现场
- 趣味：搞笑名场面/玩梗/口误/主播互动整活/直播间突发趣事
- 回应：观众答疑/问题解答/针对性科普

【输出字段（严格按要求输出，字段不要增减）】
id：从1开始连续编号
title：≤15字，短视频标题风格，抓眼球带钩子，明确核心亮点
summary：2-3句话，讲清核心内容和传播价值，让人一眼知道这个片段讲什么、为什么值得看
category：从上述7个分类中选最贴切的主分类
score：1-5分，评分参考：
  5分=必切爆款：强情绪冲击/反常识/短平快爆点/极高实用价值，发出去大概率有高播放
  4分=很值得切：内容扎实有干货/有趣有记忆点/价值明确
  3分=可切可不切：普通内容/常规讲解/价值点不突出
  2分=备胎：内容平淡无亮点/缺乏传播性
  1分=边角料：无价值/重复内容/无关闲聊
start/end：大致时间戳（秒），**宁宽30秒不窄1秒**：必须完整包含命题的引入、论证/展开、结论/包袱全过程，前后相关的衔接和观众反应也可以包含进去，哪怕范围大一点、有少量过渡冗余也没关系，精修会处理；单条命题建议范围不超过15分钟，过长的连续内容如果有明显子主题可以拆分
preview：原文中最核心、最有冲击力的2-4句连续原话，≤80字，必须一字不差复制，不总结不改写不省略主语，直接放最炸的那几句结论/爆点/核心观点，**哪怕铺垫内容在时间范围内，也不要选铺垫句放在preview里**

【硬规则——必须严格遵守】
1. 必须通读完全文再输出，不要看一部分就开始写，避免漏后面的精彩内容
2. 命题100%独立自包含：标题/摘要/preview必须明确说清主体，禁止用"它/这个/那个/这种"等无主语代词开头，脱离上下文单独看也能完全看懂讲的是什么（正例："益生菌不要乱吃"✅，反例："不要乱吃这个"❌）
3. 完整性第一：对于干货方法、完整故事、逻辑论证类内容，必须把前因后果、完整论点、所有关键点、结论都包含进去，绝对不能因为怕范围大而截断核心内容、漏了关键论点/包袱/结论/观众反应
4. 允许时间重叠：同一段内容有不同角度的看点（既是金句又是干货）要分多条列出，不要合并，让用户自己选择角度
5. 长短都要：20分钟的完整干货教程作为整体列出，十几秒的短爆点/神回应金句也必须列出，不要因为内容太短或太长忽略
6. 明确排除以下内容，绝对不要列入：
   - 纯无意义凑数内容：反复说的"大家好把666打公屏""点关注不迷路""接下来讲下一个"这类纯引导/过渡话术
   - 硬广凑数内容："321上链接""左下角小黄车下单""只剩最后XX单"这类纯催促下单的无信息话术（有价格/福利/痛点钩子的内容除外，归为金句类）
   - 无关内容：主播念礼物感谢/和助理聊无关私事/设备调试/纯停顿沉默等和内容无关的片段
   - 违规敏感内容：涉及医疗宣称/极限词/敏感言论/违规引导的内容不要列入
7. 重复内容判断：主播为了强调反复讲的完全相同的观点/内容只列一次，换角度/举不同例子讲同一主题算不同命题保留；完全雷同的重复内容合并，不同角度的重叠内容保留
8. 按时间顺序排列，不要按评分高低排序；所有命题id连续编号，不要跳号
9. 每条命题核心内容时长≥10秒（范围包含的铺垫不算，只要实际有价值的核心内容超过10秒即可）

只输出纯JSON对象，不要markdown代码块围栏、不要任何解释说明、不要多余文字：
{"propositions":[{"id":1,"title":"...","summary":"...","category":"金句","score":5,"start":120.5,"end":180.3,"preview":"原话..."}]}
"""


# ────────────────── 命题精修系统提示词 ──────────────────

REFINE_SYSTEM_PROMPT = """\
你是直播切片精修师，擅长从原始直播素材中剪出吸引人的短视频。
给你一段台词上下文和目标命题（标题+摘要+大致范围），请精修出一条高质量的切片定义。

【标记说明】
- `>>> 目标命题大致开始 <<<` 和 `<<< 目标命题大致结束 >>>` 之间是命题核心范围
- 前后文本是上下文缓冲，用于找开头/结尾衔接

【精修规则（优先级从高到低）】
1. **严格围绕命题主题**：只剪和给定命题直接相关的内容，窗口里其他无关的精彩内容不要管，绝对不能剪跑题到别的主题
2. **开头钩子+主语完整**：前3秒必须抓眼球，起点落在金句/冲突/爆点上
   ❌ 禁止：
   - 发语词/口癖开头（啊/那个/就是/对吧/大家好等）
   - 无主语代词开头（它/这个/那种/这就是等），必须明确主体（"磷虾油能降血脂"✅，"能降血脂"❌）
   ✅ 如果范围内开头不好，可以从标记前**最多30秒**上下文找钩子，但必须和命题直接相关，不能为钩子凑无关内容
3. **内容完整+结尾利落**：完整呈现核心观点/故事，不切关键论证/包袱/结论；结尾落在观点讲完、包袱响完、静音停顿处，不半路切断话尾；结尾有笑声/掌声等观众反应时，要把完整反应包含进去
4. **时长控制**：总时长尽量控制在[MIN_DUR, MAX_DUR]秒之间；高评分金句/反转/趣味类短爆点最短允许15秒；如果核心观点完整讲完自然超MAX_DUR，最多允许超出20%，不要为了卡时长砍掉关键结论/包袱，绝不为了凑时长留废话
5. **多段拼接规则**：不限制拼接段数，也不限制段间间隔，但必须满足：所有段围绕同一命题主题、语义连贯，绝对禁止拼接不相关内容；严格按时间顺序排列，禁止倒序；拼接处优先选有≥400ms静音间隔、或有笑声/掌声自然停顿的位置，剪完不跳戏
6. **切点工艺**：
   - start/end必须是文本中出现过的短语时间戳，不自己造
   - 优先≥400ms静音处，150-400ms可用，禁止<150ms处切
   - 保留笑声/掌声等音频事件，结尾可延后包含观众反应
7. **标题**：短视频标题，可沿用命题title或优化得更抓眼球
8. **reason**：简要说明选点逻辑和拼接调整

【输出格式】纯JSON，无其他内容：
{"segments":[{"start":123.45,"end":145.67}],"title":"标题","reason":"说明"}
内容不足无法剪出合格切片时输出{"segments":[],"title":"","reason":"内容不足"}。
"""


# ────────────────── LLM 调用工具函数 ──────────────────

def _call_llm_json(
    client: OpenAI,
    model: str,
    system_prompt: str,
    user_prompt: str,
    temperature: float = 0.3,
    max_retries: int = 2,
    timeout: float = 300.0,
    validate_empty: callable = None,
) -> dict | None:
    """调用 LLM 并解析 JSON 响应，带 JSON 修复/空结果重试。

    Args:
        client: OpenAI 兼容客户端
        model: 模型ID
        system_prompt: 系统提示词
        user_prompt: 用户提示词
        temperature: 生成温度
        max_retries: JSON解析失败/空结果最大重试次数
        timeout: 请求超时秒数
        validate_empty: 可选校验函数，签名为(result)→bool，返回True表示结果为空需要重试

    Returns:
        解析后的字典，全部重试失败返回None
    """
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]
    last_content = ""
    for attempt in range(max_retries + 1):
        try:
            resp = client.chat.completions.create(
                model=model,
                messages=messages,
                response_format={"type": "json_object"},
                temperature=temperature,
                timeout=timeout,
            )
            content = resp.choices[0].message.content or ""
            last_content = content
            data = safe_json_loads(content)
            # 校验是否为空结果
            if validate_empty and validate_empty(data):
                if attempt < max_retries:
                    wait = 2
                    print(f"        LLM返回空结果，{wait}秒后重试({attempt+1}/{max_retries})", file=sys.stderr)
                    messages = [
                        {"role": "system", "content": system_prompt + "\n\n上次输出为空结果，请务必通读全文后输出所有找到的内容，不要遗漏。只输出纯JSON对象。"},
                        {"role": "user", "content": user_prompt},
                    ]
                    temperature = max(0.1, temperature - 0.1)
                    time.sleep(wait)
                    continue
                print(f"        LLM返回空结果，已重试{max_retries}次", file=sys.stderr)
                return None
            return data
        except json.JSONDecodeError:
            if attempt < max_retries:
                # 重试时强化"只输出纯JSON"指令
                messages = [
                    {"role": "system", "content": system_prompt + "\n\n务必只输出纯JSON对象，不要输出任何其它文字，不要markdown围栏。"},
                    {"role": "user", "content": user_prompt},
                    {"role": "assistant", "content": last_content},
                    {"role": "user", "content": "上面不是合法JSON，请只输出JSON对象，不要任何解释。"},
                ]
                temperature = max(0.1, temperature - 0.1)
                wait = (attempt + 1) * 2
                print(f"        JSON解析失败，{wait}秒后重试({attempt+1}/{max_retries})", file=sys.stderr)
                time.sleep(wait)
                continue
            print(f"        LLM JSON解析失败，已重试{max_retries}次", file=sys.stderr)
            return None
        except Exception as e:
            if attempt < max_retries:
                wait = (attempt + 1) * 2
                print(f"        LLM调用失败: {e!r}，{wait}秒后重试({attempt+1}/{max_retries})", file=sys.stderr)
                time.sleep(wait)
                continue
            print(f"        LLM调用失败，已重试{max_retries}次: {e!r}", file=sys.stderr)
            return None
    return None


def _count_tokens_approx(text: str) -> int:
    """粗略估算文本token数（中文按约1.5字/token，英文按约4字符/token，混合估算偏保守）。"""
    # 简单估算：中文字符每个约1 token，英文单词每个约1 token，标点忽略
    cjk = len(re.findall(r'[一-鿿]', text))
    other = len(re.findall(r'[a-zA-Z0-9]+', text))
    return cjk + other


# ────────────────── 公共API ──────────────────

def extract_propositions(
    packed_md: str,
    video_dur_s: float,
    client: OpenAI | None = None,
    model: str | None = None,
    on_log=None,
) -> list[dict]:
    """从完整转录文本中穷举所有精彩命题，保证不遗漏。

    策略：将完整的 takes_packed.md 全文一次性发送给 LLM，保证全局视角、不遗漏跨段关联的命题和故事线。
    豆包 256K 上下文窗口可轻松容纳约20小时直播的打包转录（打包后约1/10 token密度），覆盖绝大多数使用场景。

    Args:
        packed_md: takes_packed.md 的完整内容
        video_dur_s: 视频总时长（秒）
        client: OpenAI客户端，为None时自动创建
        model: 模型ID，为None时自动探测
        on_log: 日志回调函数，签名为(msg: str)

    Returns:
        命题列表，每个元素包含id/title/summary/category/score/start/end/preview字段
    """
    def _log(msg: str):
        # 只通过回调输出日志，避免重复打印到stderr导致日志重复
        if on_log:
            try:
                on_log(msg)
            except Exception:
                pass

    # 初始化客户端
    if client is None:
        api_key, base_url, _model = ark_config()
        client = OpenAI(api_key=api_key, base_url=base_url, timeout=600.0)
        if model is None:
            model = _model
    if model is None:
        model = discover_model(client)

    phrases = parse_phrases(packed_md)
    if not phrases:
        return []

    approx_tokens = _count_tokens_approx(packed_md)
    _log(f"  [propose] 转录文本约 {len(packed_md)} 字符，估算 ~{approx_tokens} tokens")
    _log(f"  [propose] 全文通读，穷举提取命题…")

    user_prompt = (
        f"视频总时长约 {video_dur_s/60:.0f} 分钟。\n\n"
        f"以下是完整的直播台词文本（每行格式[开始时间-结束时间] 台词），请逐行通读后穷举所有值得切片的命题：\n\n"
        f"{packed_md}"
    )
    # 空结果校验：propositions数组长度为0则重试
    data = _call_llm_json(
        client, model, PROPOSE_SYSTEM_PROMPT, user_prompt,
        temperature=0.3, timeout=600, max_retries=1,
        validate_empty=lambda d: not d or not d.get("propositions"),
    )
    if not data:
        _log("  [propose] 警告：命题提取返回空结果，已重试1次仍失败")
        return []
    props = data.get("propositions", [])
    props = _validate_propositions(props, phrases)
    _log(f"  [propose] 提取到 {len(props)} 个命题")
    return props


def _validate_propositions(props: list[dict], phrases: list[dict]) -> list[dict]:
    """校验并清理命题列表，修复非法字段。

    Args:
        props: LLM返回的原始命题列表
        phrases: 解析后的短语列表，用于校验时间戳

    Returns:
        清理后的合法命题列表
    """
    valid = []
    for i, p in enumerate(props):
        try:
            start = float(p.get("start", 0))
            end = float(p.get("end", 0))
            score = int(p.get("score", 3))
            title = str(p.get("title", "")).strip()
            summary = str(p.get("summary", "")).strip()
            category = str(p.get("category", "其他")).strip()
            preview = str(p.get("preview", "")).strip()
        except (TypeError, ValueError):
            continue
        if not title or end <= start + 10:  # 时长<10秒丢弃
            continue
        # 时间戳边界校验
        if phrases:
            if start < 0:
                start = 0
            if end > phrases[-1]["end"]:
                end = phrases[-1]["end"]
            if end <= start:
                continue
        # 评分clamp到1-5
        score = max(1, min(5, score))
        # 分类映射，统一别名
        cat_map = {
            "金句": "金句", "观点": "金句", "洞见": "金句",
            "冲突": "冲突", "争论": "冲突", "对峙": "冲突", "情绪": "冲突",
            "干货": "干货", "教程": "干货", "方法": "干货", "经验": "干货", "建议": "干货",
            "故事": "故事", "经历": "故事", "案例": "故事", "八卦": "故事",
            "反转": "反转", "转折": "反转",
            "趣味": "趣味", "搞笑": "趣味", "梗": "趣味", "名场面": "趣味",
            "回应": "回应", "答疑": "回应", "问答": "回应",
        }
        category = cat_map.get(category, "其他")
        # 预览截短到80字
        if len(preview) > 80:
            preview = preview[:77] + "..."
        valid.append({
            "id": i + 1,  # 重新编号
            "title": title,
            "summary": summary,
            "category": category,
            "score": score,
            "start": round(start, 2),
            "end": round(end, 2),
            "preview": preview,
        })
    # 按时间排序
    valid.sort(key=lambda x: x["start"])
    # 重新编号
    for i, p in enumerate(valid):
        p["id"] = i + 1
    return valid


def build_prop_context_window(
    phrases: list[dict],
    prop_start: float,
    prop_end: float,
    padding: float = 120.0,
    min_window: float = 240.0,
) -> tuple[str, float, float]:
    """为单个命题构建精修用的局部上下文窗口。

    在命题范围前后各加padding秒，保证总窗口不小于min_window秒。
    在窗口文本中插入标记帮助LLM定位命题范围。

    Args:
        phrases: 完整短语列表
        prop_start: 命题大致开始时间
        prop_end: 命题大致结束时间
        padding: 前后padding秒数，默认90秒
        min_window: 最小窗口总时长（秒），默认3分钟

    Returns:
        (窗口markdown文本, 窗口开始时间, 窗口结束时间)
    """
    win_start = max(0.0, prop_start - padding)
    win_end = prop_end + padding
    # 保证窗口最小宽度
    if win_end - win_start < min_window:
        extra = (min_window - (win_end - win_start)) / 2
        win_start = max(0.0, win_start - extra)
        win_end = win_end + extra

    # 提取窗口内的短语
    window_phrases = [p for p in phrases if win_start - 5 <= p["start"] and p["end"] <= win_end + 5]
    if not window_phrases:
        return "", win_start, win_end

    # 构建文本，在命题范围前后插入标记
    lines = []
    marked_start = False
    marked_end = False
    for p in window_phrases:
        if not marked_start and p["start"] >= prop_start:
            lines.append(">>> 目标命题大致开始 <<<")
            marked_start = True
        lines.append(f"[{p['start']:06.2f}-{p['end']:06.2f}] {p['text']}")
        if not marked_end and p["end"] >= prop_end:
            lines.append("<<< 目标命题大致结束 >>>")
            marked_end = False  # 只插入一次
            marked_end = True
    if not marked_start:
        lines.insert(0, ">>> 目标命题大致开始 <<<")
    if not marked_end:
        lines.append("<<< 目标命题大致结束 >>>")

    return "\n".join(lines), win_start, win_end


def refine_single_proposition(
    prop: dict,
    phrases: list[dict],
    min_dur: float,
    max_dur: float,
    client: OpenAI,
    model: str,
) -> dict | None:
    """精修单个命题，返回clip定义（segments/title/reason），失败自动重试1次，仍失败返回None。"""
    for attempt in range(2):
        try:
            window_text, _, _ = build_prop_context_window(phrases, prop["start"], prop["end"])
            if not window_text.strip():
                return None
            user_prompt = (
                f"目标命题：{prop['title']}\n"
                f"命题摘要：{prop['summary']}\n"
                f"命题大致时间范围：{prop['start']:.1f}s - {prop['end']:.1f}s\n"
                f"单条切片时长要求：{min_dur:.0f}-{max_dur:.0f}秒\n\n"
                f"以下是上下文文本（标记之间是目标范围，前后是缓冲上下文）：\n\n"
                f"{window_text}"
            )
            sys_prompt = REFINE_SYSTEM_PROMPT.replace("MIN_DUR", str(int(min_dur))).replace("MAX_DUR", str(int(max_dur)))
            temp = 0.2 if attempt == 0 else 0.1
            data = _call_llm_json(client, model, sys_prompt, user_prompt, temperature=temp, timeout=600, max_retries=1)
            if not data:
                if attempt == 0:
                    time.sleep(2)
                    continue
                return None
            segments = data.get("segments", [])
            if not segments:
                if attempt == 0:
                    time.sleep(2)
                    continue
                return None
            # 校验segment格式
            valid_segs = []
            for seg in segments:
                try:
                    s = float(seg["start"])
                    e = float(seg["end"])
                    if e > s:
                        valid_segs.append({"start": round(s, 2), "end": round(e, 2)})
                except (KeyError, TypeError, ValueError):
                    continue
            if not valid_segs:
                if attempt == 0:
                    time.sleep(2)
                    continue
                return None
            # 按时间排序
            valid_segs.sort(key=lambda x: x["start"])
            return {
                "segments": valid_segs,
                "title": str(data.get("title", prop["title"])).strip() or prop["title"],
                "reason": str(data.get("reason", "")).strip(),
                "category": prop.get("category", ""),
                "score": prop.get("score", 3),
            }
        except Exception as e:
            if attempt == 0:
                print(f"        精修命题#{prop.get('id')}失败(尝试{attempt+1}/2): {e!r}，2秒后重试", file=sys.stderr)
                time.sleep(2)
                continue
            print(f"        精修命题#{prop.get('id')}失败，已重试1次: {e!r}", file=sys.stderr)
            return None
    return None


def refine_propositions(
    phrases: list[dict],
    selected_props: list[dict],
    min_dur: float,
    max_dur: float,
    client: OpenAI | None = None,
    model: str | None = None,
    max_concurrency: int = 3,
    on_progress=None,
    on_log=None,
    failed_props: list | None = None,
) -> list[dict]:
    """对用户选中的命题逐个精修切点，支持并发加速。

    Args:
        phrases: 完整短语列表（来自parse_phrases）
        selected_props: 用户选中的命题列表
        min_dur: 单条最小时长（秒）
        max_dur: 单条最大时长（秒）
        client: OpenAI客户端
        model: 模型ID
        max_concurrency: 最大并发LLM调用数，默认3
        on_progress: 进度回调 (done: int, total: int)
        on_log: 日志回调
        failed_props: 可选列表，用于收集精修失败的命题标题

    Returns:
        精修后的clip列表（已做短语边界吸附、去重、时长校验）
    """
    def _log(msg: str):
        # 只通过回调输出日志，避免重复打印到stderr导致日志重复
        if on_log:
            try:
                on_log(msg)
            except Exception:
                pass

    if not selected_props:
        return []

    if client is None:
        api_key, base_url, _model = ark_config()
        client = OpenAI(api_key=api_key, base_url=base_url, timeout=300.0)
        if model is None:
            model = _model
    if model is None:
        model = discover_model(client)

    total = len(selected_props)
    raw_clips = []
    done = 0

    _log(f"  [refine] 开始精修 {total} 个选中命题，并发度{max_concurrency}…")

    def _refine_one(prop: dict) -> dict | None:
        return refine_single_proposition(prop, phrases, min_dur, max_dur, client, model)

    with ThreadPoolExecutor(max_workers=max_concurrency) as pool:
        futures = {pool.submit(_refine_one, p): p for p in selected_props}
        for fut in as_completed(futures):
            prop = futures[fut]
            result = fut.result()
            done += 1
            if result:
                raw_clips.append(result)
                _log(f"    ({done}/{total}) ✓ 命题#{prop['id']} {prop['title']}")
            else:
                _log(f"    ({done}/{total}) ✗ 命题#{prop['id']} {prop['title']} 精修失败，跳过")
                if failed_props is not None:
                    failed_props.append(f"#{prop['id']} {prop['title']}")
            if on_progress:
                try:
                    on_progress(done, total)
                except Exception:
                    pass

    # 短语边界吸附 + 去重 + 时长校验
    _log(f"  [refine] 精修完成，吸附短语边界、去重、时长校验…")
    snapped = []
    for c in raw_clips:
        segs_out = []
        for seg in c["segments"]:
            snap = snap_to_phrases(seg["start"], seg["end"], phrases)
            if snap:
                ss, ee = snap
                segs_out.append({"start": round(ss, 2), "end": round(ee, 2)})
        if not segs_out:
            continue
        # 开头口癖兜底调整
        segs_out = _trim_opening_filler(segs_out, phrases, c.get("category", ""), c.get("score", 3))

        total_dur = sum(s["end"] - s["start"] for s in segs_out)
        # 高评分短爆点允许最短15秒，其他保持min_dur；上限允许超出20%
        min_allowed = min_dur
        if c.get("score", 0) >= 4 and c.get("category") in ("金句", "反转", "趣味"):
            min_allowed = 15.0
        max_allowed = max_dur * 1.2
        if total_dur < min_allowed or total_dur > max_allowed:
            _log(f"    跳过\"{c['title']}\"：时长{total_dur:.1f}s超出范围{min_allowed:.0f}-{max_allowed:.0f}s")
            continue
        snapped.append({
            "segments": segs_out,
            "title": c["title"],
            "reason": c.get("reason", ""),
        })

    _log(f"  [refine] 最终得到 {len(snapped)} 条有效切片")
    return snapped


# 开头纯口癖/发语词兜底过滤（只处理最无意义的语气词，防误伤合法内容）
_FILLER_STARTS = {
    "啊", "哦", "嗯", "呃", "就是", "对吧", "其实", "怎么说呢", "那个", "然后呢",
    "你懂吧", "我跟你说", "是吧", "对对对", "啊，", "哦，", "嗯，", "呃，", "就是说",
}


def _trim_opening_filler(segments: list[dict], phrases: list[dict], category: str, score: int) -> list[dict]:
    """保守兜底：微调开头起点，跳过纯口癖/无意义发语词，最多后跳不超过5秒，防误伤。

    规则：
    - 只有高评分（≥4分）的金句/反转/趣味类内容做积极调整，普通内容保守处理
    - 最多往后看3个短语（约3-5秒），如果前2个都是纯口癖才调整，否则不碰
    - 调整后保证segment有意义，不切到无主语内容
    """
    if not segments:
        return segments
    # 只对高评分的短内容做兜底，普通内容完全靠LLM保证
    is_short_highlight = score >= 4 and category in ("金句", "反转", "趣味")
    if not is_short_highlight:
        return segments

    first_seg = segments[0]
    orig_start = first_seg["start"]
    max_jump = orig_start + 5.0  # 最多后跳5秒

    # 从orig_start开始往后找短语，最多看3个
    found = None
    filler_count = 0
    for p in phrases:
        if p["start"] < orig_start - 0.1:
            continue
        if p["start"] > max_jump:
            break
        text = p["text"].strip()
        is_filler = text in _FILLER_STARTS
        if is_filler:
            filler_count += 1
            continue
        # 第一个非口癖短语，如果前面有至少1个口癖，调整到这里
        if filler_count >= 1:
            found = p["start"]
        break

    if found is not None and found > orig_start:
        new_seg = dict(first_seg)
        new_seg["start"] = round(found, 2)
        # 调整后seg不能是负时长
        if new_seg["end"] > new_seg["start"] + 0.5:
            segments[0] = new_seg
    return segments


# ────────────────── 缓存读写 ──────────────────

def save_propositions(edit_dir: Path, propositions: list[dict], video_path: Path, video_dur_s: float) -> Path:
    """保存命题列表到propositions.json缓存文件。

    Args:
        edit_dir: 工作目录
        propositions: 命题列表
        video_path: 源视频路径（记录元信息）
        video_dur_s: 视频总时长

    Returns:
        保存的文件路径
    """
    from datetime import datetime
    data = {
        "version": 1,
        "video": video_path.name,
        "video_path": str(video_path.resolve()),
        "video_duration_s": round(video_dur_s, 2),
        "created_at": datetime.now().isoformat(),
        "propositions": propositions,
    }
    out_path = edit_dir / "propositions.json"
    out_path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    return out_path


def load_propositions(edit_dir: Path) -> list[dict] | None:
    """从缓存加载命题列表，如果缓存失效（takes_packed.md更新过）返回None。

    Args:
        edit_dir: 工作目录

    Returns:
        命题列表，缓存不存在或失效时返回None
    """
    props_path = edit_dir / "propositions.json"
    packed_path = edit_dir / "takes_packed.md"
    if not props_path.exists() or not packed_path.exists():
        return None
    # mtime校验：propositions必须新于takes_packed.md
    if props_path.stat().st_mtime < packed_path.stat().st_mtime:
        return None
    try:
        data = json.loads(props_path.read_text(encoding="utf-8"))
        props = data.get("propositions", [])
        if props:
            return props
    except Exception:
        return None
    return None
