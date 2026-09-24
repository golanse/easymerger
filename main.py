"""EasyMerger —— 程序入口。

用法：
    python main.py
打包（Windows）：
    pyinstaller --noconsole --name "easymerger" --icon app.ico main.py
"""

from __future__ import annotations

import os
import sys
import traceback

# 让「以脚本方式直接运行」和「打包后运行」都能 import 到 core / ui
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtWidgets import QApplication, QMessageBox  # noqa: E402


def _enable_high_dpi() -> None:
    """高 DPI 适配。

    Qt 6 已默认开启高 DPI 缩放，AA_EnableHighDpiScaling / AA_UseHighDpiPixmaps
    被标记为废弃，这里只在 Qt 5 环境下调用，避免打包后控制台刷警告。
    """
    try:
        from PySide6 import __version__ as pyside_version
        major = int(str(pyside_version).split(".")[0])
    except Exception:  # noqa: BLE001
        major = 6
    try:
        QApplication.setHighDpiScaleFactorRoundingPolicy(
            Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)
        if major < 6:
            QApplication.setAttribute(Qt.AA_EnableHighDpiScaling, True)
            QApplication.setAttribute(Qt.AA_UseHighDpiPixmaps, True)
    except Exception:  # noqa: BLE001
        pass


def _set_app_user_model_id() -> None:
    """Windows：给任务栏一个独立的分组 ID。

    为什么必须做：Windows 任务栏是按 AppUserModelID 把窗口分组的。
    不设置的话，程序会被归到 Python 那一组，
    于是**任务栏显示的图标是 Python 的，而不是我们自己的** ——
    哪怕窗口图标已经设置正确了。
    """
    if sys.platform.startswith("win"):
        try:
            import ctypes
            from core.app_meta import BUNDLE_ID, VERSION
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
                f"{BUNDLE_ID}.{VERSION}")
        except Exception:  # noqa: BLE001
            pass


def _set_window_icon(app) -> None:
    """设置运行时窗口图标（窗口左上角 / Alt+Tab / 任务栏）。

    关键点：PyInstaller 的 `--icon` **只改 exe 文件本身的图标**
    （资源管理器里看到的那个），不会改程序运行起来之后的图标。
    要想窗口左上角和任务栏变样，必须调用 setWindowIcon()。
    少了这一步，就是"加了 app.ico 但图标没变"的原因。
    """
    try:
        from core.app_icon import load_app_icon
        icon = load_app_icon()
        if not icon.isNull():
            app.setWindowIcon(icon)
            return
    except Exception as exc:  # noqa: BLE001
        print(f"[图标] 设置失败：{exc}")
    # 兜底：图标缺失不该影响软件启动


def _excepthook(exc_type, exc, tb) -> None:
    text = "".join(traceback.format_exception(exc_type, exc, tb))
    print(text, file=sys.stderr)
    try:
        QMessageBox.critical(None, "程序异常",
                             f"发生了未捕获的错误：\n{exc}\n\n详细信息已打印到控制台。\n\n{text[-1500:]}")
    except Exception:  # noqa: BLE001
        pass


def main() -> int:
    _enable_high_dpi()
    app = QApplication(sys.argv)
    from core.app_meta import APP_NAME, APP_NAME_DISPLAY, ORG_NAME, VERSION
    app.setApplicationName(APP_NAME)
    app.setApplicationDisplayName(APP_NAME_DISPLAY)
    app.setApplicationVersion(VERSION)
    app.setOrganizationName(ORG_NAME)
    app.setStyle("Fusion")
    # 装箭头样式（apply_theme 每次也会重裝，这里保证首屏就有）
    try:
        from ui.arrow_style import install_arrow_style
        install_arrow_style(app)
    except Exception:  # noqa: BLE001
        pass
    # 顺序有讲究：AppUserModelID 要在窗口创建前设，图标要在 QApplication
    # 建好后尽早设 —— 两者缺一，Windows 任务栏图标就不会变成我们自己的。
    _set_app_user_model_id()
    _set_window_icon(app)
    sys.excepthook = _excepthook

    from ui.main_window import MainWindow

    win = MainWindow()
    win.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
