# 智能直播切片工具

从长直播视频自动抽出 N 条高光切片，事后审核即可。双击 `启动.bat` 打开本地网页，选视频点按钮即可；技术用户也可以用命令行一键跑完整流水线。

```
长直播 mp4
  → 火山 ASR 转录（词级时间戳 + 说话人分离）
  → 打包成 takes_packed.md（短语级，豆包友好）
  → 豆包 LLM 选高光片段（按内容密度自定条数，支持多段拼接，使分散的同一论点连贯）
  → 每条独立渲染：精确切割 + 自动调色 + 30ms 淡入淡出（无爆音）+ 按句烧录硬字幕（可选）+ -14 LUFS 响度归一 + vision QC
  → N 条独立 mp4 + srt + manifest.json
```

---

## 快速开始（非技术用户）

1. **安装依赖**（一次性）：安装 Python 3.10+、ffmpeg，然后：
   ```
   pip install -r requirements.txt
   ```
2. **配置凭证**：把 `.env.example` 复制为 `.env`，填入火山引擎的两个 Key（ASR APP Key 和方舟 API Key）。
3. **启动**：双击 `启动.bat`，浏览器会自动打开 http://localhost:5876。
4. **使用**：在网页上选择视频 → 勾选是否烧录字幕 → 点"开始切片"。等待进度条走完，结果页可直接预览、下载、打开文件夹。

关闭黑色命令行窗口即停止服务。

---

## 命令行用法（技术用户）

```bash
# 智能默认：豆包自动决定切几条（推荐），30s-5min/条，auto 调色，不烧字幕
python cli.py 你的直播.mp4

# 指定条数和时长
python cli.py 直播.mp4 --count 6 --min-duration 20 --max-duration 60

# 烧录硬字幕
python cli.py 直播.mp4 --subtitles

# 复用转录缓存续跑（省 ASR 费用）
python cli.py 直播.mp4 --from-stage select
```

### 命令行参数

| 参数 | 默认 | 说明 |
| --- | --- | --- |
| `--count N` | 0（自动） | 目标切片条数；0 = 豆包按内容密度自定（每小时约 5-8 条） |
| `--min-duration S` | 30 | 单条切片最短秒数 |
| `--max-duration S` | 300（5分钟） | 单条切片最长秒数 |
| `--chunk-minutes N` | 0（不分块） | 选段分块分钟数；0 = 整份一次给豆包（跨任意位置关联片段都能拼） |
| `--grade preset` | auto | 调色：auto / none / subtle / warm_cinematic 等 |
| `--subtitles` | off | 烧录硬字幕（不加此参数则只生成独立 .srt 文件） |
| `--preview` | off | 快速低质量渲染（QC/调试用，跳过 vision 自评） |
| `--from-stage STAGE` | all | 从某步续跑：`transcribe` / `pack` / `select` |

### 智能默认说明

非技术用户通过 Web UI 启动时，所有参数使用智能默认：
- 条数：豆包根据视频长度和内容密度自主判断（1 小时约 5-8 条，3-16 条之间，不凑数、不拆论点）
- 时长：30秒 – 5分钟
- 调色：auto（采样帧信号做 ±8% 以内的微调，让画面干净但看不出调过色）
- 字幕：默认不烧，生成独立 .srt 文件；用户可勾选烧录
- 渲染质量：原画质正式成品，带豆包 vision 切点 QC 质检

### 关于 `--chunk-minutes`

豆包 256k 上下文能装几十小时直播文本，**正常直播无需分块**。默认 `0` 表示整份一次调豆包——跨任意位置的关联片段都能拼成一条（关联完整性最好）。仅当直播极长（几十小时）文本超窗口时，传 `--chunk-minutes 30` 降级分块（跨块关联会丢失）。

> 注意：ASR 转录的物理分段不受此参数影响。火山极速版单请求实测 56min/102.5MB 稳过、2h/219.7MB 撞 HTTP 413（网关 payload 墙）；安全单段取 60 分钟。ASR 自动按 60 分钟分段转录，但结果拼回**一份完整、连续、带全局时间戳**的转录，不影响关联。

---

## 前置依赖

- **Python 3.10+**
- **ffmpeg + ffprobe**（在 PATH 中）
  ```powershell
  winget install --id Gyan.FFmpeg -e
  ```
- **火山引擎账号**，开通两个服务：
  - **大模型录音文件识别（极速版）** → 控制台拿 APP Key
  - **方舟 Ark** → 控制台拿 API Key，并开通豆包模型（如 `doubao-seed-2-1-pro-260628`）

  > 这两个是火山上**独立的服务**，凭证不通用。

### 安装

```bash
pip install -r requirements.txt
```

### 配置

在项目根目录 `.env` 填：

```
VOLC_APP_KEY=你的ASR_APP_Key
ARK_API_KEY=你的方舟API_Key
ARK_BASE_URL=https://ark.cn-beijing.volces.com/api/v3
ARK_MODEL=doubao-seed-2-1-pro-260628
```

> `ARK_MODEL` 可留空，首次运行自动探测可用模型（但建议显式指定，避免选到不合适的）。

---

## 输出

```
<视频目录>/edit/
  transcripts/<视频名>.json     # 词级+句级转录
  takes_packed.md                # 打包文本（豆包读这个选段）
  clips/
    edl_multi.json               # 选段决策（clips[].segments[]）
    clip_001.mp4  clip_001.srt   # 切片 + 字幕
    clip_002.mp4  clip_002.srt
    ...
    manifest.json                # 清单（标题/理由/时间码/段结构/文件/QC标记）
```

`manifest.json` 每条记录含 `segments`（多段时间码）、`duration`、`title`、`reason`、`file`、`srt`、`qc_flag`（vision 自评有问题时非空，提示人工复看）。

---

## Web UI 截图

- 首页：下拉选择视频、勾选是否烧字幕、点开始
- 进度页：4 个阶段进度条、实时日志可展开
- 结果页：每条切片可直接在浏览器播放、下载 mp4/srt、打开文件夹
- QC 标记：⚠️ 表示 vision 检测到切点跳变/爆音/字幕遮挡，建议人工复看

---

## 核心特性

- **语义选段**：豆包读整份转录文本，按金句/冲突/干货/情绪高点选段，不是按静音/音量的规则法。
- **多段拼接**：当一个论点分散在原视频几处不连续的地方，豆包把它们拼成一条切片，使成片逻辑连贯。
- **切点吸附**：起止吸附到短语边界，不在句中切断。
- **切点 padding**：每个切点前后留 50/80ms 缓冲，吸收 ASR 时间戳漂移，防止切到词头/词尾吞字。
- **cut-craft 工艺**：prompt 引导豆包优先在 ≥400ms 静音处切、保留笑声/掌声等音频事件标记并往后延含反应。
- **按句字幕**：一条字幕一整句（含标点、正常大小写），使用微软雅黑，不再是 2 字碎块。
- **广播级渲染**：auto 调色 + 30ms 淡入淡出消除切点爆音 + 响度归一（-14 LUFS，社交就绪）。
- **整份选段**：默认不分块，豆包一次看完整份转录，跨任意位置的关联片段都能拼。
- **vision 自评**：多段拼接的切点处自动出胶片条+波形图，豆包 vision 模型判画面跳变/爆音/字幕遮挡，有问题在 manifest 标 `qc_flag` 提示人工复看。
- **本地 Web UI**：双击 bat 启动，浏览器操作，非技术人员可用。

---

## 项目结构

```
live-slicing/
├── 启动.bat                     # 双击启动 Web UI
├── web.py                       # Web UI 启动入口（shim）
├── cli.py                       # 命令行入口（shim）
├── liveslicing/                 # Python 包（核心代码）
│   ├── __init__.py
│   ├── config.py                # .env 配置加载
│   ├── cli.py                   # 流水线编排
│   ├── transcribe.py            # 火山 ASR 适配
│   ├── transcribe_batch.py      # 批量并行转录
│   ├── pack_transcripts.py      # 打包 takes_packed.md
│   ├── pick_clips.py            # 豆包选段 + vision 自评
│   ├── render.py                # 多片段渲染（concat+字幕+响度）
│   ├── grade.py                 # 调色（auto/subtle/warm_cinematic）
│   └── timeline_view.py         # QC 可视化（胶片条+波形）
├── web/                         # Flask Web UI
│   ├── app.py                   # Flask 路由
│   ├── job.py                   # 后台任务管理
│   └── templates/index.html     # 前端页面（vanilla JS）
├── scripts/                     # 一次性探针/测试脚本
├── pyproject.toml
├── requirements.txt
├── .env.example
├── .env                         # 你的凭证（不入库）
├── README.md
└── DESIGN.md                    # 详细设计文档
```

---

## 故障排查

- **`ffmpeg 不在 PATH`**：新开终端；或 `winget install --id Gyan.FFmpeg -e` 安装后重启终端。
- **`X-Api-Status-Code 非 20000000`**：ASR APP Key 或资源 ID 不对，看输出里的 `X-Api-Message`。
- **`model does not exist or no access`**：方舟控制台没开通该豆包模型，或 `ARK_MODEL` 写错。
- **双击 bat 闪退**：看黑色窗口最后一行错误，通常是 Python 没装或依赖没装（运行 `pip install -r requirements.txt`）。
- **网页打不开**：确认命令行窗口里显示 "Running on http://127.0.0.1:5876"；手动访问这个地址。
- **终端中文乱码**：是 Windows 终端 GBK 显示问题，mp4 里烧的字幕是 UTF-8 正常的。
- **concat 失败警告**：多段拼接时自动回退重编码，无需干预。

---

## 关于费用

- 火山 ASR 按音频时长计费（约几元/小时）。转录结果有缓存，重跑不重转。
- 豆包推理按 token 计费，整份一次调用，量很小。

---

## Credits

渲染流水线、转录打包、timeline_view 胶片条+波形 QC 基于 [video-use](https://github.com/browser-use/video-use) skill（MIT License）改造。已扩展多段拼接、CJK 字幕、响度归一、auto 调色、Volcengine/豆包适配、vision 自评和本地 Web UI。详见 `THIRD_PARTY_NOTICES.md` 和 `LICENSE`。
