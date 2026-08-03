"""智能直播切片Web UI Flask后端服务。

提供视频上传、切片任务启动、进度查询、结果预览/下载、历史任务管理、打开输出目录等接口。

使用方式：
    python web.py
启动后自动打开浏览器访问 http://localhost:5876
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path


def _validate_output_path(path_str: str) -> Path:
    """校验输入路径是否在OUTPUT_DIR内，防止路径遍历攻击，返回解析后的绝对路径。"""
    p = Path(path_str).resolve()
    output_root = OUTPUT_DIR.resolve()
    try:
        p.relative_to(output_root)
    except ValueError:
        abort(403, description="非法路径，仅允许操作输出目录下的内容")
    return p

from flask import Flask, jsonify, render_template, request, send_from_directory, abort

from web.job import JobManager


# 项目根目录（web/app.py向上两级）
PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"
UPLOAD_DIR = DATA_DIR / "uploads"
OUTPUT_DIR = DATA_DIR / "output"

# 初始化数据目录
for d in (DATA_DIR, UPLOAD_DIR, OUTPUT_DIR):
    d.mkdir(parents=True, exist_ok=True)

# 支持的输入视频格式（ffmpeg原生支持的常见直播/录屏格式，最终输出统一为标准MP4）
ALLOWED_VIDEO_EXTS = {".mp4", ".mov", ".mkv", ".flv", ".ts", ".avi", ".m4v", ".webm"}

app = Flask(
    __name__,
    template_folder=str(Path(__file__).resolve().parent / "templates"),
    static_folder=str(Path(__file__).resolve().parent / "static"),
)
# 关闭上传大小限制，支持大视频文件
app.config["MAX_CONTENT_LENGTH"] = None
# 开启模板自动重载，修改模板后无需重启服务即可生效
app.config["TEMPLATES_AUTO_RELOAD"] = True
app.jinja_env.auto_reload = True

jm = JobManager.instance()


def _ffmpeg_available() -> bool:
    """检查ffmpeg和ffprobe是否已安装并在PATH中。"""
    return shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None


def _env_configured() -> bool:
    """检查.env中是否已配置火山ASR和豆包API所需的密钥。"""
    sys.path.insert(0, str(PROJECT_ROOT))
    try:
        from liveslicing.config import load_env
        env = load_env()
        return bool(env.get("VOLC_APP_KEY")) and bool(env.get("ARK_API_KEY"))
    except Exception:
        return False


def _probe_video_info(path: Path) -> dict:
    """探测mp4视频的大小、时长信息。

    Args:
        path: 视频文件路径

    Returns:
        视频信息字典：文件名、路径、大小MB、时长秒、修改时间
    """
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
    """将秒级时长格式化为易读字符串。

    Args:
        seconds: 时长秒数

    Returns:
        格式化字符串如"12s"、"3m45s"、"1h23m"
    """
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
    """生成父目录下唯一路径，重名文件自动加 (N) 后缀（Windows资源管理器风格）避免覆盖。

    Args:
        parent: 父目录
        name: 原始文件名

    Returns:
        不冲突的唯一路径
    """
    target = parent / name
    if not target.exists():
        return target
    stem = target.stem
    suffix = target.suffix
    i = 1
    while True:
        target = parent / f"{stem} ({i}){suffix}"
        if not target.exists():
            return target
        i += 1


@app.route("/")
def index():
    """首页：渲染前端单页应用。"""
    return render_template("index.html")


@app.route("/api/config")
def api_config():
    """获取环境配置状态接口：返回ffmpeg是否安装、密钥是否配置、目录路径。"""
    return jsonify({
        "ffmpeg_ok": _ffmpeg_available(),
        "env_ok": _env_configured(),
        "project_root": str(PROJECT_ROOT),
        "output_dir": str(OUTPUT_DIR),
    })


@app.route("/api/upload", methods=["POST"])
def api_upload():
    """视频文件上传接口：multipart/form-data流式上传，1MB分块保存，支持大文件不占内存。"""
    if "video" not in request.files:
        return jsonify({"error": "未找到上传文件"}), 400
    f = request.files["video"]
    if not f.filename:
        return jsonify({"error": "文件名为空"}), 400
    # 校验视频格式，大小写不敏感
    file_ext = Path(f.filename).suffix.lower()
    if file_ext not in ALLOWED_VIDEO_EXTS:
        return jsonify({"error": f"不支持的视频格式，支持的格式：{', '.join(ALLOWED_VIDEO_EXTS)}"}), 400

    # 安全处理文件名，只取文件名部分避免路径遍历
    filename = Path(f.filename).name
    save_path = _unique_path(UPLOAD_DIR, filename)

    # 流式分块写入磁盘，避免大文件占用过多内存
    chunk_size = 1024 * 1024  # 1MB块
    try:
        with open(save_path, "wb") as out_f:
            while True:
                chunk = f.stream.read(chunk_size)
                if not chunk:
                    break
                out_f.write(chunk)
    except Exception as e:
        # 保存失败时清理部分文件
        if save_path.exists():
            save_path.unlink()
        return jsonify({"error": f"保存文件失败: {str(e)}"}), 500

    # 探测视频元信息
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
    """启动切片任务接口：接收视频路径和切片配置参数。"""
    # 启动新任务前强制重置所有状态，彻底避免旧任务卡死导致无法启动
    jm.reset()
    data = request.get_json(force=True) or {}
    video_str = (data.get("video") or "").strip()
    subtitles = bool(data.get("subtitles", False))
    preview = bool(data.get("preview", False))
    reuse_dir_str = (data.get("reuse_dir") or "").strip()
    reuse_dir = None

    # 运行模式：propose(命题选择，默认) / auto(全自动)
    mode = str(data.get("mode", "propose")).lower()
    if mode not in ("propose", "auto"):
        mode = "propose"

    # 调色模式校验
    allowed_grades = {"auto", "none", "light", "neutral_punch", "warm_cinematic"}
    grade = str(data.get("grade", "auto")).lower()
    if grade not in allowed_grades:
        grade = "auto"

    # 时长参数校验：10秒~10分钟，最小间隔1秒
    try:
        min_duration = float(data.get("min_duration", 30.0))
    except (TypeError, ValueError):
        min_duration = 30.0
    min_duration = max(10.0, min(min_duration, 599.0))

    try:
        max_duration = float(data.get("max_duration", 300.0))
    except (TypeError, ValueError):
        max_duration = 300.0
    # 保证下限不超过上限，至少间隔1秒
    if max_duration < min_duration + 1:
        max_duration = min_duration + 1
    max_duration = max(min_duration + 1.0, min(max_duration, 600.0))

    if not video_str:
        return jsonify({"error": "未选择视频"}), 400

    # 解析视频路径，支持相对/绝对路径
    video_path = Path(video_str)
    if not video_path.is_absolute():
        video_path = (PROJECT_ROOT / video_path).resolve()
    else:
        video_path = video_path.resolve()

    # 先校验格式，再校验文件存在
    if video_path.suffix.lower() not in ALLOWED_VIDEO_EXTS:
        return jsonify({"error": f"不支持的视频格式，支持的格式：{', '.join(sorted(ALLOWED_VIDEO_EXTS))}"}), 400
    if not video_path.exists() or not video_path.is_file():
        return jsonify({"error": f"视频不存在: {video_path.name}"}), 400

    # 启动前已强制重置，不需要检查运行状态，直接允许启动新任务

    if not _ffmpeg_available():
        return jsonify({"error": "ffmpeg/ffprobe 未安装或不在 PATH"}), 500
    if not _env_configured():
        return jsonify({"error": ".env 未配置 VOLC_APP_KEY / ARK_API_KEY"}), 500

    # 生成/复用输出目录：重剪时复用原有目录，不新建重复文件夹
    if reuse_dir_str:
        reuse_dir = _validate_output_path(reuse_dir_str)
        output_dir = reuse_dir
    else:
        output_dir = _unique_path(OUTPUT_DIR, video_path.stem)
    output_dir.mkdir(parents=True, exist_ok=True)

    # 确定起始阶段（根据模式和缓存情况）
    from_stage = "all"
    if reuse_dir is not None:
        transcripts_dir_check = reuse_dir / "transcripts"
        packed_file_check = reuse_dir / "takes_packed.md"
        props_file_check = reuse_dir / "propositions.json"
        has_transcripts = transcripts_dir_check.exists() and any(transcripts_dir_check.glob("*.json"))
        has_packed = packed_file_check.exists()
        has_props = props_file_check.exists() and props_file_check.stat().st_size > 10

        if mode == "propose":
            # 命题模式：优先检查propositions缓存
            if has_transcripts and has_packed and has_props:
                # 检查propositions是否比packed新
                if props_file_check.stat().st_mtime >= packed_file_check.stat().st_mtime:
                    from_stage = "propose"  # 直接加载propositions等待选择，会走缓存
            if has_transcripts and has_packed and from_stage == "all":
                from_stage = "propose"  # 有转录和打包，直接从propose阶段开始
            elif has_transcripts:
                from_stage = "pack"
        else:
            # 全自动模式
            if has_transcripts and has_packed:
                from_stage = "select"
            elif has_transcripts:
                from_stage = "pack"

    # 新上传视频复用缓存逻辑：同视频转录/打包文件自动复用
    if not reuse_dir:
        import shutil
        target_transcript_dir = output_dir / "transcripts"
        target_transcript_dir.mkdir(parents=True, exist_ok=True)
        target_transcript_path = target_transcript_dir / f"{video_path.stem}.json"
        target_packed_path = output_dir / "takes_packed.md"
        target_props_path = output_dir / "propositions.json"
        if not target_transcript_path.exists() or not target_packed_path.exists():
            for existing_dir in OUTPUT_DIR.iterdir():
                if not existing_dir.is_dir() or existing_dir == output_dir:
                    continue
                existing_transcript = existing_dir / "transcripts" / f"{video_path.stem}.json"
                existing_packed = existing_dir / "takes_packed.md"
                existing_props = existing_dir / "propositions.json"
                if existing_transcript.exists() and existing_transcript.is_file():
                    try:
                        if not target_transcript_path.exists():
                            shutil.copy2(existing_transcript, target_transcript_path)
                        if not target_packed_path.exists() and existing_packed.exists() and existing_packed.is_file():
                            shutil.copy2(existing_packed, target_packed_path)
                        if mode == "propose" and not target_props_path.exists() and existing_props.exists() and existing_props.is_file():
                            if existing_packed.exists() and existing_props.stat().st_mtime >= existing_packed.stat().st_mtime:
                                shutil.copy2(existing_props, target_props_path)
                        break
                    except Exception:
                        pass

    job = jm.start(
        video_path=video_path,
        subtitles=subtitles,
        output_dir=output_dir,
        preview=preview,
        grade=grade,
        min_duration=min_duration,
        max_duration=max_duration,
        from_stage=from_stage,
        mode=mode,
    )
    return jsonify({"job_id": job.id, "output_dir": str(output_dir), "mode": mode})


@app.route("/api/select-propositions", methods=["POST"])
def api_select_propositions():
    """提交选中的命题ID列表，恢复流水线继续精修+渲染。支持merge参数将多个命题合并为单个切片。"""
    data = request.get_json(force=True) or {}
    prop_ids_raw = data.get("prop_ids", [])
    merge = bool(data.get("merge", False))
    try:
        prop_ids = [int(pid) for pid in prop_ids_raw]
    except (TypeError, ValueError):
        return jsonify({"error": "命题ID格式错误"}), 400
    if not prop_ids:
        return jsonify({"error": "请至少选择一个看点"}), 400
    if not jm.current or jm.current.status != "waiting_selection":
        return jsonify({"error": "当前没有等待选择的任务"}), 409
    ok = jm.resume_with_selection(prop_ids, merge=merge)
    if not ok:
        return jsonify({"error": "恢复任务失败"}), 500
    return jsonify({"ok": True})


@app.route("/api/refresh-propositions", methods=["POST"])
def api_refresh_propositions():
    """重新提取一批命题（换一批），复用转录和打包缓存。"""
    if not jm.current or jm.current.status != "waiting_selection":
        return jsonify({"error": "当前没有等待选择的任务"}), 409
    ok = jm.refresh_propositions()
    if not ok:
        return jsonify({"error": "刷新失败"}), 500
    return jsonify({"ok": True})


@app.route("/api/status")
def api_status():
    """查询当前任务状态接口：实时返回进度、阶段、日志、结果清单。"""
    return jsonify(jm.status_dict())


@app.route("/clips/<path:filename>")
def serve_clip(filename: str):
    """提供切片文件访问服务，支持预览和下载。

    支持两种路径格式：
    1. 版本化路径：<video_stem>/clips_YYYYMMDD_HHMMSS/clip_NNN.mp4，直接定位到指定版本目录
    2. 裸文件名（向后兼容）：clip_NNN.mp4，递归搜索所有clips_*目录返回最近匹配项

    安全防护：禁止路径遍历，仅允许访问output目录下clips子目录内的文件。
    """
    # 拦截路径遍历攻击
    if ".." in filename or filename.startswith("/") or filename.startswith("\\"):
        abort(404)

    # 版本化路径：包含路径分隔符，直接解析定位
    if "/" in filename or "\\" in filename:
        try:
            # 相对于OUTPUT_DIR解析完整路径
            target = (OUTPUT_DIR / filename).resolve()
            output_root = OUTPUT_DIR.resolve()
            target.relative_to(output_root)
            # 校验路径中包含clips_目录
            rel_parts = target.relative_to(output_root).parts
            has_clips_dir = any(part.startswith("clips_") for part in rel_parts[:-1])
            if has_clips_dir and target.exists() and target.is_file():
                # 确定文件所在的clips目录
                clips_dir = target.parent
                # 向上查找clips_开头的目录
                for parent in [target.parent] + list(target.parents):
                    try:
                        parent.relative_to(output_root)
                        if parent.name.startswith("clips_"):
                            clips_dir = parent
                            break
                    except ValueError:
                        break
                return send_from_directory(str(clips_dir), target.name)
        except ValueError:
            abort(403)
        abort(404)

    # 裸文件名（向后兼容）：递归查找最新的匹配文件
    candidates = []
    for clips_dir in OUTPUT_DIR.rglob("clips*"):
        if not clips_dir.is_dir() or not clips_dir.name.startswith("clips"):
            continue
        try:
            clips_resolved = clips_dir.resolve()
            target = (clips_resolved / filename).resolve()
            target.relative_to(clips_resolved)
            if target.exists() and target.is_file():
                candidates.append((target.stat().st_mtime, clips_resolved, filename))
        except ValueError:
            continue
    if candidates:
        # 返回最新修改的版本
        candidates.sort(reverse=True, key=lambda x: x[0])
        _, clips_dir, fname = candidates[0]
        return send_from_directory(str(clips_dir), fname)
    abort(404)


@app.route("/api/history")
def api_history():
    """查询历史任务接口：扫描output目录返回所有已完成的切片任务，包含所有重剪版本。"""
    items = []
    for output_subdir in sorted(OUTPUT_DIR.iterdir(), key=lambda x: x.stat().st_mtime, reverse=True):
        if not output_subdir.is_dir():
            continue
        # 只收集clips_开头的真实时间戳目录，跳过软链接/联接和clips目录本身
        clips_dirs = []
        for d in output_subdir.iterdir():
            try:
                # 跳过重解析点（软链接/目录联接），避免重复计数
                if d.is_symlink() or (d.is_dir() and hasattr(d.stat(), 'st_file_attributes') and (d.stat().st_file_attributes & 0x400)):
                    continue
                if not d.is_dir() or not d.name.startswith("clips_"):
                    continue
                manifest_path = d / "manifest.json"
                if manifest_path.exists():
                    clips_dirs.append((d.stat().st_mtime, d))
            except Exception:
                continue
        if not clips_dirs:
            continue
        # 按修改时间倒序排列，最新版本在前
        clips_dirs.sort(reverse=True, key=lambda x: x[0])

        # 构建所有版本数组
        versions = []
        for mtime, clips_dir in clips_dirs:
            try:
                manifest_path = clips_dir / "manifest.json"
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                clips = manifest.get("clips", [])
                total_dur = sum(c.get("duration", 0) for c in clips)
                config = manifest.get("config", {})
                # 计算相对OUTPUT_DIR的路径，用于版本化文件访问
                try:
                    clips_rel = str(clips_dir.resolve().relative_to(OUTPUT_DIR.resolve())).replace("\\", "/")
                except ValueError:
                    clips_rel = clips_dir.name
                versions.append({
                    "clips_dir": str(clips_dir),
                    "clips_rel": clips_rel,
                    "created_at": mtime,
                    "clip_count": len(clips),
                    "total_duration": round(total_dur, 1),
                    "clips": clips,
                    "config": config,
                })
            except Exception:
                # 损坏的manifest跳过，不影响其他版本
                continue

        if not versions:
            continue

        # 最新版本信息
        latest = versions[0]
        items.append({
            "video_name": output_subdir.name,
            "output_dir": str(output_subdir),
            "clips_dir": latest["clips_dir"],
            "created_at": latest["created_at"],
            "version_count": len(versions),
            "versions": versions,
        })
    return jsonify({"items": items})


@app.route("/api/history/delete", methods=["POST"])
def api_history_delete():
    """删除历史任务接口：递归删除指定输出目录。"""
    data = request.get_json(silent=True) or {}
    path_str = data.get("path")
    if not path_str:
        return jsonify({"error": "缺少路径参数"}), 400
    target = _validate_output_path(path_str)
    if not target.exists() or not target.is_dir():
        return jsonify({"error": "目录不存在"}), 404
    try:
        shutil.rmtree(target)
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": f"删除失败: {str(e)}"}), 500


@app.route("/api/history/config", methods=["POST"])
def api_history_config():
    """获取历史任务配置接口：返回原视频路径和参数，用于填充重剪表单。兼容旧版本无config、无clips软链接的情况。"""
    data = request.get_json(silent=True) or {}
    path_str = data.get("path")
    if not path_str:
        return jsonify({"error": "缺少路径参数"}), 400
    target = _validate_output_path(path_str)
    # 查找所有clips_开头的真实时间戳目录，取最新的manifest，跳过软链接/联接
    manifest = None
    clips_dirs = []
    for d in target.iterdir():
        try:
            # 跳过重解析点（软链接/目录联接）
            if d.is_symlink() or (d.is_dir() and hasattr(d.stat(), 'st_file_attributes') and (d.stat().st_file_attributes & 0x400)):
                continue
            if d.is_dir() and d.name.startswith("clips_"):
                mp = d / "manifest.json"
                if mp.exists():
                    clips_dirs.append((d.stat().st_mtime, mp, d))
        except Exception:
            continue
    if not clips_dirs:
        return jsonify({"error": "任务清单不存在，无法获取配置"}), 404
    # 取最新修改的manifest
    clips_dirs.sort(reverse=True, key=lambda x: x[0])
    _, manifest_path, _ = clips_dirs[0]
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        video_str = manifest.get("video")
        # 旧版本无config字段，用默认值填充
        config = manifest.get("config", {
            "mode": "auto",
            "subtitles": False,
            "preview": False,
            "grade": "auto",
            "min_duration": 30.0,
            "max_duration": 300.0,
        })
        # 兼容旧版本无mode字段
        if "mode" not in config:
            config["mode"] = manifest.get("mode", "auto")
        if not video_str:
            return jsonify({"error": "清单中未找到原视频路径"}), 400
        video_path = Path(video_str).resolve()
        if not video_path.exists() or not video_path.is_file():
            return jsonify({"error": f"原视频不存在: {video_path.name}，可能已被移动或删除"}), 400
        # 探测视频信息用于填充选中状态
        info = _probe_video_info(video_path)
        info["duration_str"] = _fmt_dur(info["duration_s"])
        info["stem"] = video_path.stem
        return jsonify({
            "ok": True,
            "video": info,
            "config": config,
        })
    except Exception as e:
        return jsonify({"error": f"获取配置失败: {str(e)}"}), 500


@app.route("/api/reset", methods=["POST"])
def api_reset():
    """强制重置任务状态接口：清除卡住的任务，允许启动新任务。"""
    jm.reset()
    return jsonify({"ok": True})


@app.route("/api/open-folder", methods=["POST"])
def api_open_folder():
    """打开输出目录接口：跨平台调用系统文件管理器打开指定目录。"""
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
            # 显式调用explorer打开，比os.startfile更可靠，避免后台进程不弹窗
            subprocess.Popen(["explorer", str(p)])
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(p)]) # Mac访达
        else:
            subprocess.Popen(["xdg-open", str(p)]) # Linux文件管理器
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


def main():
    """启动Web服务，延迟1.5秒自动打开浏览器。"""
    import threading
    import webbrowser

    # 启动时强制重置任务状态，避免服务异常退出后残留任务锁
    jm.reset()

    host = "127.0.0.1"
    port = 5876
    url = f"http://localhost:{port}"
    print(f"智能直播切片 Web UI 启动中…")
    print(f"  地址: {url}")
    print(f"  项目目录: {PROJECT_ROOT}")
    print(f"  关闭此窗口即停止服务")
    print()

    # 延迟打开浏览器，等待Flask服务就绪
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