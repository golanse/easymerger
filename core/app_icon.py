"""应用程序图标的查找与加载。

为什么需要这个模块
-------------------
很多人以为 PyInstaller 的 `--icon app.ico` 就完事了，
结果打包出来发现：**窗口左上角还是 Qt 默认图标，任务栏也是**。

原因是这两者是完全不同的东西：

    --icon / build.spec 的 icon=
        ↓ 只改 **exe 文件本身**的图标
        ↓ 也就是在「资源管理器」里看 easymerger.exe 时显示的那个

    QApplication.setWindowIcon()
        ↓ 才决定 **运行起来之后**的图标
        ↓ 窗口左上角、Alt+Tab、任务栏

所以必须有 `setWindowIcon()`，否则运行时图标永远是 Qt 的默认图标。

Windows 任务栏还有额外一步：它会按 AppUserModelID 把窗口分组，
ID 不同的窗口即使图标一样也会被分到不同组，
而这个组显示的就是窗口图标。所以两件事都要做对。
"""

from __future__ import annotations

import os
import sys
from typing import List, Optional

__all__ = ["icon_search_paths", "resolve_icon_path", "load_app_icon",
           "ICON_CANDIDATES"]


# 可能的图标文件名（按优先级）
ICON_CANDIDATES = (
    "app.ico",      # Windows
    "app.icns",     # macOS
    "app.png",      # 通用兜底
    "icon.ico",
    "icon.png",
)


def _is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def _meipass() -> str:
    """PyInstaller 单文件模式解压到的临时目录。"""
    return str(getattr(sys, "_MEIPASS", "") or "")


def _exe_dir() -> str:
    """exe 所在目录（文件夹模式与单文件模式都适用）。"""
    return os.path.dirname(os.path.abspath(sys.executable))


def _source_dir() -> str:
    """源码目录（未打包时）。"""
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def icon_search_paths() -> List[str]:
    """所有应该查找图标的目录，按优先级排列。

    打包后必须查 `_MEIPASS`（单文件模式）和 exe 同级目录，
    否则图标虽然在源码目录里、打包时也被打进去了，但运行时找不到。
    """
    dirs: List[str] = []
    if _is_frozen():
        mp = _meipass()
        if mp:
            dirs.append(mp)
            dirs.append(os.path.join(mp, "resources"))
        # PyInstaller 6.x 会把数据文件收进 exe 同级的 _internal
        dirs.append(_exe_dir())
        dirs.append(os.path.join(_exe_dir(), "_internal"))
        dirs.append(os.path.join(_exe_dir(), "_internal", "resources"))
    dirs.append(_source_dir())
    dirs.append(os.path.join(_source_dir(), "resources"))

    seen = set()
    out = []
    for d in dirs:
        key = os.path.normcase(os.path.abspath(d))
        if key in seen:
            continue
        seen.add(key)
        out.append(d)
    return out


def resolve_icon_path() -> Optional[str]:
    """找到第一个真实存在的图标文件。"""
    for d in icon_search_paths():
        for name in ICON_CANDIDATES:
            p = os.path.join(d, name)
            if os.path.isfile(p):
                return p
    return None


def load_app_icon():
    """返回一个 QIcon。找不到图标文件时返回空 QIcon（不会抛异常）。

    注意返回的是 QIcon 而不是路径：调用方拿到就能直接用，
    不必关心 QIcon 该怎么构造、路径找不到会怎样。
    """
    from PySide6.QtGui import QIcon

    path = resolve_icon_path()
    if path:
        icon = QIcon(path)
        if not icon.isNull():
            return icon
        # 文件存在但 Qt 读不出来（比如 .icns 在 Windows 上）
        print(f"[图标] 文件存在但无法解析：{path}")
    else:
        print("[图标] 未找到 app.ico / app.icns / app.png，"
              "将使用 Qt 默认图标。把图标文件放到程序目录即可生效。")
    return QIcon()
