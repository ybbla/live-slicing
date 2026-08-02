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
你是资深直播内容分析师，核心任务是**将整个直播转录文本全面拆解为独立完整的内容命题，最大化覆盖所有有实际意义的内容，宁多勿漏，全覆盖优先**。
请通读完整份直播台词后，将所有可以独立成篇、表达了完整观点/内容/事件的片段都拆分为命题。用户会自己筛选最终要剪的内容，你只负责拆全、拆完整，不要自行过滤你觉得"不够精彩"的内容，只要是有明确主题的完整内容都要列出来。

⚠️ 核心原则：此阶段仅负责识别内容和圈定大致范围，**不需要追求切点精准、不需要卡时长、不需要过滤铺垫、不需要裁剪冗余**。后续会有专门的精修阶段来收窄切点、优化开头结尾、裁剪冗余、控制时长、对齐语句边界，所以你圈的时间范围宽一点完全没关系，窄了漏核心内容才是致命问题。

【输出字段（严格按要求输出，字段不要增减）】
id：从1开始连续编号
title：≤20字，明确核心主题，清晰说明这个命题讲的是什么，不需要刻意追求夸张抓眼球的标题党风格，准确清晰即可
summary：2-3句话，讲清核心内容，让人一眼知道这个片段讲了什么
start/end：精确时间戳（秒），直接使用转录文本中该命题核心内容第一行的开始时间作为start，最后一行的结束时间作为end，完全对齐文本边界，不需要额外预留冗余、不要包含不属于该命题的无关过渡内容；必须完整包含命题的核心内容（引入、论证/展开、结论），绝对不要为了追求边界精准截断核心观点/结论；单条命题建议范围不超过15分钟，过长的连续内容如果有明显子主题可以拆分。精修阶段会自动在前后上下文窗口中查找更自然的开头结尾切点，不需要在此阶段预留冗余
preview：原文中最能代表核心内容的2-4句连续原话，≤100字，必须一字不差复制，不总结不改写不省略主语，选择核心内容原文即可，不需要刻意挑选最有冲击力的爆点句子

【硬规则——必须严格遵守】
1. 必须通读完全文再输出，不要看一部分就开始写，避免漏后面的内容
2. 命题100%独立自包含：标题/摘要/preview必须明确说清主体，禁止用"它/这个/那个/这种"等无主语代词开头，脱离上下文单独看也能完全看懂讲的是什么（正例："益生菌不要乱吃"✅，反例："不要乱吃这个"❌）
3. 完整性第一：对于方法讲解、完整故事、逻辑论证类内容，必须把前因后果、完整论点、所有关键点、结论都包含进去，绝对不能因为怕范围大而截断核心内容、漏了关键论点/结论
4. 最大化覆盖：所有有实际信息含量的内容都要拆分为命题，包括普通的内容讲解、常规话题讨论、观点表达、故事分享、问题解答等，不要只挑所谓"爆点"内容，尽量做到所有非无意义内容都有对应的命题覆盖
5. 双粒度并存：既要有覆盖完整大主题的长命题（如完整的教程讲解、完整的故事分享、完整的问题解答），也要有大主题内部独立的子观点、短回应、小案例、金句等细粒度命题，哪怕子命题的时间范围完全被大命题包含也要单独列出，不要因为内容属于某个大主题就合并，大小粒度的命题都要保留，用户会自己选择
6. 时间重叠自由：命题之间允许部分重叠、完全包含，不需要刻意调整边界避免重叠，同一段内容有不同主题的拆分角度要分多条列出，不要合并，让用户自己选择角度
7. 长短都要：20分钟的完整教程作为整体列出，十几秒的短观点/短回应也必须列出，不要因为内容太短或太长忽略
8. 明确排除以下内容，绝对不要列入：
   - 纯无意义凑数内容：反复说的"大家好把666打公屏""点关注不迷路""接下来讲下一个"这类纯引导/过渡话术
   - 纯催促下单话术："321上链接""左下角小黄车下单""只剩最后XX单""还没付款的抓紧拍""手慢无"这类无信息含量的纯促单内容（有实际价格/福利/产品卖点/痛点讲解的实质内容除外）
   - 无关内容：主播念礼物感谢/和助理聊无关私事/设备调试/纯停顿沉默/无实质内容的简单附和感叹等和内容主题无关的片段
   - 违规敏感内容：涉及医疗宣称/极限词/敏感言论/违规引导的内容不要列入
9. 重复内容判断：主播为了强调反复讲的完全相同的观点/内容只列一次，换角度/举不同例子讲同一主题算不同命题保留；完全雷同的重复内容合并，不同角度的重叠内容保留
10. 按时间顺序排列；所有命题id连续编号，不要跳号
11. 每条命题核心内容时长≥10秒：无实质内容的短感叹、简单附和不需要单独列为命题，只要是有完整观点/内容的片段，哪怕只有十几秒也要保留（范围包含的铺垫不算时长）

只输出纯JSON对象，不要markdown代码块围栏、不要任何解释说明、不要多余文字：
{"propositions":[{"id":1,"title":"...","summary":"...","start":120.5,"end":180.3,"preview":"原话..."}]}
"""


# ────────────────── 命题精修系统提示词 ──────────────────

REFINE_SYSTEM_PROMPT = """\
你是直播切片精修师，擅长从原始直播素材中剪出吸引人的短视频。
给你一段台词上下文和目标命题（标题+摘要+大致范围），请精修出一条高质量的切片定义。

【标记说明】
- `>>> 目标命题大致开始 <<<` 和 `<<< 目标命题大致结束 >>>` 之间是命题核心范围
- 前后文本是上下文缓冲，用于找开头/结尾衔接

【精修规则（优先级从高到低）】
1. **严格围绕命题主题**：只剪和给定命题直接相关的内容，窗口里其他无关的精彩内容不要管，绝对不能剪跑题到别的主题；命题范围内如果出现和主题无关的插话、观众互动闲聊、主播临时跑题内容、纯口癖重复，可以直接跳过不剪，通过多段拼接把同主题的有效内容连起来即可，不需要把范围内所有内容都包含进去
2. **开头利落+主语完整**：开头直接切入主题，避免无意义铺垫，前3秒直接进入核心内容，不要拖沓
   ❌ 禁止：
   - 发语词/口癖开头（啊/那个/就是/对吧/大家好等）
   - 无主语代词开头（它/这个/那种/这就是等），必须明确主体（"磷虾油能降血脂"✅，"能降血脂"❌）
   ✅ 如果范围内开头不好，可以从标记前**最多30秒**上下文找合适的起点，但必须和命题直接相关，不能为凑开头加入无关内容
3. **内容完整+结尾利落**：完整呈现核心观点/故事，不切关键论证/包袱/结论；结尾落在观点讲完、包袱响完、静音停顿处，不半路切断话尾；结尾有笑声/掌声等观众反应时，最多保留2秒自然收尾即可，不要留过长的空白、停顿或者后续无关内容
4. **时长控制**：总时长尽量控制在[MIN_DUR, MAX_DUR]秒之间；所有内容最短允许15秒；如果核心观点完整讲完自然超MAX_DUR，最多允许超出20%，不要为了卡时长砍掉关键结论/包袱，绝不为了凑时长留废话
5. **多段拼接规则**：不限制拼接段数，段与段之间的无关内容间隔最多不超过30秒，超过则不要强行拼接，只保留连续相关的内容即可；必须满足：所有段围绕同一命题主题、语义连贯，绝对禁止拼接不相关内容；严格按时间顺序排列，禁止倒序；拼接处优先选有≥400ms静音间隔、或有笑声/掌声自然停顿的位置，剪完不跳戏
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


# ────────────────── 自定义命题（多选中合并）生成提示词 ──────────────────

CUSTOM_PROP_SYSTEM_PROMPT = """\
你是直播内容分析师，请根据给定的多段直播内容，总结成一个完整独立的内容命题。
这些内容是用户手动选中的多个相关片段，你需要将它们整合为一个连贯的主题，就像正常提取命题一样输出标题、摘要和原文预览。

【输出要求】
- title：≤25字，清晰准确概括这些内容的整体主题，不需要夸张标题党风格
- summary：2-3句话，讲清整合后的核心内容，让人一眼知道讲了什么
- preview：原文中最能代表核心内容的2-4句连续原话，≤100字，必须一字不差复制原文，不要改写
- 命题必须独立自包含：标题/摘要/preview禁止使用无主语代词（它/这个/那个等），脱离上下文也能看懂
- 所有内容围绕同一个核心主题，忽略中间穿插的无关插话内容
- 不需要做切点判断，只需要总结内容主题即可

只输出纯JSON对象，不要markdown代码块围栏、不要任何解释说明、不要多余文字：
{"title":"...","summary":"...","preview":"原话..."}
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
        命题列表，每个元素包含id/title/summary/start/end/preview字段
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
            title = str(p.get("title", "")).strip()
            summary = str(p.get("summary", "")).strip()
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
        # 预览截短到100字
        if len(preview) > 100:
            preview = preview[:97] + "..."
        valid.append({
            "id": i + 1,  # 重新编号
            "title": title,
            "summary": summary,
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
            }
        except Exception as e:
            if attempt == 0:
                print(f"        精修命题#{prop.get('id')}失败(尝试{attempt+1}/2): {e!r}，2秒后重试", file=sys.stderr)
                time.sleep(2)
                continue
            print(f"        精修命题#{prop.get('id')}失败，已重试1次: {e!r}", file=sys.stderr)
            return None
    return None


def build_custom_prop_context(
    phrases: list[dict],
    selected_props: list[dict],
) -> tuple[str, float, float]:
    """为用户选中的多个命题构建上下文文本，用于生成合并后的新命题。
    仅包含选中命题范围内的文本，完全跳过选中命题之间未被选中的无关内容，
    确保总结新命题时不会包含未选中的内容。

    Args:
        phrases: 完整短语列表
        selected_props: 选中的命题列表（会自动按start排序）

    Returns:
        (仅包含选中内容的上下文markdown文本, 总范围开始时间, 总范围结束时间)
    """
    if not selected_props:
        return "", 0.0, 0.0
    # 按开始时间排序选中的命题，计算总精确范围（所有选中命题的最早开始/最晚结束）
    sorted_props = sorted(selected_props, key=lambda x: x["start"])
    min_start = min(p["start"] for p in sorted_props)
    max_end = max(p["end"] for p in sorted_props)

    # 构建每个命题的时间范围集合，用于判断短语是否属于选中内容
    selected_ranges = [(p["start"], p["end"]) for p in sorted_props]
    # 仅提取属于任意一个选中命题范围内的短语，跳过中间未选中的内容
    selected_phrases = []
    for p in phrases:
        # 判断该短语是否在任意选中命题的范围内
        in_selected = False
        for s, e in selected_ranges:
            if p["start"] >= s - 0.1 and p["end"] <= e + 0.1:
                in_selected = True
                break
        if in_selected:
            selected_phrases.append(p)

    if not selected_phrases:
        return "", min_start, max_end

    # 拼接仅包含选中内容的文本
    lines = []
    for p in selected_phrases:
        lines.append(f"[{p['start']:06.2f}-{p['end']:06.2f}] {p['text']}")

    return "\n".join(lines), min_start, max_end


def merge_selected_propositions(
    selected_props: list[dict],
    phrases: list[dict],
    min_dur: float,
    max_dur: float,
    client: OpenAI,
    model: str,
) -> dict | None:
    """将用户选中的多个命题整合为一个新命题，复用单命题精修流程生成切片。

    相当于用户自定义了一个新的大命题：范围是选中命题的最早开始到最晚结束，
    LLM先总结这个大命题的标题/摘要/预览，然后走和普通命题完全一致的精修流程。

    Args:
        selected_props: 选中的命题列表
        phrases: 完整短语列表
        min_dur: 最小时长（秒）
        max_dur: 最大时长（秒）
        client: OpenAI客户端
        model: 模型ID

    Returns:
        精修后的clip定义（segments/title/reason），失败返回None
    """
    if len(selected_props) < 2:
        # 单个命题直接走普通精修
        return refine_single_proposition(selected_props[0], phrases, min_dur, max_dur, client, model)
    for attempt in range(2):
        try:
            context_text, total_start, total_end = build_custom_prop_context(phrases, selected_props)
            if not context_text.strip():
                return None
            # 调用LLM总结为一个新命题（仅基于选中的内容，不含未选中的无关内容）
            user_prompt = (
                f"以下是用户选中的{len(selected_props)}个相关直播片段的文本（已过滤掉未选中的无关内容），总时间范围从{total_start:.1f}秒到{total_end:.1f}秒。\n"
                f"请将这些内容整合总结为一个完整独立的命题：\n\n"
                f"{context_text}"
            )
            data = _call_llm_json(client, model, CUSTOM_PROP_SYSTEM_PROMPT, user_prompt, temperature=0.3, timeout=300, max_retries=1)
            if not data:
                if attempt == 0:
                    time.sleep(2)
                    continue
                return None
            # 构建新命题，时间范围使用精确的选中命题边界（转录文本自带的精确时间）
            new_prop = {
                "id": -1,  # 自定义命题ID为-1，不影响逻辑
                "title": str(data.get("title", f"整合{len(selected_props)}个内容")).strip(),
                "summary": str(data.get("summary", "")).strip(),
                "start": total_start,
                "end": total_end,
                "preview": str(data.get("preview", "")).strip(),
            }
            # 复用现有单命题精修流程，完全和普通命题处理逻辑一致
            return refine_single_proposition(new_prop, phrases, min_dur, max_dur, client, model)
        except Exception as e:
            if attempt == 0:
                print(f"        整合命题失败(尝试{attempt+1}/2): {e!r}，2秒后重试", file=sys.stderr)
                time.sleep(2)
                continue
            print(f"        整合命题失败，已重试1次: {e!r}", file=sys.stderr)
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
    merge: bool = False,
) -> list[dict]:
    """对用户选中的命题精修切点，支持单条精修和智能合并为一条。

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
        merge: 是否将所有选中命题智能合并为一个连贯切片

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

    # 智能合并模式：直接调用合并函数返回单条结果
    if merge:
        _log(f"  [refine] 开始智能合并 {len(selected_props)} 个选中命题…")
        if client is None:
            api_key, base_url, _model = ark_config()
            client = OpenAI(api_key=api_key, base_url=base_url, timeout=600.0)
            if model is None:
                model = _model
        if model is None:
            model = discover_model(client)
        merged_clip = merge_selected_propositions(selected_props, phrases, min_dur, max_dur, client, model)
        if on_progress:
            try:
                on_progress(1, 1)
            except Exception:
                pass
        if merged_clip:
            _log(f"  [refine] 智能合并完成：{merged_clip['title']}")
            return [merged_clip]
        else:
            _log(f"  [refine] 智能合并失败")
            if failed_props is not None:
                failed_props.append("智能合并")
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
        segs_out = _trim_opening_filler(segs_out, phrases)

        total_dur = sum(s["end"] - s["start"] for s in segs_out)
        # 所有内容统一允许最短15秒，上限允许超出20%
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


def _trim_opening_filler(segments: list[dict], phrases: list[dict]) -> list[dict]:
    """保守兜底：微调开头起点，跳过纯口癖/无意义发语词，最多后跳不超过5秒，防误伤。

    规则：
    - 所有内容都做保守的开头口癖修剪
    - 最多往后看3个短语（约3-5秒），如果前1个以上是纯口癖才调整，否则不碰
    - 调整后保证segment有意义，不切到无主语内容
    """
    if not segments:
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
        # 清理旧版本缓存中的冗余字段（category/score），保证新老数据结构一致
        cleaned_props = []
        for p in props:
            cleaned_props.append({
                "id": p.get("id"),
                "title": p.get("title", ""),
                "summary": p.get("summary", ""),
                "start": p.get("start", 0),
                "end": p.get("end", 0),
                "preview": p.get("preview", ""),
            })
        if cleaned_props:
            return cleaned_props
    except Exception:
        return None
    return None
