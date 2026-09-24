#!/usr/bin/env bash
# ============================================================
#  EasyMerger —— macOS / Linux 打包脚本
#
#  用法：
#     bash scripts/build_unix.sh              # 文件夹版（推荐）
#     bash scripts/build_unix.sh onefile      # 单文件版
#     bash scripts/build_unix.sh dir  --clean # 清理后重新打包
#
#  重要：PyInstaller **不支持交叉编译**。
#  必须在目标平台上运行 —— 在 Mac 上跑得到 .app，在 Linux 上跑得到可执行
#  文件；在 Linux 上打包不出 Windows 的 exe，反之亦然。
# ============================================================
set -euo pipefail

cd "$(dirname "$0")/.."          # 切到项目根目录
ROOT="$(pwd)"

MODE="${1:-dir}"                 # dir | onefile
CLEAN="${2:-}"

echo "=============================================="
echo " EasyMerger 打包（$(uname -s)）"
echo "=============================================="
echo "  目录：$ROOT"
echo "  模式：$MODE"
echo

# ---------- 1. 检查 Python ----------
PYTHON=""
for c in python3 python; do
    if command -v "$c" >/dev/null 2>&1; then
        PYTHON="$c"
        break
    fi
done
if [ -z "$PYTHON" ]; then
    echo "[×] 没找到 python3，请先安装 Python 3.9 以上版本"
    exit 1
fi
echo "[1/5] Python：$($PYTHON -V 2>&1)  ($PYTHON)"

# ---------- 2. 装依赖 ----------
echo
echo "[2/5] 检查依赖…"
if ! "$PYTHON" -c "import PySide6" >/dev/null 2>&1; then
    echo "      安装 PySide6…"
    "$PYTHON" -m pip install --user -q PySide6-Essentials \
        || "$PYTHON" -m pip install -q PySide6-Essentials
fi
if ! "$PYTHON" -c "import PyInstaller" >/dev/null 2>&1; then
    echo "      安装 PyInstaller…"
    "$PYTHON" -m pip install --user -q pyinstaller \
        || "$PYTHON" -m pip install -q pyinstaller
fi
echo "      PySide6 / PyInstaller 就绪"

# ---------- 3. 准备 ffmpeg ----------
echo
echo "[3/5] 准备 ffmpeg 组件…"
"$PYTHON" scripts/prepare_ffmpeg.py || {
    echo "[×] ffmpeg 组件准备失败"
    echo "    也可手动下载后放进 vendor/ffmpeg/bin/ 再重跑本脚本"
    exit 1
}

# ---------- 4. 设打包模式 ----------
echo
echo "[4/5] 设置打包模式…"
"$PYTHON" scripts/set_build_mode.py "$MODE"

if [ "$CLEAN" = "--clean" ]; then
    echo "      清理旧产物…"
    rm -rf build dist
fi

# ---------- 5. 打包 ----------
echo
echo "[5/5] 开始打包（PyInstaller，约 1~3 分钟）…"
"$PYTHON" -m PyInstaller --noconfirm --clean build.spec

echo
echo "=============================================="
if [ "$(uname -s)" = "Darwin" ]; then
    APP="dist/easymerger.app"
    if [ -d "$APP" ]; then
        echo " ✅ 打包完成：$APP"
        echo
        echo " 运行方式："
        echo "   open \"$APP\""
        echo "   或把它拖进「应用程序」文件夹"
        echo
        echo " 首次运行若被 Gatekeeper 拦截（"已损坏，无法打开"）："
        echo "   sudo xattr -rd com.apple.quarantine \"$APP\""
        echo "   （或用「系统设置 → 隐私与安全性 → 仍要打开」）"
        echo "   这不是软件有问题，而是没有苹果开发者签名。"
    fi
elif [ -d "dist/easymerger" ]; then
    echo " ✅ 打包完成：dist/easymerger/"
    echo
    echo " 运行方式："
    echo "   ./dist/easymerger/easymerger"
    echo
    echo " 如果报缺少 libGL 之类："
    echo "   sudo apt install libgl1 libglib2.0-0 libfontconfig1 libxcb-cursor0   # Debian/Ubuntu"
    echo "   sudo dnf install mesa-libGL glib2 fontconfig libxcb-cursor0          # Fedora"
fi

if [ "$MODE" = "onefile" ]; then
    echo
    echo " 单文件版产物在 dist/ 下（启动会慢一些，每次运行都要解压）"
fi
echo "=============================================="
