#!/usr/bin/env bash
# 打包产物的便捷启动脚本（Linux）
# 有些发行版缺 Qt 运行时库，这里统一处理一下，避免双击没反应。
cd "$(dirname "$0")"
BIN="./easymerger"

if [ ! -x "$BIN" ]; then
    echo "[×] 找不到可执行文件：$BIN"
    exit 1
fi

# 缺 libxcb-cursor 时 Qt 6 会静默失败（表现为"双击没反应"），
# 这里给出明确提示，而不是让用户对着空白发呆。
if ! ldd "$BIN" 2>/dev/null | grep -q "not found" ; then
    exec "$BIN"
fi

echo "[!] 检测到缺少运行时库："
ldd "$BIN" | grep "not found" | sed 's/^/    /'
echo
echo "请按你的发行版安装（任选其一）："
echo "  Debian/Ubuntu : sudo apt install libgl1 libglib2.0-0 libfontconfig1 libxcb-cursor0 libxcb-icccm4 libxcb-image0 libxcb-keysyms1 libxcb-randr0 libxcb-render-util0 libxcb-shape0 libxcb-xinerama0"
echo "  Fedora/RHEL   : sudo dnf install mesa-libGL glib2 fontconfig libxcb-cursor libxcb-icccm libxcb-image libxcb-keysyms libxcb-randr libxcb-render-util libxcb-shape libxcb-xinerama"
echo "  Arch          : sudo pacman -S mesa glib2 fontconfig libxcb-cursor libxcb-icccm libxcb-image libxcb-keysyms libxcb-randr libxcb-render-util libxcb-shape libxcb-xinerama"
echo
read -r -p "仍要尝试启动吗？[y/N] " ans
[ "$ans" = "y" ] || [ "$ans" = "Y" ] && exec "$BIN"
