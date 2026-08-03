"""后台任务管理模块：单例任务管理器，支持切片任务后台运行、进度聚合、stdout日志 Tee 捕获。

同一时间仅允许运行一个切片任务，通过on_progress回调实时更新进度，捕获标准输出到环形缓冲区供前端轮询展示。
"""
from __future__ import annotations

import io
import sys
import threading
import uuid
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable


# 各阶段进度权重
# 全自动模式：转录55%，打包5%，选段15%，渲染25%
STAGE_WEIGHTS_AUTO = {
    "transcribe": 0.55,
    "pack": 0.05,
    "select": 0.15,
    "render": 0.25,
}
# 命题模式（默认）：转录45%，打包5%，提取命题10%，精修15%，渲染25%
STAGE_WEIGHTS_PROPOSE = {
    "transcribe": 0.45,
    "pack": 0.05,
    "propose": 0.10,
    "refine": 0.15,
    "render": 0.25,
}
STAGE_ORDER_AUTO = ["transcribe", "pack", "select", "render"]
STAGE_ORDER_PROPOSE = ["transcribe", "pack", "propose", "refine", "render"]


@dataclass
class Job:
    """切片任务数据类。

    Attributes:
        id: 任务唯一ID（8位hex）
        video_path: 源视频路径
        subtitles: 是否烧录硬字幕
        background: 是否启用竖屏背景+顶部标题模式
        preview: 是否快速预览模式
        grade: 调色模式
        min_duration: 单条最短时长（秒）
        max_duration: 单条最长时长（秒）
        output_dir: 输出目录
        mode: 运行模式："propose"(命题选择模式，默认) / "auto"(全自动模式)
        from_stage: 起始阶段缓存复用
        status: 任务状态：idle/running/waiting_selection/done/error
        current_stage: 当前运行阶段
        stage_percent: 当前阶段进度0-100
        stage_message: 当前阶段状态消息
        log_lines: 日志环形缓冲区，最多保留200行
        manifest: 完成后的结果清单
        clips_dir: 切片输出目录
        propositions: 提取到的命题列表（waiting_selection状态时有效）
        selected_prop_ids: 用户选中的命题ID列表
        resume_event: 暂停/恢复用的threading.Event
        error: 错误信息
        started_at: 开始时间
        finished_at: 结束时间
    """
    id: str
    video_path: Path
    subtitles: bool
    output_dir: Path
    background: bool = False
    preview: bool = False
    grade: str = "auto"
    min_duration: float = 30.0
    max_duration: float = 300.0
    from_stage: str = "all"
    mode: str = "propose"
    status: str = "idle"
    current_stage: str = ""
    stage_percent: int = 0
    stage_message: str = ""
    log_lines: deque = field(default_factory=lambda: deque(maxlen=200))
    manifest: dict | None = None
    clips_dir: Path | None = None
    propositions: list | None = None
    selected_prop_ids: list[int] = field(default_factory=list)
    merge_selected: bool = False
    resume_event: threading.Event | None = None
    refresh_propositions: bool = False
    error: str | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None

    def stage_weights(self) -> dict:
        """动态计算阶段权重，跳过的阶段权重为0，剩余阶段按原比例归一化到总和100%。

        Returns:
            各阶段权重字典，总和为1.0
        """
        is_propose = (self.mode == "propose")
        base_weights = STAGE_WEIGHTS_PROPOSE if is_propose else STAGE_WEIGHTS_AUTO
        stage_order = STAGE_ORDER_PROPOSE if is_propose else STAGE_ORDER_AUTO

        # 判断各阶段是否会实际运行
        run_transcribe = self.from_stage in ("all", "transcribe")
        run_pack = self.from_stage in ("all", "transcribe", "pack")
        run_propose = is_propose and self.from_stage in ("all", "transcribe", "pack", "propose")
        run_select = (not is_propose)
        run_refine = is_propose  # refine阶段始终会运行（在用户选择后）
        run_render = True

        active = {}
        for st in stage_order:
            if st == "transcribe":
                active[st] = base_weights[st] if run_transcribe else 0
            elif st == "pack":
                active[st] = base_weights[st] if run_pack else 0
            elif st == "propose":
                active[st] = base_weights[st] if run_propose else 0
            elif st == "select":
                active[st] = base_weights[st] if run_select else 0
            elif st == "refine":
                active[st] = base_weights[st] if run_refine else 0
            elif st == "render":
                active[st] = base_weights[st] if run_render else 0
        total = sum(active.values())
        if total <= 0:
            return base_weights
        # 归一化到总和1.0
        return {k: v / total for k, v in active.items()}

    def overall_percent(self) -> int:
        """计算整体流水线进度0-100，按动态阶段权重聚合。

        Returns:
            整体进度百分比
        """
        if self.status == "done":
            return 100
        if self.status == "idle":
            return 0
        if self.status == "error":
            return self.stage_percent
        if self.status == "waiting_selection":
            # 等待用户选择时，进度停在propose阶段完成处
            weights = self.stage_weights()
            stage_order = STAGE_ORDER_PROPOSE if self.mode == "propose" else STAGE_ORDER_AUTO
            pct = 0
            for st in stage_order:
                if st == "propose":
                    pct += int(weights[st] * 100)
                    break
                pct += int(weights[st] * 100)
            return min(100, max(0, pct))
        # 累加已完成阶段权重，再加上当前阶段进度占比
        weights = self.stage_weights()
        stage_order = STAGE_ORDER_PROPOSE if self.mode == "propose" else STAGE_ORDER_AUTO
        pct = 0
        for st in stage_order:
            if st == self.current_stage:
                pct += int(weights[st] * self.stage_percent)
                break
            pct += int(weights[st] * 100)
        return min(100, max(0, pct))


class _TeeWriter(io.TextIOBase):
    """Tee输出流：同时写入原始stdout/stderr和任务日志缓冲区。"""
    def __init__(self, job: Job, original_stream):
        self.job = job
        self.orig = original_stream
        self._buf = ""

    def write(self, s: str) -> int:
        """写入字符串，同时输出到原始流和日志缓冲区。

        Args:
            s: 要写入的字符串

        Returns:
            写入的字符数
        """
        try:
            self.orig.write(s)
        except Exception:
            pass
        self._buf += s
        # 按换行符分割成行写入日志
        while "\n" in self._buf:
            line, self._buf = self._buf.split("\n", 1)
            line = line.rstrip("\r")
            if line:
                self.job.log_lines.append(line)
        return len(s)

    def flush(self):
        """刷新缓冲区，将剩余内容写入日志。"""
        if self._buf:
            self.job.log_lines.append(self._buf)
            self._buf = ""
        try:
            self.orig.flush()
        except Exception:
            pass


class JobManager:
    """单例任务管理器，同一时间仅运行一个切片任务。"""
    _instance: "JobManager | None" = None
    _lock = threading.Lock()

    def __init__(self):
        self.current: Job | None = None
        self._thread: threading.Thread | None = None

    @classmethod
    def instance(cls) -> "JobManager":
        """获取单例实例，双重检查锁保证线程安全。"""
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = cls()
        return cls._instance

    def is_running(self) -> bool:
        """检查当前是否有任务正在运行或等待选择（不允许启动新任务）。
        自动检测线程是否已死亡，如果线程已退出则自动重置状态，避免卡死。
        """
        if self.current is None:
            return False
        # 如果任务标记为运行中但线程已经死了，自动重置状态
        if self.current.status in ("running", "waiting_selection"):
            if self._thread is None or not self._thread.is_alive():
                # 线程已经异常退出，重置任务状态
                self.current = None
                self._thread = None
                return False
            return True
        return False

    def reset(self):
        """强制重置任务管理器状态，清除卡住的任务。"""
        self.current = None
        self._thread = None

    def start(
        self,
        video_path: Path,
        subtitles: bool,
        output_dir: Path,
        background: bool = False,
        preview: bool = False,
        grade: str = "auto",
        min_duration: float = 30.0,
        max_duration: float = 300.0,
        from_stage: str = "all",
        mode: str = "propose",
    ) -> Job:
        """启动新的切片任务，后台线程运行。

        Args:
            video_path: 源视频路径
            subtitles: 是否烧录硬字幕
            output_dir: 输出目录
            background: 是否启用竖屏背景+顶部标题模式
            preview: 是否快速预览模式
            grade: 调色模式
            min_duration: 单条最短时长（秒）
            max_duration: 单条最长时长（秒）
            from_stage: 起始阶段：all/transcribe/pack/select/propose
            mode: 运行模式："propose"(命题选择默认) / "auto"(全自动)

        Returns:
            创建的Job对象

        Raises:
            RuntimeError: 已有任务运行时抛出
        """
        with self._lock:
            if self.is_running():
                raise RuntimeError("已有任务在运行，请等它完成")
            job = Job(
                id=uuid.uuid4().hex[:8],
                video_path=video_path.resolve(),
                subtitles=bool(subtitles),
                background=bool(background),
                output_dir=output_dir.resolve(),
                preview=bool(preview),
                grade=str(grade),
                min_duration=float(min_duration),
                max_duration=float(max_duration),
                from_stage=str(from_stage),
                mode=str(mode) if str(mode) in ("propose", "auto") else "propose",
            )
            self.current = job

        t = threading.Thread(target=self._run, args=(job,), daemon=True)
        self._thread = t
        t.start()
        return job

    def resume_with_selection(self, prop_ids: list[int], merge: bool = False) -> bool:
        """用户提交选择后恢复命题模式流水线。

        Args:
            prop_ids: 用户选中的命题ID列表
            merge: 是否将选中的命题合并为单个切片

        Returns:
            是否成功恢复
        """
        with self._lock:
            job = self.current
            if not job or job.status != "waiting_selection":
                return False
            job.selected_prop_ids = list(prop_ids)
            job.merge_selected = bool(merge)
            job.status = "running"
            job.current_stage = "refine"
            job.stage_percent = 0
            if merge:
                job.stage_message = f"开始精修并合并 {len(prop_ids)} 个选中命题…"
            else:
                job.stage_message = f"开始精修 {len(prop_ids)} 个选中命题…"
            job.refresh_propositions = False
            if job.resume_event:
                job.resume_event.set()
            return True

    def refresh_propositions(self) -> bool:
        """用户点击「换一批看点」，重新提取命题。

        Returns:
            是否成功触发刷新
        """
        with self._lock:
            job = self.current
            if not job or job.status != "waiting_selection":
                return False
            job.status = "running"
            job.current_stage = "propose"
            job.stage_percent = 0
            job.stage_message = "🔄 正在重新提取精彩看点…"
            job.refresh_propositions = True
            job.propositions = None
            if job.resume_event:
                job.resume_event.set()
            return True

    def _run(self, job: Job):
        """后台线程实际运行流水线的函数。

        Args:
            job: 要运行的任务对象
        """
        from liveslicing.cli import run as pipeline_run
        from liveslicing.cli import run_to_propositions, run_from_selection

        job.status = "running"
        job.started_at = datetime.now()

        # 替换stdout/stderr捕获日志
        original_stdout = sys.stdout
        original_stderr = sys.stderr
        tee_out = _TeeWriter(job, original_stdout)
        tee_err = _TeeWriter(job, original_stderr)
        sys.stdout = tee_out
        sys.stderr = tee_err

        try:
            def on_progress(stage: str, percent: int, message: str):
                """进度回调，更新任务状态。"""
                job.current_stage = stage
                job.stage_percent = max(0, min(100, int(percent)))
                job.stage_message = message
                tee_out.flush()

            # 确保输出目录存在
            job.output_dir.mkdir(parents=True, exist_ok=True)
            edit_dir = job.output_dir

            if job.mode == "propose":
                # 命题模式：第一阶段跑转录→打包→命题提取（支持换一批循环）
                while True:
                    # 确定from_stage：刷新时强制重新提取命题
                    current_from_stage = "propose" if job.refresh_propositions else job.from_stage
                    job.refresh_propositions = False

                    # 刷新时删除旧缓存
                    if current_from_stage == "propose":
                        props_cache = edit_dir / "propositions.json"
                        if props_cache.exists():
                            try:
                                props_cache.unlink()
                            except Exception:
                                pass

                    propositions = run_to_propositions(
                        video=job.video_path,
                        edit_dir=edit_dir,
                        from_stage=current_from_stage,
                        on_progress=on_progress,
                    )
                    job.propositions = propositions
                    if not propositions:
                        job.status = "error"
                        job.error = "未能提取到精彩看点，请尝试全自动模式"
                        job.stage_message = "❌ 未能提取到精彩看点"
                        return

                    # 进入等待用户选择状态，线程阻塞在Event上
                    job.status = "waiting_selection"
                    job.current_stage = "propose"
                    job.stage_percent = 100
                    job.stage_message = f"发现 {len(propositions)} 个精彩看点，请选择要生成的切片"
                    tee_out.flush()

                    job.resume_event = threading.Event()
                    # 最多等待30分钟
                    selected = job.resume_event.wait(timeout=1800)
                    job.resume_event = None

                    if not selected:
                        # 超时
                        job.status = "error"
                        job.error = "等待选择超时（30分钟），任务已取消"
                        job.stage_message = "等待选择超时"
                        return
                    if job.status != "running":
                        # 被取消或错误
                        return
                    if job.refresh_propositions:
                        # 用户点击了「换一批」，循环回去重新提取
                        job.selected_prop_ids = []
                        continue
                    if not job.selected_prop_ids:
                        job.status = "done"
                        job.stage_message = "未选择任何命题，任务结束"
                        job.current_stage = "done"
                        job.stage_percent = 100
                        return
                    # 用户选择了命题，跳出循环进入精修
                    break

                # 第二阶段：精修+渲染
                merge_mode = getattr(job, 'merge_selected', False)
                if merge_mode:
                    on_progress("refine", 0, f"开始精修并合并 {len(job.selected_prop_ids)} 个选中命题…")
                else:
                    on_progress("refine", 0, f"开始精修 {len(job.selected_prop_ids)} 个选中命题…")
                manifest = run_from_selection(
                    video=job.video_path,
                    edit_dir=edit_dir,
                    selected_prop_ids=job.selected_prop_ids,
                    grade=job.grade,
                    subtitles=job.subtitles,
                    background=job.background,
                    preview=job.preview,
                    min_duration=job.min_duration,
                    max_duration=job.max_duration,
                    merge=merge_mode,
                    on_progress=on_progress,
                )
            else:
                # 全自动模式：完整流水线（自动选高光片段）
                manifest = pipeline_run(
                    video=job.video_path,
                    edit_dir=edit_dir,
                    min_duration=job.min_duration,
                    max_duration=job.max_duration,
                    grade=job.grade,
                    subtitles=job.subtitles,
                    background=job.background,
                    preview=job.preview,
                    from_stage=job.from_stage,
                    on_progress=on_progress,
                )

            job.manifest = manifest
            # 优先从manifest读取实际带时间戳的clips目录，兼容旧清单回退到clips软链接
            clips_dir_str = manifest.get("clips_dir")
            if clips_dir_str:
                job.clips_dir = Path(clips_dir_str)
            else:
                job.clips_dir = edit_dir / "clips"
            job.current_stage = "done"
            job.stage_percent = 100
            job.stage_message = f"完成！共 {len(manifest.get('clips', []))} 条切片"
            job.status = "done"
        except Exception as e:
            import traceback
            traceback.print_exc()
            job.status = "error"
            job.error = str(e)
            job.stage_message = f"出错：{e}"
        finally:
            # 恢复原始stdout/stderr
            tee_out.flush()
            tee_err.flush()
            sys.stdout = original_stdout
            sys.stderr = original_stderr
            job.finished_at = datetime.now()

    def status_dict(self) -> dict:
        """返回任务状态字典，供API接口序列化为JSON。

        Returns:
            状态字典，无任务时返回{"status": "idle"}
        """
        j = self.current
        if not j:
            return {"status": "idle"}
        result = {
            "id": j.id,
            "status": j.status,
            "mode": j.mode,
            "video": str(j.video_path),
            "video_name": j.video_path.name,
            "subtitles": j.subtitles,
            "current_stage": j.current_stage,
            "percent": j.overall_percent(),
            "stage_percent": j.stage_percent,
            "message": j.stage_message,
            "log_lines": list(j.log_lines)[-80:],
            "clips_dir": str(j.clips_dir) if j.clips_dir else None,
            "manifest": j.manifest,
            "error": j.error,
            "started_at": j.started_at.isoformat() if j.started_at else None,
            "finished_at": j.finished_at.isoformat() if j.finished_at else None,
        }
        # 等待选择状态时返回命题列表
        if j.status == "waiting_selection" and j.propositions:
            result["propositions"] = j.propositions
        return result