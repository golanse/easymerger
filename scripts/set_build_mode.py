"""切换 build.spec 的打包模式（替代 PowerShell，避免编码/执行策略问题）。

    python scripts/set_build_mode.py onedir    # 文件夹版（默认）
    python scripts/set_build_mode.py onefile   # 单文件版
    python scripts/set_build_mode.py           # 查看当前模式
"""

from __future__ import annotations

import io
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SPEC = os.path.join(ROOT, "build.spec")


def read_text(path: str) -> str:
    """按 UTF-8 读取（带 BOM 也能处理）。"""
    with open(path, "rb") as f:
        raw = f.read()
    for enc in ("utf-8-sig", "utf-8", "gbk"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def write_text(path: str, text: str) -> None:
    """统一写成 UTF-8 无 BOM（PyInstaller 能正常读取）。"""
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)


def current_mode(text: str) -> str:
    for line in text.splitlines():
        if line.startswith("ONEFILE"):
            return "onefile" if "True" in line else "onedir"
    return "onedir"


def set_mode(text: str, mode: str) -> str:
    target = "ONEFILE = True" if mode == "onefile" else "ONEFILE = False"
    out = []
    replaced = False
    for line in text.splitlines():
        if line.startswith("ONEFILE"):
            out.append(target)
            replaced = True
        else:
            out.append(line)
    if not replaced:
        # spec 里没有这行就插到开头
        out.insert(0, target)
    return "\n".join(out) + "\n"


def main() -> int:
    if not os.path.isfile(SPEC):
        print(f"[错误] 找不到 build.spec：{SPEC}")
        return 1

    text = read_text(SPEC)
    mode = sys.argv[1].lower() if len(sys.argv) > 1 else ""

    if mode not in ("", "onedir", "dir", "folder", "onefile", "single"):
        print(f"[错误] 未知模式：{mode}（支持 onedir / onefile）")
        return 1

    if not mode:
        cur = current_mode(text)
        print("onefile" if cur == "onefile" else "onedir")
        return 0

    norm = "onefile" if mode in ("onefile", "single") else "onedir"
    new_text = set_mode(text, norm)
    if new_text != text:
        write_text(SPEC, new_text)
    print(f"[√] 打包模式已设为：{norm}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
