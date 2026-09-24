"""轻量配置持久化（JSON，存程序目录；只读时自动回退到用户目录）。"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field, asdict
from typing import Any, Dict

from .ffmpeg_env import user_data_dir

__all__ = ["AppConfig", "load_config", "save_config"]


def _config_path() -> str:
    """配置文件位置：优先 exe 同级（方便整机迁移），不可写则回退用户目录。

    打包成 onefile 时 _MEIPASS 是只读临时目录，所以这里用 user_data_dir()
    （即 exe 所在目录），而不是 app_dir()。
    """
    for base in (user_data_dir(), os.path.expanduser("~")):
        try:
            if os.path.isdir(base) and os.access(base, os.W_OK):
                return os.path.join(base, "config.json")
        except OSError:
            continue
    fallback_dir = os.path.join(os.path.expanduser("~"), ".mp4merger")
    os.makedirs(fallback_dir, exist_ok=True)
    return os.path.join(fallback_dir, "config.json")


@dataclass
class AppConfig:
    ffmpeg_path: str = ""
    ffprobe_path: str = ""
    last_params: Dict[str, Any] = field(default_factory=dict)
    last_output: Dict[str, Any] = field(default_factory=dict)
    recursive_scan: bool = False
    window_geometry: str = ""
    last_input_dir: str = ""
    # 已提示过"PATH 里有更好的 ffmpeg"就不再打扰（用户可能故意用自带的）
    hw_switch_hinted: bool = False

    # 让 FFmpegEnv 能直接用 config.ffmpeg_path
    def save(self) -> str:
        path = _config_path()
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(asdict(self), f, ensure_ascii=False, indent=2)
        except OSError:
            pass
        return path


def load_config() -> AppConfig:
    path = _config_path()
    if not os.path.isfile(path):
        return AppConfig()
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return AppConfig()
    valid = set(AppConfig.__dataclass_fields__)  # type: ignore[attr-defined]
    return AppConfig(**{k: v for k, v in data.items() if k in valid})


def save_config(cfg: AppConfig) -> str:
    return cfg.save()


if __name__ == "__main__":
    c = load_config()
    print(_config_path(), c)
