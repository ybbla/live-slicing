"""统一配置加载 —— 从 .env 读取火山 ASR 和 Ark/豆包的凭证。

查找优先级：
  1. LIVESLICING_ENV 环境变量指向的路径
  2. 当前工作目录 .env
  3. 项目根目录（liveslicing 包上一级）.env
"""
from __future__ import annotations

import os
import sys
from pathlib import Path


def _find_dotenv() -> Path | None:
    explicit = os.environ.get("LIVESLICING_ENV", "")
    candidates = []
    if explicit:
        candidates.append(Path(explicit))
    candidates.append(Path.cwd() / ".env")
    candidates.append(Path(__file__).resolve().parent.parent / ".env")
    for c in candidates:
        if c.is_file():
            return c
    return None


def _parse_dotenv(path: Path) -> dict[str, str]:
    env: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        env[k.strip()] = v.strip().strip('"').strip("'")
    return env


def load_env() -> dict[str, str]:
    """加载 .env 并 setdefault 到 os.environ，返回合并后的环境 dict。"""
    env_path = _find_dotenv()
    file_env: dict[str, str] = {}
    if env_path:
        file_env = _parse_dotenv(env_path)
    for k, v in file_env.items():
        os.environ.setdefault(k, v)
    return dict(os.environ)


def volc_app_key() -> str:
    """返回火山 ASR APP Key，缺失则 sys.exit。"""
    load_env()
    v = os.environ.get("VOLC_APP_KEY", "")
    if not v:
        sys.exit(
            "VOLC_APP_KEY not found in .env or environment（火山大模型录音文件识别 APP Key）\n"
            "请在项目根目录 .env 文件中配置 VOLC_APP_KEY=xxx"
        )
    return v


def ark_config() -> tuple[str, str, str]:
    """返回 (api_key, base_url, model)，api_key 缺失则 sys.exit。"""
    load_env()
    key = os.environ.get("ARK_API_KEY", "")
    url = os.environ.get("ARK_BASE_URL", "https://ark.cn-beijing.volces.com/api/v3")
    model = os.environ.get("ARK_MODEL", "")
    if not key:
        sys.exit(
            "ARK_API_KEY not found in .env or environment（火山方舟 Ark API Key）\n"
            "请在项目根目录 .env 文件中配置 ARK_API_KEY=xxx"
        )
    return key, url, model
