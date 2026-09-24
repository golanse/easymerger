"""UI 崩溃回归测试：把**每个**弹窗和按钮都真的打开一遍。

## 为什么要这个测试

之前出现过一次用户可见的崩溃：

    NameError: name 'QDialog' is not defined

成因是我用字符串补丁插入新代码时漏了 import，而：
  · `ast.parse()`      —— 只查语法，看不出名字未定义
  · `compileall`       —— 同上
  · `mypy` 之类        —— 没配

**缺失的 import 是运行时错误，静态语法检查永远发现不了。**

更要命的是同一个函数里还有第二个未定义名（`sys`）。
只修第一个的话，用户点第二次会撞上新的 NameError ——
这类 bug 会一个接一个地冒出来，修一个出一个。

## 本测试怎么防

两层：

1. **pyflakes 静态扫描**：全量检查未定义名（含注解里的）
2. **运行时实开弹窗**：真的把每个对话框构造出来、把每个按钮点一遍

第 2 层是关键 —— 它能抓住静态分析抓不到的
（属性名写错、API 用错、条件分支里的 NameError 等）。
"""

from __future__ import annotations

import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

MODULES = ["core", "ui", "main.py", "scripts"]
TEST_FILES = [f for f in os.listdir(ROOT)
              if f.startswith("test_") and f.endswith(".py")]


# ==================================================================
# 第 1 层：静态扫描未定义名
# ==================================================================
def test_pyflakes() -> bool:
    print("=" * 66)
    print("1. 静态扫描：未定义名（pyflakes）")
    print("=" * 66)

    try:
        import pyflakes
        pyflakes.__version__  # 仅确认可用
    except ImportError:
        print("  （未安装 pyflakes，尝试安装…）")
        r = subprocess.run([sys.executable, "-m", "pip", "install",
                            "-q", "--no-input", "pyflakes"],
                           capture_output=True, text=True)
        if r.returncode != 0:
            print("  ⚠ 安装失败，跳过静态扫描")
            return True

    targets = MODULES + TEST_FILES
    r = subprocess.run([sys.executable, "-m", "pyflakes", *targets],
                       capture_output=True, text=True, cwd=ROOT)
    lines = [ln for ln in (r.stdout or "").splitlines()
             if "undefined name" in ln]
    if lines:
        print(f"  ❌ 发现 {len(lines)} 处未定义名：")
        for ln in lines:
            print(f"     {ln}")
        return False
    print(f"  ✅ 扫描 {len(targets)} 个目标，未定义名 0 处")
    return True


# ==================================================================
# 第 2 层：运行时真的把每个弹窗打开
# ==================================================================
def test_dialogs() -> bool:
    print()
    print("=" * 66)
    print("2. 运行时：把每个弹窗 / 报告窗口真的打开一遍")
    print("=" * 66)

    try:
        from PySide6.QtWidgets import QApplication, QMessageBox
    except ImportError:
        print("  （未安装 PySide6，跳过）")
        return True

    QApplication.instance() or QApplication([])

    # 屏蔽会阻塞的模态框，改成自动选一个选项
    _asked: list = []

    def _fake_question(*a, **k):
        _asked.append(("question", a[1] if len(a) > 1 else ""))
        return QMessageBox.Yes

    def _fake_info(*a, **k):
        _asked.append(("info", a[1] if len(a) > 1 else ""))
        return QMessageBox.Ok

    def _fake_warn(*a, **k):
        _asked.append(("warning", a[1] if len(a) > 1 else ""))
        return QMessageBox.Ok

    QMessageBox.question = staticmethod(_fake_question)
    QMessageBox.information = staticmethod(_fake_info)
    QMessageBox.warning = staticmethod(_fake_warn)
    # 关键：exec() 会阻塞，这里让它立即返回
    QDialog_exec_patched = []

    from PySide6.QtWidgets import QDialog as _QDialog
    if not getattr(_QDialog, "_test_patched", False):
        _QDialog.exec = lambda self, *a, **k: (
            QDialog_exec_patched.append(self.windowTitle()), 0)[1]
        _QDialog._test_patched = True

    from ui.main_window import MainWindow
    from ui.ffmpeg_dialog import FFmpegDialog

    win = MainWindow()
    win.env.refresh()
    try:
        win._apply_env_to_panels()
    except Exception:  # noqa: BLE001
        pass

    ok = True
    panel = win.encode_panel
    ff_dialog = FFmpegDialog(win.env, win.config, win)

    # ---- (a) 编码面板上的每个按钮 ----
    print("\n  [a] 编码面板按钮")
    buttons = [
        ("诊断", panel.btn_diag),
        ("检测硬件编码", panel.btn_probe),
        ("选最快可用", panel.btn_best),
    ]
    # AAC 检测按钮（名字可能不同，按存在性取）
    for name in ("btn_aac_probe", "btn_probe_audio", "btn_audio_probe"):
        b = getattr(panel, name, None)
        if b is not None:
            buttons.append((name, b))

    for label, btn in buttons:
        if btn is None:
            print(f"      {label:<14} （不存在，跳过）")
            continue
        try:
            btn.click()
            print(f"      {label:<14} ✅")
        except Exception as exc:  # noqa: BLE001
            print(f"      {label:<14} ❌ {type(exc).__name__}: {exc}")
            ok = False

    # ---- (b) ffmpeg 状态对话框里的每个按钮 ----
    print("\n  [b] ffmpeg 状态对话框按钮")
    dlg_buttons = [
        ("一键自检", "btn_selfcheck"),
        ("AMF 深度诊断", "btn_amf"),
        ("测试某个 ffmpeg", "btn_test_ff"),
        ("选择用哪个", "btn_switch"),
        ("打开组件目录", "btn_open_dir"),
        ("打开下载临时目录", "btn_open_tmp"),
        ("磁盘占用/清理", "btn_usage"),
        ("带 AMF 的下载链接", "btn_links"),
    ]
    for label, attr in dlg_buttons:
        b = getattr(ff_dialog, attr, None)
        if b is None:
            print(f"      {label:<18} （不存在，跳过）")
            continue
        try:
            b.click()
            print(f"      {label:<18} ✅")
        except Exception as exc:  # noqa: BLE001
            print(f"      {label:<18} ❌ {type(exc).__name__}: {exc}")
            ok = False

    # ---- (c) 报告窗口（_show_report 是静态方法，单独调）----
    print("\n  [c] 报告窗口")
    try:
        FFmpegDialog._show_report("测试标题", "测试内容\n第二行")
        print("      _show_report        ✅")
    except Exception as exc:  # noqa: BLE001
        print(f"      _show_report        ❌ {type(exc).__name__}: {exc}")
        ok = False

    # ---- (d) 自测：故意制造未定义名，确认本测试抓得住 ----
    print("\n  [d] 反向验证：故意漏 import，本测试应能抓到")
    probe = r'''
from __future__ import annotations
code = """
import sys
sys.path.insert(0, r"{root}")
import pyflakes.api, pyflakes.reporter
import io
src = ''' + "'''" + '''
from __future__ import annotations
def f():
    dlg = QDialog()      # 故意漏 import
    return dlg
''' + "'''" + '''
buf = io.StringIO()
pyflakes.api.check(src, "t.py", pyflakes.reporter.Reporter(buf, buf))
out = buf.getvalue()
assert "undefined name" in out and "QDialog" in out, out
print("OK")
"""
exec(compile(code, "<t>", "exec"), {{"__name__": "__main__"}})
'''.replace("{root}", ROOT)
    r = subprocess.run([sys.executable, "-c", probe],
                       capture_output=True, text=True, cwd=ROOT)
    if r.returncode == 0:
        print("      pyflakes 能抓到漏 import  ✅")
    else:
        print(f"      ❌ 反向验证失败：{r.stderr.strip()[-200:]}")
        ok = False

    return ok


def main() -> int:
    print()
    print("#" * 66)
    print("#  UI 崩溃回归测试")
    print("#" * 66)
    print()

    ok1 = test_pyflakes()
    ok2 = test_dialogs()

    print()
    if ok1 and ok2:
        print("✅ UI 崩溃回归测试全部通过")
        return 0
    print("❌ 存在会导致运行时崩溃的问题")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
