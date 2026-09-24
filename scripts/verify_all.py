#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""打包前的**真实**校验——任何一项失败都必须退出码非 0。

为什么需要它：之前几轮用
    python3 -m pyflakes ... || echo "静态扫描 0 处 ✅"
pyflakes 没装时命令直接失败，却被 || 分支打印成"通过"。
于是漏 import 一路带进了发布包（QDialog / QApplication 都因此崩过）。

本脚本的原则：不吞任何失败。工具不存在 → 直接报错退出。
"""
import subprocess
import sys
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TARGETS = ["core", "ui", "main.py", "scripts"]
TARGETS += [f for f in os.listdir(ROOT)
            if f.startswith("test_") and f.endswith(".py")]

fails = []


def run(desc, cmd, allow_fallback=False):
    """跑一步检查。

    :param allow_fallback: 为 True 时，工具缺失**返回 None** 交给调用方
        用别的手段兜底；但仍然绝不允许"什么都不做就通过"。
        为 False（默认）时，工具缺失直接判失败。
    """
    print(f"\n── {desc}")
    r = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True)
    out = (r.stdout or "") + (r.stderr or "")
    if r.returncode == 127 or "No module named" in out:
        if allow_fallback:
            print("  ⚠ 工具不可用，交兜底：" + out.strip()[:200])
            return None
        print("  ❌ 工具不可用（不能当作通过）：")
        print("    " + out.strip()[:300])
        fails.append(f"{desc}: 工具缺失")
        return ""
    print("  " + (out.strip()[:600] or "(无输出)"))
    return out


# 1) 语法：必须能编译
r = subprocess.run([sys.executable, "-m", "compileall", "-q"] + TARGETS,
                   cwd=ROOT, capture_output=True, text=True)
print("── 语法编译")
if r.returncode != 0:
    print("  ❌", r.stderr[:300]); fails.append("语法编译失败")
else:
    print("  ✅ 全部可编译")

# 2) 未定义名（真·静态扫描）
#    pyflakes 优先；但**没装时不能当作通过**，改用内置检查器兜底。
#    教训：`pyflakes ... || echo "0 处 ✅"` 曾把"工具缺失"报成通过，
#    漏 import 因此一路带进发布包（QDialog / QApplication 都这么崩过）。
out = run("pyflakes 未定义名",
          [sys.executable, "-m", "pyflakes"] + TARGETS,
          allow_fallback=True)
if out is None:
    print("  → 改用内置检查器兜底（绝不跳过）")
    r2 = subprocess.run([sys.executable, "scripts/check_undefined.py"],
                        cwd=ROOT, capture_output=True, text=True)
    print("  " + (r2.stdout or r2.stderr).strip()[-300:])
    if r2.returncode != 0:
        fails.append("未定义名（内置检查）")
else:
    bad = [l for l in out.splitlines() if "undefined name" in l]
    if bad:
        print(f"  ❌ {len(bad)} 处未定义名"); fails.append(f"未定义名 {len(bad)}")
    else:
        print("  ✅ 0 处未定义名")

# 3) .bat 完整性
r = subprocess.run([sys.executable, "scripts/check_bat.py"],
                   cwd=ROOT, capture_output=True, text=True)
print("── .bat 完整性")
print("  " + (r.stdout or r.stderr).strip()[-200:])
if r.returncode != 0:
    fails.append("bat 检查失败")

print("\n" + "=" * 60)
if fails:
    print("❌ 校验未通过：")
    for f in fails:
        print("   ·", f)
    sys.exit(1)
print("✅ 全部校验通过")
sys.exit(0)
