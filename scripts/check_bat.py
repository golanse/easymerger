"""检查所有 .bat 文件是否会被 Windows cmd 正确执行。

为什么需要这个脚本
-------------------
批处理文件有三个"隐形"要求，稍微违反其中一个，
表现就是**双击没反应**（窗口一闪而过、或者只执行了第一行）：

  1. 必须是 **CRLF 换行**。
     纯 LF 的文件，cmd 可能把整个文件当成"一行超长命令"，
     于是 @echo off 之后什么都不做就退出了。
     这是最坑的一条 —— 文件看起来完全正常，用记事本打开也看不出问题。

  2. 必须是 **GBK(ANSI) 编码且无 BOM**。
     UTF-8 无 BOM 时，中文行会被 GBK 解码成乱码指令，cmd 直接终止。

  3. 语法要站得住：goto 的标签得存在、括号得配对、
     for 循环里要用 %% 而不是 %。

这个脚本就是用来在打包前把这三条都验一遍的。

用法
----
    python scripts/check_bat.py            # 检查，有问题就退出码 1
    python scripts/check_bat.py --fix      # 自动修复换行与编码
"""

from __future__ import annotations

import glob
import os
import re
import sys
from typing import List

# 常见问题模式
_CHCP = re.compile(r"\bchcp\b", re.I)
_POWERSHELL = re.compile(r"\bpowershell\b", re.I)
_GOTO = re.compile(r"^\s*goto\s+(\S+)", re.I | re.M)
_LABEL = re.compile(r"^\s*:(\S+)", re.M)
_FOR_SINGLE_PCT = re.compile(r"^\s*for\s+.*(?<!%)%[A-Za-z]\b", re.I | re.M)


def _decode(raw: bytes) -> tuple:
    """尝试用 GBK 解码（Windows 中文环境的 ANSI 代码页）。"""
    try:
        return raw.decode("gbk"), "gbk", None
    except UnicodeDecodeError as exc:
        try:
            return raw.decode("utf-8"), "utf-8", None
        except UnicodeDecodeError:
            return raw.decode("gbk", errors="replace"), "unknown", str(exc)


def check_one(path: str, fix: bool = False) -> List[str]:
    """检查单个 bat 文件，返回问题列表（空列表 = 没问题）。"""
    problems: List[str] = []
    raw = open(path, "rb").read()

    # ---- 1. BOM ----
    if raw.startswith(b"\xef\xbb\xbf"):
        problems.append("有 UTF-8 BOM —— cmd 会把 BOM 当命令，必须去掉")
        if fix:
            raw = raw[3:]

    # ---- 2. 换行符 ----
    crlf = raw.count(b"\r\n")
    total_lf = raw.count(b"\n")
    bare_lf = total_lf - crlf          # 没有前导 CR 的 LF
    bare_cr = raw.count(b"\r") - crlf
    if bare_lf or bare_cr:
        problems.append(
            f"换行符不是纯 CRLF（裸 LF {bare_lf} 处、裸 CR {bare_cr} 处）—— "
            "cmd 可能把整个文件当成一行，表现为双击没反应")
        if fix:
            norm = raw.replace(b"\r\n", b"\n").replace(b"\r", b"\n")
            raw = norm.replace(b"\n", b"\r\n")
            crlf = raw.count(b"\r\n")

    # ---- 3. 编码 ----
    txt, enc, derr = _decode(raw)
    if enc == "utf-8":
        problems.append("是 UTF-8 编码 —— 中文行会被 GBK 解码成乱码指令，"
                        "必须存为 GBK(ANSI)")
        if fix:
            raw = txt.encode("gbk", errors="replace")
            txt = raw.decode("gbk", errors="replace")
    elif enc == "unknown":
        problems.append(f"既不是合法 GBK 也不是合法 UTF-8（{derr}）")

    # ---- 4. goto 标签 ----
    labels = {m.group(1).rstrip(":").lower() for m in _LABEL.finditer(txt)}
    labels.add("eof")           # goto :eof 是内置的
    for m in _GOTO.finditer(txt):
        target = m.group(1).lstrip(":").lower()
        if target not in labels:
            line_no = txt[:m.start()].count("\n") + 1
            problems.append(f"第 {line_no} 行 goto {target}，但文件里没有 :{target} 标签")

    # ---- 5. 括号配对（忽略注释行）----
    depth = 0
    for i, line in enumerate(txt.splitlines(), 1):
        s = line.strip()
        if not s or s.startswith(("rem", "@rem", "::", "@::")):
            continue
        depth += line.count("(") - line.count(")")
        if depth < 0:
            problems.append(f"第 {i} 行出现多余的右括号")
            depth = 0
    if depth > 0:
        problems.append(f"有 {depth} 个左括号没有闭合")

    # ---- 6. for 循环里的 % ----
    for m in _FOR_SINGLE_PCT.finditer(txt):
        line_no = txt[:m.start()].count("\n") + 1
        problems.append(f"第 {line_no} 行 for 循环用了单个 %（bat 文件里必须写 %%）")

    # ---- 7. 已知会导致提前退出的命令 ----
    if _CHCP.search(txt):
        problems.append("含 chcp 命令 —— 部分 Win10/Win7 上会让批处理提前退出")
    if _POWERSHELL.search(txt):
        problems.append("含 powershell 调用 —— 可能被执行策略拦截而失败")

    # ---- 8. 退出前应有 pause（否则报错一闪而过）----
    lines = [l.strip().lower() for l in txt.splitlines()]
    for i, l in enumerate(lines):
        if re.match(r"^exit\b", l):
            # 往前找最近的 pause
            window = lines[max(0, i - 3):i]
            if not any("pause" in w for w in window):
                problems.append(f"第 {i+1} 行 exit 前没有 pause，报错会一闪而过")

    if fix:
        with open(path, "wb") as f:
            f.write(raw)
    return problems


def main(argv=None) -> int:
    args = list(argv if argv is not None else sys.argv[1:])
    fix = "--fix" in args
    args = [a for a in args if a != "--fix"]

    root = args[0] if args else os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    bats = sorted(glob.glob(os.path.join(root, "*.bat")))
    if not bats:
        print("没有找到 .bat 文件")
        return 0

    print("=" * 62)
    print(f" 批处理文件检查（{'检查并修复' if fix else '仅检查'}）")
    print("=" * 62)

    bad = 0
    for path in bats:
        name = os.path.basename(path)
        problems = check_one(path, fix=fix)
        if problems:
            bad += 1
            print(f"\n✘ {name}")
            for p in problems:
                print(f"    · {p}")
        else:
            print(f"✔ {name}")

    print()
    print("=" * 62)
    if bad:
        print(f" {bad}/{len(bats)} 个文件有问题")
        if not fix:
            print(" 运行 python scripts/check_bat.py --fix 可自动修复换行与编码")
        return 1
    print(f" 全部 {len(bats)} 个 .bat 文件检查通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
