# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置 —— EasyMerger

用法（在 MP4Merger 目录下执行）：

    # 方式一：文件夹版（推荐，启动快、ffmpeg 可见可替换）
    pyinstaller build.spec

    # 方式二：单文件版（一个 exe，但启动慢、内置目录只读）
    pyinstaller build.spec   # 然后把下面 ONEFILE 改成 True 再跑

打包前请先确认 vendor/ffmpeg/bin/ 下已经有 ffmpeg.exe 和 ffprobe.exe，
没有的话先双击「2_install_ffmpeg.bat」或运行：
    python scripts/prepare_ffmpeg.py
"""

import os
import sys

# ===== 开关：True = 单文件（onefile），False = 文件夹（onedir，推荐）=====
ONEFILE = False

import importlib.util as _ilu
_spec_dir = os.path.abspath(globals().get("SPECPATH") or os.getcwd())  # noqa: F821
_spec = _ilu.spec_from_file_location(
    "app_meta", os.path.join(_spec_dir, "core", "app_meta.py"))
_meta = _ilu.module_from_spec(_spec)
_spec.loader.exec_module(_meta)  # type: ignore[union-attr]
APP_NAME = _meta.APP_NAME

# PyInstaller 用 exec 执行 spec 文件，globals 里没有 __file__，
# 但有 SPECPATH（spec 文件所在目录）；直接 python build.spec 时退回 cwd。
ROOT = os.path.abspath(globals().get("SPECPATH") or os.getcwd())  # noqa: F821

# ---------- 收集要打包进去的 ffmpeg ----------
datas = []
ffmpeg_bin = os.path.join(ROOT, "vendor", "ffmpeg", "bin")
if os.path.isdir(ffmpeg_bin):
    found = [f for f in os.listdir(ffmpeg_bin)
             if f.lower() in ("ffmpeg.exe", "ffprobe.exe", "ffmpeg", "ffprobe")]
    if found:
        # 目标路径 vendor/ffmpeg/bin —— 与 core/ffmpeg_env.py 的查找路径一致
        datas.append((ffmpeg_bin, os.path.join("vendor", "ffmpeg", "bin")))
        print(f"[打包] 已包含 ffmpeg 组件：{', '.join(found)}")
    else:
        print("[警告] vendor/ffmpeg/bin 存在但没有 ffmpeg/ffprobe，打包后软件将找不到组件")
else:
    print("[警告] 未找到 vendor/ffmpeg/bin，请先运行：python scripts/prepare_ffmpeg.py")

# 把图标作为数据文件打进去（供运行时 setWindowIcon 读取）
_icon_added = False
for _name in ("app.ico", "app.icns", "app.png"):
    _p = os.path.join(ROOT, _name)
    if os.path.isfile(_p):
        datas.append((_p, "."))
        print(f"[打包] 已包含图标文件：{_name}（用于运行时窗口图标）")
        _icon_added = True
        break
if not _icon_added:
    print("[警告] 未找到 app.ico —— 打包后窗口/任务栏图标会是 Qt 默认图标")

# ---------- 图标：既要当 exe 图标，也要作为数据文件打进去 ----------
# 光有 icon= 只改了 exe 文件的图标（资源管理器里看到的那个）。
# 运行时窗口左上角 / 任务栏的图标来自 QApplication.setWindowIcon()，
# 而它需要从磁盘读到 app.ico —— 所以必须同时把图标作为数据文件打进去，
# 否则打包后 resolve_icon_path() 找不到文件，图标又变回 Qt 默认的。
# Windows 用 .ico，macOS 用 .icns，Linux 不需要。
IS_WIN = sys.platform.startswith("win")
IS_MAC = sys.platform == "darwin"

icon_path = ""
icon_candidates = ["app.ico", "app.icns"] if IS_WIN else (
    ["app.icns", "app.ico"] if IS_MAC else ["app.ico", "app.icns"])
for cand in icon_candidates + [os.path.join("resources", c) for c in icon_candidates]:
    p = os.path.join(ROOT, cand)
    if os.path.isfile(p):
        # macOS 的 --icon 只接受 .icns，给 .ico 会被忽略并报警告
        if IS_MAC and not p.lower().endswith(".icns"):
            continue
        icon_path = p
        break

# ---------- 不需要的库（显著减小体积）----------
excludes = [
    "tkinter", "unittest", "pydoc", "doctest", "test",
    "numpy", "scipy", "pandas", "matplotlib", "PIL",
    "PySide6.QtWebEngineCore", "PySide6.QtWebEngineWidgets",
    "PySide6.QtWebEngineQuick", "PySide6.QtQuick", "PySide6.QtQuick3D",
    "PySide6.QtQuickWidgets", "PySide6.QtQml", "PySide6.Qt3DCore",
    "PySide6.Qt3DRender", "PySide6.Qt3DInput", "PySide6.Qt3DLogic",
    "PySide6.Qt3DAnimation", "PySide6.Qt3DExtras", "PySide6.QtMultimedia",
    "PySide6.QtMultimediaWidgets", "PySide6.QtCharts", "PySide6.QtDataVisualization",
    "PySide6.QtBluetooth", "PySide6.QtNfc", "PySide6.QtPositioning",
    "PySide6.QtSerialPort", "PySide6.QtSensors", "PySide6.QtTest",
    "PySide6.QtDesigner", "PySide6.QtHelp", "PySide6.QtOpenGL",
    "PySide6.QtOpenGLWidgets", "PySide6.QtPdf", "PySide6.QtPdfWidgets",
    "PySide6.QtSpatialAudio", "PySide6.QtSql", "PySide6.QtNetworkAuth",
    "PySide6.QtRemoteObjects", "PySide6.QtScxml", "PySide6.QtStateMachine",
    "PySide6.QtTextToSpeech", "PySide6.QtUiTools", "PySide6.QtWebChannel",
    "PySide6.QtWebSockets", "PySide6.QtHttpServer", "PySide6.QtSvg",
]

block_cipher = None

a = Analysis(  # noqa: F821
    [os.path.join(ROOT, "main.py")],
    pathex=[ROOT],
    binaries=[],
    datas=datas,
    hiddenimports=[
        "PySide6.QtCore",
        "PySide6.QtGui",
        "PySide6.QtWidgets",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)  # noqa: F821

if ONEFILE:
    exe = EXE(  # noqa: F821
        pyz,
        a.scripts,
        a.binaries,
        a.zipfiles,
        a.datas,
        [],
        name=APP_NAME,
        debug=False,
        bootloader_ignore_signals=False,
        strip=False,
        upx=False,
        upx_exclude=[],
        runtime_tmpdir=None,
        console=False,
        disable_windowed_traceback=False,
        argv_emulation=False,
        target_arch=None,
        codesign_identity=None,
        entitlements_file=None,
        icon=icon_path or None,
    )
else:
    exe = EXE(  # noqa: F821
        pyz,
        a.scripts,
        [],
        exclude_binaries=True,
        name=APP_NAME,
        debug=False,
        bootloader_ignore_signals=False,
        strip=False,
        upx=False,
        console=False,
        disable_windowed_traceback=False,
        argv_emulation=False,
        target_arch=None,
        codesign_identity=None,
        entitlements_file=None,
        icon=icon_path or None,
    )
    coll = COLLECT(  # noqa: F821
        exe,
        a.binaries,
        a.zipfiles,
        a.datas,
        strip=False,
        upx=False,
        upx_exclude=[],
        name=APP_NAME,
    )

    # macOS：onedir 只是个文件夹，双击里面的可执行文件才能启动，
    # 不够"原生的 App 体验"。再包一层 BUNDLE 就生成标准的
    # easymerger.app，可以拖进「应用程序」文件夹、能在 Launchpad 里看到。
    if IS_MAC:
        info_plist = {
            # 声明需要摄像头/麦克风等权限时才用；这里没有，保持最小
            "CFBundleName": _meta.APP_NAME_DISPLAY,
            "CFBundleDisplayName": _meta.APP_NAME_DISPLAY,
            "CFBundleIdentifier": _meta.BUNDLE_ID,
            "CFBundleVersion": _meta.VERSION,
            "CFBundleShortVersionString": _meta.VERSION,
            "NSHighResolutionCapable": True,
            # 允许启动未签名的 ffmpeg 子进程（不设会被 Gatekeeper 拦）
            "NSAppleEventsUsageDescription": "用于调用 ffmpeg 处理视频",
        }
        app = BUNDLE(  # noqa: F821
            coll,
            name=APP_NAME + ".app",
            icon=icon_path or None,
            bundle_identifier=_meta.BUNDLE_ID,
            info_plist=info_plist,
        )
