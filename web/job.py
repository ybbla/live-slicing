"""后台任务管理：一次一个切片任务，状态更新、日志捕获。"""
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


# Stage ordering for percent aggregation
STAGE_WEIGHTS = {
    "transcribe": 0.55,   # ASR is the slowest
    "pack": 0.05,
    "select": 0.15,
    "render": 0.25,
}
STAGE_ORDER = ["transcribe", "pack", "select", "render"]


@dataclass
class Job:
    id: str
    video_path: Path
    subtitles: bool
    output_dir: Path
    status: str = "idle"   # idle | running | done | error
    current_stage: str = ""
    stage_percent: int = 0
    stage_message: str = ""
    log_lines: deque = field(default_factory=lambda: deque(maxlen=200))
    manifest: dict | None = None
    clips_dir: Path | None = None
    error: str | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None

    def overall_percent(self) -> int:
        """Compute overall pipeline progress 0-100."""
        if self.status == "done":
            return 100
        if self.status == "idle":
            return 0
        if self.status == "error":
            return self.stage_percent
        # accumulate
        pct = 0
        for st in STAGE_ORDER:
            if st == self.current_stage:
                pct += int(STAGE_WEIGHTS[st] * self.stage_percent)
                break
            pct += int(STAGE_WEIGHTS[st] * 100)
        return min(100, max(0, pct))


class _TeeWriter(io.TextIOBase):
    """Write to both original stdout and job.log_lines."""
    def __init__(self, job: Job, original_stream):
        self.job = job
        self.orig = original_stream
        self._buf = ""

    def write(self, s: str) -> int:
        try:
            self.orig.write(s)
        except Exception:
            pass
        self._buf += s
        while "\n" in self._buf:
            line, self._buf = self._buf.split("\n", 1)
            line = line.rstrip("\r")
            if line:
                self.job.log_lines.append(line)
        return len(s)

    def flush(self):
        if self._buf:
            self.job.log_lines.append(self._buf)
            self._buf = ""
        try:
            self.orig.flush()
        except Exception:
            pass


class JobManager:
    _instance: "JobManager | None" = None
    _lock = threading.Lock()

    def __init__(self):
        self.current: Job | None = None
        self._thread: threading.Thread | None = None

    @classmethod
    def instance(cls) -> "JobManager":
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = cls()
        return cls._instance

    def is_running(self) -> bool:
        return self.current is not None and self.current.status == "running"

    def start(self, video_path: Path, subtitles: bool, output_dir: Path) -> Job:
        with self._lock:
            if self.is_running():
                raise RuntimeError("已有任务在运行，请等它完成")
            job = Job(
                id=uuid.uuid4().hex[:8],
                video_path=video_path.resolve(),
                subtitles=bool(subtitles),
                output_dir=output_dir.resolve(),
            )
            self.current = job

        t = threading.Thread(target=self._run, args=(job,), daemon=True)
        self._thread = t
        t.start()
        return job

    def _run(self, job: Job):
        from liveslicing.cli import run as pipeline_run
        import json

        job.status = "running"
        job.started_at = datetime.now()

        original_stdout = sys.stdout
        original_stderr = sys.stderr
        tee_out = _TeeWriter(job, original_stdout)
        tee_err = _TeeWriter(job, original_stderr)
        sys.stdout = tee_out
        sys.stderr = tee_err

        try:
            def on_progress(stage: str, percent: int, message: str):
                job.current_stage = stage
                job.stage_percent = max(0, min(100, int(percent)))
                job.stage_message = message
                tee_out.flush()

            # Ensure output directory exists
            job.output_dir.mkdir(parents=True, exist_ok=True)
            edit_dir = job.output_dir
            manifest = pipeline_run(
                video=job.video_path,
                edit_dir=edit_dir,
                count=0,                     # auto
                min_duration=30.0,
                max_duration=300.0,
                chunk_minutes=0,
                grade="auto",
                subtitles=job.subtitles,
                preview=False,
                from_stage="all",
                on_progress=on_progress,
            )
            job.manifest = manifest
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
            tee_out.flush()
            tee_err.flush()
            sys.stdout = original_stdout
            sys.stderr = original_stderr
            job.finished_at = datetime.now()

    def status_dict(self) -> dict:
        j = self.current
        if not j:
            return {"status": "idle"}
        return {
            "id": j.id,
            "status": j.status,
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
