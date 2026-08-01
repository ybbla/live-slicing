# 智能直播切片工具 — 初步设计方案

> 一条命令从长直播 mp4 自动抽出 N 条独立的高光切片：转录 → 语义选段 → 渲染成带字幕的独立 mp4。AI 能力全部落在火山引擎（ASR 转录 + 豆包选段），本工具只产出文件、不接平台上传，事后人工审核即可。

---

## 1. 背景与目标

对长直播做精华切片长期占用大量人工，而既有"基于视频文本"的切片工具效果不佳——句中断裂、不懂语义、切完仍需大量返工。本工具的目标是做一个 **CLI 批处理工具**，从一条长直播自动抽出 **N 条独立、内容完整、逻辑连贯**的高光切片 mp4，把人工介入压缩到"事后审核"。

### 关键约束

- **AI 服务全部用火山引擎**：转录用火山 ASR（大模型录音文件识别），高光选段推理用豆包（火山方舟 Ark，OpenAI 兼容）。两者是火山上**独立的两套服务**，凭证不通用。
- **ASR 能力是最大未知**：词级时间戳、说话人分离、同步/异步形态在落地前无法确定。据此采用**探针优先**策略——先用独立探针脚本 `probe_volc_asr.py` 跑短音频、打印火山真实响应，据实编写归一化层，再落地正式转录代码。探针双路径探测：先试同步直接传字节，失败再走 TOS 上传 + 异步轮询。
- **改造基座**：从 video-use skill 改造。其 helpers 仅一处网络调用（原 ElevenLabs 转录），其余 ASR 无关逻辑可复用；渲染原本只拼成一条成片，本次改为多段出口。代码已从 `.claude/skills/video-use/` 抽出为独立 `liveslicing/` Python 包，不再嵌套在 skill 目录中，提供 CLI + 本地 Web UI 两种入口。

### 已确认的需求决策

| 维度 | 决策 |
| --- | --- |
| 交付形态 | CLI + 本地 Web UI（双击启动，浏览器操作；命令行也可用） |
| 输出 | N 条独立 mp4 + srt + manifest.json |
| 选段 | 全自动（豆包 LLM 选），事后人工审 |
| 高光信号 | 仅靠转录语义 |
| 工具范围 | 只产出文件，不接平台上传 |
| 字幕 | 默认烧录硬字幕、可 `--no-subtitles` 关；顺带出 .srt |
| 改造边界 | 直接改造 skill 原文件，不保留"对话驱动 skill"用法 |
| 多段拼接 | 一条切片可由多个不连续片段拼成，使分散的同一论点连贯 |
| 字幕样式 | 按句一条（含标点、正常大小写），不再 2 字碎块 |

---

## 2. 整体架构

```
长直播 mp4
  → transcribe (火山 ASR)   词级时间戳 + 说话人分离 → transcripts/<stem>.json
  → pack_transcripts        打包成 takes_packed.md（短语级，豆包友好）
  → pick_clips (豆包/Ark)    读 packed.md → 多片段 EDL (clips[].segments[])
  → render_clips            每条独立渲染：精确切割 + 调色 + 30ms 淡入淡出 + 按句烧硬字幕 + 响度归一
  → N 条独立 mp4 + srt + manifest.json
```

### 模块布局

```
live-slicing/
├── 启动.bat                     # 双击启动 Web UI，自动开浏览器
├── web.py                       # Web UI 启动 shim
├── cli.py                       # CLI 入口 shim
├── liveslicing/                 # Python 包（核心代码）
│   ├── __init__.py
│   ├── config.py                # 统一 .env 加载（volc_app_key, ark_config）
│   ├── cli.py                   # 流水线编排（run() 含 on_progress 回调，CLI/Web共用）
│   ├── transcribe.py            # 火山 ASR 云服务适配（默认，60min 分段）
│   ├── transcribe_qwen3.py      # Qwen3-ASR 本地开源模型适配（备用方案，无长度限制）
│   ├── transcribe_batch.py      # 批量并行转录（4 worker）
│   ├── pack_transcripts.py      # 打包 takes_packed.md
│   ├── pick_clips.py            # 豆包选段（支持 auto count）+ vision 自评
│   ├── render.py                # 多片段渲染（concat + 字幕 + 响度归一）
│   ├── grade.py                 # 调色（auto/none/light/warm_cinematic）
│   └── timeline_view.py         # QC 可视化（胶片条 + 波形，跨平台 CJK 字体）
├── web/                         # Flask Web UI
│   ├── app.py                   # 路由（上传/启动/状态/历史/预览/下载/打开目录）
│   ├── job.py                   # 后台任务管理 + stdout tee + 进度聚合
│   └── templates/index.html     # 前端（vanilla JS，无构建）
├── data/                        # 运行时数据（自动创建）
│   ├── uploads/                 # Web端上传的原视频存储
│   └── output/                  # 所有切片结果统一输出根目录，按视频名分子目录
└── scripts/                     # 一次性探针脚本（开发用）
    └── probe_*.py
```

### 依赖关系

所有源码在 `liveslicing/` 包下，通过标准包导入（`from liveslicing.xxx import ...`）。`transcribe.py` 是唯一写 `transcripts/<stem>.json` 的模块，定义并保证 `words[]` + `utterances[]` 契约；`pack_transcripts.py` 只读 `words[]`；`render.py` 的 `build_master_srt` 读 `utterances[]`；`grade.py` 与转录管线解耦，直接读视频像素；`timeline_view.py` 只读 `words[]` 做可视化阴影。`config.py` 统一 .env 查找（cwd → 项目根）和加载。`web/job.py` 在后台线程运行 `liveslicing.cli.run()`，通过 `on_progress` 回调更新状态，通过 Tee writer 捕获 stdout 到日志环形缓冲。

---

## 3. 阶段一：转录（火山 ASR 极速版）

**职责**：抽音 → 单次 HTTP flash 识别 → 归一化为 `words[]` + `utterances[]` 契约 → 落盘缓存。

### 关键常量

- `FLASH_URL = "https://openspeech.bytedance.com/api/v3/auc/bigmodel/recognize/flash"`
- `RESOURCE_ID = "volc.bigasr.auc_turbo"`（极速版）
- `CODE_OK = "20000000"`（成功状态码，位于响应头 `X-Api-Status-Code`）
- `DEFAULT_MAX_CHUNK_MINUTES = 60`（单段上限，实测极速版单请求 56min/102.5MB wav 稳过、2h/219.7MB 撞 HTTP 413 网关 payload 墙；文档「≤2h/≤100MB」中 100MB 是软建议。安全单段取 60min，POST~150MB 离 413 阈值有余量）

### 音频预处理

`extract_audio` 用 ffmpeg 抽成单声道 16kHz 16-bit PCM wav（`-vn -ac 1 -ar 16000 -c:a pcm_s16le`）。分段时用 `-ss <start> -t <seg>` 从视频直接切片再编码。

### 请求契约（`call_volcengine`）

Header：`X-Api-Key`、`X-Api-Resource-Id`、`X-Api-Request-Id`(uuid4)、`X-Api-Sequence:"-1"`、`Content-Type`。

Body：

```json
{ "user": {"uid": "<app_key>"},
  "audio": {"data": "<wav base64>"},
  "request": {
    "model_name": "bigmodel",
    "enable_itn": true,          // 逆文本归一化
    "enable_punc": true,         // 标点
    "show_utterances": true,     // 返回 utterances + words 词级时间戳
    "enable_speaker_info": true  // 说话人分离
  }}
```

`timeout=1800`。成功判定：响应头 `X-Api-Status-Code == "20000000"`，否则 raise。

### 长视频分段机制（`transcribe_one`）

`ffprobe` 取时长。不超单段上限（默认 60 分钟）则一段直送；否则按时长切成 N 段，每段独立 `-ss/-t` 抽音 + `call_volcengine`，结果带 `time_offset_s` 偏移拼回绝对时间，最后按起点排序。

> ASR 的物理分段（60 分钟）与选段分块（`--chunk-minutes`）无关。火山极速版单请求实测 56min/102.5MB wav（POST base64 ~140MB）稳过、2h/219.7MB（POST ~301MB）撞 HTTP 413 网关 payload 墙；文档「≤2h/≤100MB」中 100MB 是软建议（102.5MB 照样过），真实硬墙在 POST 140~301MB 之间。安全单段取 60min（POST~150MB 离 413 阈值有余量）。ASR 自动按 60 分钟分段转录，结果拼回一份完整、连续、带全局时间戳的转录，不影响关联。段数大减（1h 直播单段、2h 仅 2 段）顺带让说话人跨段 ID 不一致问题几乎不再出现。

### 归一化契约（`normalize_response`）

火山返回 `result.utterances[]`，每条含 `{text, start_time, end_time(毫秒), additions:{speaker}, words:[...]}`。输出保留原 ElevenLabs Scribe 契约（下游 pack/render/timeline 不变）：

- **words[]**：扁平词级，每条 `{type:"word", text, start(秒), end(秒), speaker_id}`
- **utterances[]**：句级 `{text(保留火山原标点), start, end, speaker_id}`，供 `build_master_srt` 按句字幕

要点：时间戳毫秒÷1000 转秒，异常回落 0.0；说话人优先取 `additions.speaker`，回落到 utterance 内的别名，规范化为 `speaker_<N>`，无则 `None`；若 utterance 无 `words[]`，整句当一条 word；`time_offset_s` 加到每个 start/end（分段拼接用）。

### spacing 合成（`synthesize_spacing`）

在相邻 word 间若间隔 ≥0.05s，插入 `{type:"spacing", text:"", start:prev.end, end:next.start, speaker_id:None}`。让 pack 的静音检测稳定工作（pack 也有 fallback 直接看 word-word gap）。

### 缓存

`<edit_dir>/transcripts/<video_stem>.json` 已存在则直接返回路径、跳过调用。

---

## 4. 阶段二：打包

**职责**：把 `transcripts/*.json` 的词级 `words[]` 聚合成 phrase 级行，输出 `takes_packed.md`（编辑决策读的主产物，token 仅为原始 JSON 的 ~1/10）。

### phrase 分组规则（`group_into_phrases`，`silence_threshold=0.5`）

遍历 words，遇以下任一即 `flush()` 当前缓冲成一条 phrase：

1. `type == "spacing"` 且该 spacing 的 `end-start >= silence_threshold`
2. 上一 kept token 的 end 到当前 start 的 gap `>= silence_threshold`（fallback，防 spacing 缺失）
3. 说话人切换

flush 时保留 `word`/`audio_event`（后者自动加括号），以空格 join 后修正标点空格。phrase = `{start, end, text, speaker_id}`。

### 输出 markdown 格式

```
# Packed transcripts
Phrase-level, grouped on silences ≥ 0.5s or speaker change.

## <stem>  (duration: ..., N phrases)
  [012.34-045.67] S0 这是phrase文本
```

说话人标签 `speaker_N` 去前缀显示为 ` S0`；空 phrase → `_no speech detected_`。

---

## 5. 阶段三：选段（豆包）

**职责**：读 `takes_packed.md`，调火山方舟豆包 LLM 选 N 条高吸引力片段，吸附到短语边界保证不句中切断，输出多片段 EDL `clips/edl_multi.json`。

### 配置与模型探测

`load_ark_config()` 从 `.env`（先 skill 目录后 cwd）+ 环境变量读 `ARK_API_KEY`/`ARK_BASE_URL`/`ARK_MODEL`。`ARK_MODEL` 留空时 `discover_model(client)` 调 `client.models.list()`，按评分函数排序（含 `doubao` +10，含 `1.5/128k/256k/32k/pro/vision/thinking` 各 +3），取最高分。

### SYSTEM_PROMPT — 选段标准与切点工艺

选段标准（按重要性）：

1. 金句 / 结论性观点 / 干货总结
2. 冲突 / 反转 / 悬念 / 情绪高点
3. 内容完整可独立成段，不选半截话

切点工艺：

- 相邻短语间隔代表静音。优先把切点落在间隔 **≥400ms** 的静音处（最干净）；**150–400ms** 可用；**<150ms** 避免（可能句中）
- `(laughs)/(applause)/(sighs)` 等括号标记是音频事件=情绪高点，优先保留，末尾往后延以包含反应
- 多段拼接优先同说话人、段间有静音处切，拼接更自然

一条切片 = 1 个或多个 segment 拼成：连续论点用 1 段；分散在不连续处用多段拼成同一条（使成片逻辑连贯）。

硬约束：

- 每个 segment 的 start/end 必须落在文本里出现的 `[start-end]` 边界上，不得造时间戳
- 同一条切片多个 segment 不互相重叠、按时间顺序
- 每条切片总时长 ∈ [MIN_DUR, MAX_DUR]
- 不同切片之间不重叠
- 选够 N 条（素材不足可少选，不凑数）

### 边界吸附（`snap_to_phrases`，容差 0.5s）

起点取第一个 `p["start"] >= start - 0.5` 的短语起点；终点取最后一个 `p["end"] <= end + 0.5` 的短语终点。若 snap_end <= snap_start 返回 None。

### 调用参数（`call_doubao`）

`response_format={"type":"json_object"}`、`temperature=0.4`。输出 `{"clips":[{"segments":[{"start":..,"end":..}], "title":.., "reason":..}]}`。`safe_json_loads` 容错解析（去 markdown 围栏，抓第一个 `{...}`）。JSON 失败兜底重试一次，system 追加"务必只输出纯 JSON"，temperature 降到 0.2，并把上次输出作为 assistant 消息回灌。

### 合并去重（`merge_and_snap`）

1. 按 clip 第一段起点排序
2. 兼容旧格式 `{start,end}` → 转 `segments:[{start,end}]`
3. 每个 segment `snap_to_phrases` 吸附；吸附失败或 `e<=s` 丢弃；保留两位小数
4. clip 内 segment 按起点排序，重叠段取较宽（`end = max(end, prev.end)`）
5. 时长约束：`total_dur` 不在 [min_dur, max_dur] 丢弃
6. clip 间去重：与已选 clip 的重叠 >0 且 **≥ 50% 较短者时长** → 跳过
7. 达到 `count` 条 break

### chunk_minutes 分块降级

默认 `0=不分块`，整份 packed.md 一次调豆包，关联完整性最好（豆包 256k 上下文能装几十小时文本）。`chunk_minutes>0` 时超长流降级，按时长分块逐块调豆包再合并（**跨块关联会丢失**）。窗口按 `phrases[-1]["end"]` 计算总时长，每个窗口重格式化短语为 `[start-end] text` 喂豆包。

### vision 自评质检

`self_eval_clip(clip_path, segments, edit_dir, preview=False)` 仅对**多段拼接的内部切点**质检（单段无内部切点）。成片时间码累加：`internal_cutpoints` = 除最后一段外各段时长累加。每个内部切点 ±1.5s 窗口，调 `timeline_view.render_timeline(clip, win_s, win_e, png, 10, None)` 生成胶片条+波形合成 PNG，存到 `<clip_dir>/verify/`。

`_vision_judge(client, png)` 调豆包 vision 模型（`doubao-seed-1-6-vision-250815`），PNG base64 内联，`response_format=json_object`、`temperature=0.1`、`max_tokens=200`。`EVAL_PROMPT` 检查三类问题：1) 画面跳变/闪烁/突兀切换；2) 波形尖峰（可能爆音，30ms 淡变未消住）；3) 字幕被遮挡或错位。输出 `{"ok": bool, "issues": [str]}`。问题用"；"拼接截断 300 字符返回，无问题返回 None。

> **依赖与降级**：vision 自评依赖 Ark key 能用某个 vision 模型读 PNG。实施前先探测可用 vision 模型（试 doubao-vision-pro / doubao-1.5-vision 等）。若无任何 vision 权限 → 降级为「只出 PNG 供人工看，不自动判」，不进入重渲循环。`import timeline_view` 失败也直接返回 None 降级。

---

## 6. 阶段四：渲染

**职责**：实现"分段提取（grade + 30ms 淡变）→ 无损拼接 → overlay（PTS 偏移）+ 字幕最后烧录 → loudnorm"的成片流水线。支持单条 EDL（`main`）和多 clip 批量（`render_clips`）。

### 关键常量

- `SUB_FORCE_STYLE`：`FontName=Helvetica,FontSize=18,Bold=1,PrimaryColour=&H00FFFFFF,OutlineColour=&H00000000,BackColour=&H00000000,BorderStyle=1,Outline=2,Shadow=0,Alignment=2,MarginV=90`。MarginV=90 是平台安全区规则（竖屏底部 25–30% 被 UI 遮挡），勿低于 75
- `HDR_TRANSFERS = {"smpte2084", "arib-std-b67"}`（PQ/HDR10 与 HLG）
- `TONEMAP_CHAIN`：`zscale=t=linear:npl=100,format=gbrpf32le,zscale=p=bt709,tonemap=tonemap=hable:desat=0,zscale=t=bt709:m=bt709:r=tv,format=yuv420p`
- `LOUDNORM_I=-14.0`、`LOUDNORM_TP=-1.0`、`LOUDNORM_LRA=11.0`（社媒标准）
- 质量阶梯：final=`fast/CRF20`，preview=`medium/CRF22`，draft=`ultrafast/CRF28`

### 单段提取（`extract_segment`）

`-ss <seg_start>` 在 `-i` 之前（快速精确 seek），`-t <duration>`。vf 链顺序：`[TONEMAP_CHAIN(若HDR)] + scale + grade_filter`。scale：竖屏（`is_portrait_source`，h>w）按高缩放，其余按宽缩放到 1080p。

30ms 音频淡变（两端，防爆音）：`afade=t=in:st=0:d=0.03,afade=t=out:st={duration-0.03:.3f}:d=0.03`。

编码：`libx264 -preset <fast/medium/ultrafast> -crf <20/22/28> -pix_fmt yuv420p -r 24 -c:a aac -b:a 192k -ar 48000 -movflags +faststart`。

### 调色（`grade.py`）

两种模式：preset（固定滤镜）和 auto（默认，按片分析后输出有界的温和修正）。

**Presets**：`subtle`=`eq=contrast=1.03:saturation=0.98`（基线清理）；`neutral_punch`=对比+1.06+S曲线；`warm_cinematic`=创意预设（+12%对比、压黑、-12%饱和、暖阴影冷高光、filmic曲线）；`none`=`""`。

**auto_grade_for_clip**：`_sample_frame_stats` 用 ffmpeg `fps=<clamp>,signalstats,metadata=print` 抽 N 帧，从 stderr 解析亮度/饱和度统计并归一化。决策规则（目标"干净但不像调过色"，所有轴硬限 ±8%）：对比目标 y_range≈0.72，gamma 目标 y_mean≈0.48，饱和默认 0.98 轻微下拉。组装 `eq=contrast=X:gamma=Y:saturation=Z`，差值≤0.005 省略该项。

### 无损拼接（`concat_segments`）

1. 写 `_concat.txt`（Windows 路径转正斜杠、单引号包裹）
2. 先试 `ffmpeg -f concat -safe 0 -i _concat.txt -c copy -movflags +faststart`（无损）
3. 失败回退 concat filter 重编码：每段 `-i`，`[i:v][i:a]concat=n=N:v=1:a=1[v][a]`，`libx264 fast CRF20 yuv420p aac 192k 48k`
4. 删除 `_concat.txt`

> 各段是独立抽切+调色的，编码参数/时间基可能有微小差异，`-c copy` 偶尔失败。回退 concat filter 对参数不一致免疫，安全可靠。

### 字幕生成（`build_master_srt`）

读 `edit_dir/transcripts/<src_name>.json`。**seg_offset 累加**：遍历 `edl["ranges"]`，每段 `seg_offset += seg_duration`；该段 cue 时间 = `word.start - seg_start + seg_offset`（成片时间码从 0 起）。

优先用 `utterances`（句子级，火山带标点）→ **一句一条 cue**，文本直接用 `utterance.text`（去 `.upper()`、保留原大小写）。长句用 `_split_sentence(text, max_len=30)` 拆分（按 `.,!?;:` 等标点，短句合并到 30 字，超长硬切）；一句拆多条时按时长均匀分配。无 utterances 回退 word 级 2 词一块。

输出 `utf-8-sig` 编码 SRT。

### 响度归一（`apply_loudnorm_two_pass`）

preview：1-pass 近似，`loudnorm=I=-14:TP=-1:LRA=11`，`-c:v copy`，重编码音频。final：2-pass——pass1 `measure_loudness`（`-vn -f null -`，从 stderr 解析 JSON 取各项测量值）；pass2 用 `measured_*` 参数 + `linear=true`，`-c:v copy`。测量失败回退 1-pass。

### 最终合成（`build_final_composite`）

无 overlay 无字幕 → 直接 `-c copy`。overlay：每路 `-i`，`[idx:v]setpts=PTS-STARTPTS+{t}/TB[a{idx}]` 做 PTS 偏移（让 overlay 帧 0 落在 `start_in_output`），逐路 `overlay=enable='between(t,{t},{t+dur})'` 串接。**字幕最后**烧录（Rule 1）：`subtitles='<abs>':charenc=UTF-8:force_style='<SUB_FORCE_STYLE>'`，Windows 下路径 `\`→`/`、`:`→`\:`、`'`→`\'` 转义。编码 `libx264 fast CRF18 yuv420p -c:a copy`。

### 多片段出口（`render_clips`，核心）

`render_clips(edl, edit_dir, out_dir, *, subtitles=True, preview=False, self_eval=True, _self_eval_fn=None) -> (final_paths, qc_flags)`。支持 v3 `clips[].segments` 与旧 `ranges[]`（每个 range 当单段 clip）。

每个 clip 的处理流程：

1. **构造子 EDL**：`ranges = padded_ranges`。padding（video-use Hard Rule 7）`PAD_BEFORE=0.05`(50ms) / `PAD_AFTER=0.08`(80ms)，吸收 ASR 时间戳 50–100ms 漂移，防切掉词头词尾（padding 在短语吸附 Hard Rule 6 之后施加，词边界仍被尊重；`ee` 被 `stream_dur` 上限钳制）。另存 `_unpadded_segments` 供自评用
2. **逐段提取**：`extract_all_segments(sub_edl)`，每段 grade + 30ms 淡变
3. **拼接**：`concat_segments` 把该 clip 的多段拼成 `clip_NNN_base.mp4`；单段直接 rename 不 concat
4. **字幕**：`build_master_srt(sub_edl, edit_dir, clip_NNN.srt)`，per-clip 字幕，seg_offset 从 0 累加。无 transcript 则跳过字幕
5. **烧字幕**：`build_final_composite(base, [], srt, clip_NNN_sub.mp4)`（无 overlay）
6. **响度归一**：`apply_loudnorm_two_pass(sub_base, clip_NNN.mp4)`，失败则 `-c copy` 兜底
7. **清理**：删除中间产物（`seg_paths`、`base`、`subbed`、`.prenorm.mp4`），保留 `clip_NNN.mp4` + `clip_NNN.srt`
8. **自评**（若 `self_eval and render_timeline and _self_eval_fn`）：调 `_self_eval_fn(final, _unpadded_segments, edit_dir, preview)`，有问题打印 `⚠ qc:`

> **padding 与 30ms 淡变是两套不同机制**：30ms 音频淡变（Hard Rule 3）防切点 click 爆音；padding 防 ASR 时间戳漂移吞词。两者叠加才完整。

---

## 7. 多段拼接完整链路（豆包决策 → 最终 mp4）

1. **决策层**：豆包 SYSTEM_PROMPT 允许一条 clip 由多个不连续 segment 拼成。输出 `clips[].segments[{start,end}]`，时间戳须落在文本边界
2. **吸附层**（`merge_and_snap`）：每个 segment `snap_to_phrases`（±0.5s 容差）吸附到短语边界；clip 内重叠段取较宽；clip 间重叠 ≥50% 较短者去重；时长约束 [min,max]；限 count 条
3. **契约层**：`edl_multi.json` v3 持久化 `clips[].segments`
4. **渲染层**（`render_clips`）：对每条 clip 构造子 EDL，`ranges = padded_ranges`（50ms/80ms padding 吸收 ASR 漂移，保留 `_unpadded_segments` 供自评）
5. **提取**（`extract_segment`）：每段独立 `-ss/-t` 提取，HDR 色调映射 + 1080p scale + grade + 30ms 音频淡变，libx264 编码
6. **拼接**（`concat_segments`）：多段 → 无损 `-c copy concat` 优先，失败回退 concat filter 重编码；单段直接 rename
7. **字幕**（`build_master_srt` + `build_final_composite`）：per-clip SRT，`seg_offset` 从 0 累加使成片时间码从 0 起；一句一 cue，长句拆分；字幕最后烧录（libass force_style）
8. **响度**（`apply_loudnorm_two_pass`）：-14 LUFS / -1 dBTP / LRA 11，2-pass 线性归一化
9. **自评**（`self_eval_clip`）：仅多段 clip 的内部拼接处 ±1.5s 出 timeline_view PNG，豆包 vision 模型判跳变/爆音/字幕遮挡，问题写入 manifest.qc_flag
10. **清理**：中间产物删除，保留 `clip_NNN.mp4` + `clip_NNN.srt`

---

## 8. 关键数据契约

### transcript JSON（`transcripts/<stem>.json`）

```json
{
  "words": [
    {"type": "word", "text": "好", "start": 0.0, "end": 0.2, "speaker_id": "speaker_0"},
    {"type": "spacing", "text": "", "start": 0.2, "end": 0.6, "speaker_id": null}
  ],
  "utterances": [
    {"text": "好家人们好。", "start": 0.0, "end": 1.2, "speaker_id": "speaker_0"}
  ]
}
```

### EDL（`clips/edl_multi.json`，v3）

```json
{
  "version": 3,
  "mode": "multi_clip",
  "sources": {"<video.stem>": "<abs path>"},
  "clips": [
    {"source": "<stem>",
     "segments": [{"start": 12.34, "end": 20.00}, {"start": 35.50, "end": 48.00}],
     "title": "...", "reason": "..."}
  ],
  "grade": "auto",
  "overlays": [],
  "subtitles": null,
  "total_duration_s": 60.16
}
```

### manifest（`clips/manifest.json`）

```json
{
  "video": "<abs path>",
  "clips": [
    {"index": 1,
     "segments": [{"start": 12.34, "end": 20.00}],
     "duration": 20.00,
     "title": "...", "reason": "...",
     "file": "clip_001.mp4",
     "srt": "clip_001.srt",
     "qc_flag": null}
  ]
}
```

---

## 9. 关键常量与阈值速查表

| 值 | 位置 | 用途 |
| --- | --- | --- |
| 0.5s | pack `silence_threshold` | phrase 分组断句 |
| 0.4s | timeline_view `find_silences` | QC 可视化静音阴影 |
| 0.05s | `synthesize_spacing` | 决定是否插入 spacing 条目（非断句） |
| 50ms / 80ms | `render_clips` PAD_BEFORE/AFTER | 吸收 ASR 时间戳漂移（切点 padding） |
| 30ms | `extract_segment` afade | 段边音频淡入淡出，防爆音 |
| ±0.5s | `snap_to_phrases` 容差 | 候选时间戳吸附短语边界 |
| ≥400ms / 150–400ms / <150ms | SYSTEM_PROMPT | 切点静音分级工艺 |
| ±1.5s | vision 自评窗口 | 内部切点质检窗口 |
| -14 LUFS / -1 dBTP / LRA 11 | loudnorm | 社媒响度归一标准 |
| ±8% 硬限 | grade auto | 调色修正幅度上限 |
| 60 分钟 | ASR 分段 | 实测极速版单请求 56min/102.5MB 稳过、2h 撞 HTTP 413；安全单段取 60min |
| ≤100MB wav（软） | ASR 单请求 | 文档建议值，实测 102.5MB 照样过；真实硬墙在 POST 140~301MB 间 |

---

## 10. 续跑与缓存机制

### from_stage 递进守卫

`--from-stage` choices=`["all","transcribe","pack","select"]`。每个阶段的守卫是 `if from_stage in (...)`，stage 列表递增：

- `all` → 触发所有阶段
- `transcribe` → 跳过转录，跑 pack+select+render
- `pack` → 跳过转录+打包，跑 select+render
- `select` → 只跑 select+render（render 总是跑）

### 缓存

ASR 转录缓存：`transcripts/<stem>.json` 已存在则跳过调用，改选段参数免费重跑选段，无需重转 ASR（省 ASR 费用）。

---

## 11. 使用方式

### 前置依赖

- **Python 3.10+**
- **ffmpeg + ffprobe**（在 PATH 中），Windows 可通过 `winget install --id Gyan.FFmpeg -e` 安装
- **火山引擎账号**，开通两个独立服务：
  - 大模型录音文件识别（极速版）→ 控制台拿 APP Key
  - 方舟 Ark → 控制台拿 API Key，并开通一个豆包模型（如 `doubao-seed-2-1-pro-260628`）

### 安装

```bash
pip install openai requests
```

> libsora/matplotlib/pillow/numpy 仅调色等高级功能需要，基础流程无需。

### 配置

在项目根目录 `.env` 填写凭证：

```
VOLC_APP_KEY=你的ASR_APP_Key
ARK_API_KEY=你的方舟API_Key
ARK_BASE_URL=https://ark.cn-beijing.volces.com/api/v3
ARK_MODEL=doubao-seed-2-1-pro-260628
```

`ARK_MODEL` 可留空，首次运行自动探测可用模型（但建议显式指定，避免选到不合适的）。

### 常用命令

```bash
# 完整流程：默认 8 条、每条 15-90 秒
python cli.py 你的直播.mp4

# 指定条数和时长
python cli.py 直播.mp4 --count 6 --min-duration 15 --max-duration 60

# 快速预览（低质量，先看选段效果）
python cli.py 直播.mp4 --count 5 --preview

# 不烧字幕（只出干净视频 + srt）
python cli.py 直播.mp4 --no-subtitles

# 复用转录缓存续跑（省 ASR 费用）
python cli.py 直播.mp4 --from-stage pack
```

### 参数说明

| 参数 | 默认 | 说明 |
| --- | --- | --- |
| `--count` | 8 | 目标切片条数 |
| `--min-duration` | 15 | 单条切片最短秒数 |
| `--max-duration` | 90 | 单条切片最长秒数（多段拼接时指总时长） |
| `--chunk-minutes` | 0 | 选段分块分钟数；**0=不分块**（默认，整份一次调豆包，跨任意位置的关联片段都能拼），>0=超长流降级分块（跨块关联会丢失）。ASR 物理分段不受此参数影响，固定按 60 分钟分。 |
| `--grade` | auto | 调色：auto / none / subtle / warm_cinematic 等 |
| `--no-subtitles` | off | 不烧录硬字幕 |
| `--preview` | off | 快速低质量渲染（QC 用） |
| `--from-stage` | all | 从某步续跑：`pack` / `select` |

---

## 12. 输出形式

```
<视频目录>/edit/
  transcripts/<视频名>.json     # 词级+句级转录（words[] + utterances[]）
  takes_packed.md                # 打包文本（豆包读这个选段，短语级，token ~1/10 原始JSON）
  clips/
    edl_multi.json               # 选段决策（v3结构，clips[].segments[]）
    clip_001.mp4  clip_001.srt   # 切片 + 对应字幕
    clip_002.mp4  clip_002.srt
    ...
    manifest.json                # 清单（标题/理由/时间码/段结构/文件/qc_flag）
    verify/                      # vision自评胶片条+波形PNG（多段切片才有）
```

`manifest.json` 每条记录包含：切片序号、分段时间码列表、总时长、豆包生成的标题与选段理由、对应 mp4/srt 文件名、vision 质检 qc_flag（无问题为 null）。

---

## 13. 费用

- 火山 ASR 按音频时长计费（约几元/小时）。转录结果有缓存，重跑不重转。
- 豆包推理按 token 计费，整份一次调用，量很小。

---

## 14. 明确排除（已确认不做）

对照 video-use 原版全能力清单逐项核查后，以下能力**确认不补**：

| 排除项 | 理由 |
| --- | --- |
| filler 去除 | video-use Hard Rule 8 明令禁止（"never normalized fillers, loses editorial signal"），是反模式——filler 本身承载编辑信号，去除会失真 |
| project.md 会话记忆 | CLI 自动化场景价值低，manifest.json 已记录切片元数据（标题/理由/段结构/qc_flag），无需额外会话记忆 |
| 动画 overlay | 切片场景不需要，但合成代码（`build_final_composite` 的 PTS 偏移 + overlay 串接）已就绪备用，未删除 |

---

## 15. 风险与回退

| 风险 | 处置 |
| --- | --- |
| 火山 ASR 词级/分离能力未知（最大） | 探针脚本先验证；仅句级时 pack 仍可工作（`prev_end` 间隔检测无 spacing 也能跑），切点落句边界满足"完整连贯"，`build_master_srt` 降级每句一条可用；Phase 2 加 forced-aligner 补词边界 |
| 火山 ASR 异步 submit+poll | 探针确认后适配器实现轮询并回显进度 |
| Ark 豆包 key/model 未知 | `ARK_MODEL` 留空首运行 `models.list()` 探测 |
| Ark JSON 模式是否支持 | `json_object` + 剥围栏 `safe_json_loads` fallback |
| Ark key 与 ASR 凭证两套 | 实施前确认两个服务都开通 |
| 超长流 ASR 计费 + LLM 上下文 | 选段分块降级（`--chunk-minutes`）；ASR 缓存避免重转 |
| ffmpeg PATH 当前会话未刷新 | 新开终端生效；README 注明 |
| vision 自评无权限 | 降级为只出 PNG 供人工看，不自动判、不重渲 |
| concat 无损失败 | 自动回退 concat filter 重编码 |

---

## 16. 规划中（暂未实现）

- **敏感内容屏蔽**：直播常出现价格、数字、联系方式等敏感信息。计划支持「音频消音 + 字幕不提」或「直接剪掉」两种模式，敏感词来源为词表 + 豆包双重检测取并集。当前未实现，待后续需求明确后启动。

---

## 17. 演进历史

- **Phase 1 MVP**：探针验证 ASR → 改 transcribe（`call_scribe`→`call_volcengine`，`.env` 键改火山）→ 新增 select（豆包，单次调用不分行，短语边界吸附）→ `render_clips`（每段独立切+调色+30ms 淡变+烧字幕，跳过 concat）→ cli 单视频跑通验收。起源："一个 key"未知，探针优先定契约。
- **Phase 1.5 质量改造**：①字幕按句一条 + 正常大小写——根因是火山 `words[].text` 是单字无标点而分句标点只在 `utterance.text`，旧 `build_master_srt` 按 2 词硬切块 + `.upper()` 致中文每块仅 2 字且全大写、视觉断裂；修法是 `normalize_response` 保留 utterances，`build_master_srt` 优先用 utterance 去 upper、长句按词拆保护。②一条切片内拼多段——EDL 改 `clips[].segments[]` 分组，prompt 引导拼接，`render_clips` 按 clip 走 sub-EDL 拼接流程（复用原 `render.main` 多 range 拼一条的完整流程，`seg_offset` 累加现成可用）。
- **Phase 1.6 质量增强**：①切点 padding（Hard Rule 7，50/80ms 吸收 ASR 漂移，与 Hard Rule 3 的 30ms 淡变防 click 是不同机制）；②cut-craft prompt 增强（静音 ≥400ms/150–400ms/<150ms 分级、音频事件 beat 信号、多段拼接同说话人）；③vision 自评闭环（内部切点 ±1.5s 出 timeline_view PNG → vision 判跳变/爆音/字幕遮挡 → manifest.qc_flag）。
- **Phase 1.7 ASR 分段上调（实测驱动）**：递增时长探针实测极速版 flash 单请求真实上限——56min/102.5MB wav（POST base64 ~140MB）稳过、2h/219.7MB（POST ~301MB）撞 HTTP 413 网关 payload 墙（非 ASR 业务拒，`X-Api-Status-Code` 为空）。修正文档「≤100MB」是软建议（102.5MB 照样过）而非硬红线、真实文件硬墙在 POST 140~301MB 间。据此将 `DEFAULT_MAX_CHUNK_MINUTES` 从 10 调到 60（POST~150MB 离 413 阈值有余量），删掉 `transcribe_one` 里 `min(max_chunk_minutes, 10)` 的旧 10 分钟硬保护，cli 调用去掉 `max_chunk_minutes=10` 硬编码。1h 直播单段、2h 仅 2 段，段数大减顺带根除「说话人跨段 ID 不一致」问题（单段无跨段）。评估过改用标准版异步（≤5h）但否决：标准版内置语义顺滑可能改写 verbatim 转录、与 cut-craft（Hard Rule 8 禁 normalize fillers）冲突，且需写 submit+query+TOS 适配器，对 ≤2h 素材零收益。探针脚本 `probe_flash_limit.py`/`probe_flash_2h.py` 保留作回归工具。
- **Phase 1.8 产品化增强**：
  1. **双ASR引擎支持**：新增`transcribe_qwen3.py`模块，接入Qwen3-ASR开源本地模型，接口与火山版完全一致可无缝替换，支持离线转录、无长度限制、方言/BGM场景、隐私安全，满足不同用户需求。
  2. **统一输出目录与历史版本管理**：CLI和Web UI统一输出到`data/output/<视频名>/`，结果使用`clips_YYYYMMDD_HHMMSS/`时间戳目录存储，通过Windows目录联接（或Linux/macOS软链接）`clips`永久指向最新版本，所有历史重剪结果自动保留不覆盖。
  3. **重剪功能**：manifest新增`config`字段保存完整任务参数，支持历史任务一键重剪，自动加载原视频和参数，复用转录/打包缓存，15分钟视频重剪仅需1-2分钟出结果。
  4. **跨任务缓存复用**：新任务自动扫描同视频其他任务目录，复制已有的转录和打包结果，避免重复转录浪费时间和API费用。
  5. **参数统一与调色扩展**：CLI参数`--count`统一为`--num-clips`与Web端保持一致；调色预设新增`light`轻度增强模式，满足不同风格需求。
  6. **Web UI功能完善**：新增历史任务列表、删除任务、获取历史配置、打开目录等接口，路径遍历安全防护，支持所有ffmpeg可解码的常见视频格式。
- **后续规划**：Phase 2 目录批量处理、超长流分块+重叠合并、本地ASR说话人分离（接入pyannote.audio）、敏感内容屏蔽、人工审核HTML界面。
