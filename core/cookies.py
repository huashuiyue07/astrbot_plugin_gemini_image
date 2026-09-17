"""复用 gemini-webapi 的 Cookie 缓存。

背景：gemini-webapi 的 `auto_refresh` 会把自动刷新后的 `__Secure-1PSIDTS`
写进 `GEMINI_COOKIE_PATH/.cached_cookies_<1PSID>.json`，但**只写不读**——
进程重启后它仍然使用配置里那份（可能早已过期的）值。

因此这里补上「读回」这一步：优先使用缓存里较新的 1PSIDTS，
让长时间运行的机器人在重启后不必重新抓 Cookie。
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path


def default_cookie_dir() -> Path:
    """与 gemini-webapi 保持一致的默认缓存目录。"""
    env_path = os.getenv("GEMINI_COOKIE_PATH")
    if env_path:
        return Path(env_path)
    return Path(tempfile.gettempdir()) / "gemini_webapi"


def cache_file_path(secure_1psid: str, cookie_dir: str | Path | None = None) -> Path:
    """缓存文件路径，命名规则与 gemini-webapi 内部实现一致。"""
    base = Path(cookie_dir) if cookie_dir else default_cookie_dir()
    return base / f".cached_cookies_{secure_1psid}.json"


def load_cached_cookies(
    secure_1psid: str,
    cookie_dir: str | Path | None = None,
) -> dict[str, str]:
    """读取缓存文件，返回 `{cookie_name: value}`。

    任何异常（文件不存在、JSON 损坏、权限不足）都视为「没有缓存」，
    绝不能让一个可有可无的优化把生图流程搞挂。
    """
    if not secure_1psid:
        return {}

    path = cache_file_path(secure_1psid, cookie_dir)
    try:
        raw = path.read_text(encoding="utf-8")
        items = json.loads(raw)
    except (OSError, ValueError):
        return {}

    if not isinstance(items, list):
        return {}

    result: dict[str, str] = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        name = item.get("name")
        value = item.get("value")
        if isinstance(name, str) and isinstance(value, str):
            result[name] = value
    return result


def load_cached_1psidts(
    secure_1psid: str,
    cookie_dir: str | Path | None = None,
) -> str | None:
    """只取缓存里的 `__Secure-1PSIDTS`，取不到返回 None。"""
    return load_cached_cookies(secure_1psid, cookie_dir).get("__Secure-1PSIDTS")
