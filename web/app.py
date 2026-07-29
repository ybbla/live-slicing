"""Flask Web UI for live-slicing.

Usage:
    python web.py
Then open http://localhost:5876
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

from flask import Flask, jsonify, render_template, request, send_from_directory, abort

from web.job import JobManager


# Project root (two levels up from this file: web/app.py → root)
PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"
UPLOAD_DIR = DATA_DIR / "uploads"
OUTPUT_DIR = DATA_DIR / "output"

# Ensure directories exist
for d in (DATA_DIR, UPLOAD_DIR, OUTPUT_DIR):
    d.mkdir(parents=True, exist_ok=True)

app = Flask(
    __name__,
    template_folder=str(Path(__file__).resolve().parent / "templates"),
    static_folder=str(Path(__file__).resolve().parent / "static"),
)
# Disable upload size limit for large video files
app.config["MAX_CONTENT_LENGTH"] = None

jm = JobManager.instance()


def _ffmpeg_available() -> bool:
    return shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None


def _env_configured() -> bool:
    """Check that .env has both required keys (or env vars set)."""
    sys.path.insert(0, str(PROJECT_ROOT))
    try:
        from liveslicing.config import load_env
        env = load_env()
        return bool(env.get("VOLC_APP_KEY")) and bool(env.get("ARK_API_KEY"))
    except Exception:
        return False


def _probe_video_info(path: Path) -> dict:
    """Return size_mb, duration_s for an mp4."""
    size_mb = round(path.stat().st_size / (1024 * 1024), 1)
    dur = 0.0
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
            capture_output=True, text=True, timeout=10,
        )
        dur = float(out.stdout.strip() or 0)
    except Exception:
        pass
    return {
        "name": path.name,
        "path": str(path),
        "size_mb": size_mb,
        "duration_s": round(dur, 1),
        "mtime": path.stat().st_mtime,
    }


def _fmt_dur(seconds: float) -> str:
    if seconds <= 0:
        return ""
    s = int(seconds)
    if s < 60:
        return f"{s}s"
    m, s = divmod(s, 60)
    if m < 60:
        return f"{m}m{s:02d}s"
    h, m = divmod(m, 60)
    return f"{h}h{m}m"


def _unique_path(parent: Path, name: str) -> Path:
    """Return a unique path under parent by appending _N suffix if needed."""
    target = parent / name
    if not target.exists():
        return target
    stem = target.stem
    suffix = target.suffix
    i = 1
    while True:
        target = parent / f"{stem}_{i}{suffix}"
        if not target.exists():
            return target
        i += 1


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/config")
def api_config():
    return jsonify({
        "ffmpeg_ok": _ffmpeg_available(),
        "env_ok": _env_configured(),
        "project_root": str(PROJECT_ROOT),
        "output_dir": str(OUTPUT_DIR),
    })


@app.route("/api/upload", methods=["POST"])
def api_upload():
    """Handle video file upload via multipart/form-data."""
    if "video" not in request.files:
        return jsonify({"error": "未找到上传文件"}), 400
    f = request.files["video"]
    if not f.filename:
        return jsonify({"error": "文件名为空"}), 400
    if not f.filename.lower().endswith(".mp4"):
        return jsonify({"error": "只支持 .mp4 文件"}), 400

    # Get unique save path
    filename = Path(f.filename).name  # Sanitize: get only filename, no path
    save_path = _unique_path(UPLOAD_DIR, filename)

    # Stream save to disk (supports large files without loading to memory)
    chunk_size = 1024 * 1024  # 1MB chunks
    try:
        with open(save_path, "wb") as out_f:
            while True:
                chunk = f.stream.read(chunk_size)
                if not chunk:
                    break
                out_f.write(chunk)
    except Exception as e:
        # Clean up partial file if save fails
        if save_path.exists():
            save_path.unlink()
        return jsonify({"error": f"保存文件失败: {str(e)}"}), 500

    # Probe video info
    try:
        info = _probe_video_info(save_path)
    except Exception as e:
        save_path.unlink(missing_ok=True)
        return jsonify({"error": f"读取视频信息失败: {str(e)}"}), 500

    info["duration_str"] = _fmt_dur(info["duration_s"])
    info["stem"] = save_path.stem
    return jsonify(info)


@app.route("/api/start", methods=["POST"])
def api_start():
    data = request.get_json(force=True) or {}
    video_str = data.get("video", "").strip()
    subtitles = bool(data.get("subtitles", False))

    if not video_str:
        return jsonify({"error": "未选择视频"}), 400

    # Parse video path
    video_path = Path(video_str)
    if not video_path.is_absolute():
        video_path = (PROJECT_ROOT / video_path).resolve()
    else:
        video_path = video_path.resolve()

    # Validate video file
    if not video_path.exists() or not video_path.is_file():
        return jsonify({"error": f"视频不存在: {video_path.name}"}), 400
    if video_path.suffix.lower() != ".mp4":
        return jsonify({"error": "只支持 .mp4 文件"}), 400

    if jm.is_running():
        return jsonify({"error": "已有任务在运行，请等它完成"}), 409

    if not _ffmpeg_available():
        return jsonify({"error": "ffmpeg/ffprobe 未安装或不在 PATH"}), 500
    if not _env_configured():
        return jsonify({"error": ".env 未配置 VOLC_APP_KEY / ARK_API_KEY"}), 500

    # Determine unique output directory: OUTPUT_DIR / <video_stem>
    output_dir = _unique_path(OUTPUT_DIR, video_path.stem)

    job = jm.start(video_path, subtitles, output_dir)
    return jsonify({"job_id": job.id, "output_dir": str(output_dir)})


@app.route("/api/status")
def api_status():
    return jsonify(jm.status_dict())


@app.route("/clips/<path:filename>")
def serve_clip(filename: str):
    """Serve rendered clips for preview/download.

    Clips live under OUTPUT_DIR/<video_name>/clips/.
    For safety, only serve files whose path is under a clips directory
    within OUTPUT_DIR, and block path traversal.
    """
    # Block path traversal attempts
    if ".." in filename or filename.startswith("/") or filename.startswith("\\"):
        abort(404)
    # Search all clips directories recursively under OUTPUT_DIR
    for clips_dir in OUTPUT_DIR.rglob("clips"):
        if not clips_dir.is_dir():
            continue
        try:
            clips_resolved = clips_dir.resolve()
            target = (clips_resolved / filename).resolve()
            # Verify target is actually inside this clips_dir
            target.relative_to(clips_resolved)
        except ValueError:
            continue
        if target.exists() and target.is_file():
            return send_from_directory(str(clips_resolved), filename)
    abort(404)


@app.route("/api/history")
def api_history():
    """List all completed slicing jobs from output directory."""
    items = []
    for output_subdir in sorted(OUTPUT_DIR.iterdir(), key=lambda x: x.stat().st_mtime, reverse=True):
        if not output_subdir.is_dir():
            continue
        clips_dir = output_subdir / "clips"
        manifest_path = clips_dir / "manifest.json"
        if not manifest_path.exists():
            continue
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            clips = manifest.get("clips", [])
            total_dur = sum(c.get("duration", 0) for c in clips)
            items.append({
                "video_name": output_subdir.name,
                "clips_dir": str(clips_dir),
                "output_dir": str(output_subdir),
                "created_at": output_subdir.stat().st_mtime,
                "clip_count": len(clips),
                "total_duration": round(total_dur, 1),
                "clips": clips,
            })
        except Exception:
            continue
    return jsonify({"items": items})


@app.route("/api/open-folder", methods=["POST"])
def api_open_folder():
    """Open the clips output folder in Windows Explorer.
    Accepts optional "path" parameter for historical jobs; falls back to current job.
    """
    data = request.get_json(silent=True) or {}
    folder_str = data.get("path")
    if not folder_str:
        st = jm.status_dict()
        folder_str = st.get("clips_dir")
    if not folder_str:
        return jsonify({"error": "没有可打开的目录"}), 400
    p = Path(folder_str)
    if not p.exists():
        return jsonify({"error": f"目录不存在: {folder_str}"}), 404
    try:
        if sys.platform == "win32":
            os.startfile(str(p))  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(p)])
        else:
            subprocess.Popen(["xdg-open", str(p)])
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


def main():
    import threading
    import webbrowser

    host = "127.0.0.1"
    port = 5876
    url = f"http://localhost:{port}"
    print(f"智能直播切片 Web UI 启动中…")
    print(f"  地址: {url}")
    print(f"  项目目录: {PROJECT_ROOT}")
    print(f"  关闭此窗口即停止服务")
    print()

    # Open browser after a short delay so Flask is ready
    if not os.environ.get("SLICE_NO_BROWSER"):
        def _open_browser():
            import time
            time.sleep(1.5)
            try:
                webbrowser.open(url)
            except Exception:
                pass
        threading.Thread(target=_open_browser, daemon=True).start()

    app.run(host=host, port=port, debug=False, use_reloader=False)


if __name__ == "__main__":
    main()
