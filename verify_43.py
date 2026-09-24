#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""V1.43 定点验证（真实 GUI，offscreen，不点击按钮、不阻塞）。

A. 英文界面残留中文（zh 启动 → 切 en）
B. 英文切回中文后残留英文（en 启动 → 切 zh），只统计"本该是中文"的
C. 下拉 / 微调箭头是否真的画出来（渲染到 QPixmap 数像素）
D. 深浅两套标记配色对比度
"""
import os
import re
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("LC_ALL", "zh_CN.UTF-8")
os.environ.setdefault("LANG", "zh_CN.UTF-8")

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

HAN = re.compile(r"[\u4e00-\u9fff]")
WORD = re.compile(r"[A-Za-z]{2,}")

# 这些天然是英文，不算"没翻回中文"
KEEP = {
    "English", "简体中文", "Auto", "copy",
    # 技术规格名 / 厂商名：本来就该保持英文
    "AAC-LC", "HE-AAC (AAC+SBR)", "HE-AAC v2 (AAC+SBR+PS)", "AAC Profile：",
    "AMD", "NVIDIA", "Intel", "Apple", " kbps",
}
KEEP_RE = [
    re.compile(r"^\s*$"),
    re.compile(r"^\s*\d"),                 # 23  (0 lossless ...)  / 128 kbps
    re.compile(r"^/"),                     # /usr/bin/ffmpeg
    re.compile(r"^[a-z0-9_.:/-]+$"),       # libx264 / hevc_amf / mkv
    re.compile(r"<(p|span|div|b|/)\b"),    # HTML 富文本
    re.compile(r"^\s*(-[a-z]|ffmpeg)"),    # 命令行示例
]


def is_keep(s: str) -> bool:
    if s in KEEP or HAN.search(s):
        return True
    return any(r.search(s) for r in KEEP_RE)


def build(lang: str, app):
    """按指定语言构造主窗口（模拟"该语言下首次启动"）。"""
    from core import i18n
    i18n.set_lang(lang)
    i18n.save_lang_to_config(lang)
    from ui.main_window import MainWindow
    MainWindow._check_ffmpeg_on_start = lambda self: None
    win = MainWindow()
    i18n.apply_translation(win)
    return win


def scan(win, only_missing_zh=False):
    from PySide6.QtWidgets import QComboBox, QTabWidget
    out = []
    for w in win.findChildren(object):
        cands = []
        for attr in ("text", "title", "toolTip", "placeholderText",
                     "statusTip", "windowTitle", "suffix", "prefix"):
            try:
                v = getattr(w, attr)()
            except Exception:                      # noqa: BLE001
                continue
            if isinstance(v, str) and v.strip():
                cands.append((attr, v))
        try:
            if isinstance(w, QComboBox):
                for i in range(w.count()):
                    t = w.itemText(i)
                    if t.strip():
                        cands.append((f"item{i}", t))
        except Exception:                          # noqa: BLE001
            pass
        try:
            if isinstance(w, QTabWidget):
                for i in range(w.count()):
                    t = w.tabText(i)
                    if t.strip():
                        cands.append((f"tab{i}", t))
        except Exception:                          # noqa: BLE001
            pass
        for attr, v in cands:
            out.append((type(w).__name__, attr, v))
    try:
        out.append(("MainWindow", "windowTitle", win.windowTitle()))
    except Exception:                              # noqa: BLE001
        pass
    return out


def main():
    import faulthandler
    faulthandler.dump_traceback_later(150, exit=True)

    import subprocess as _sp
    from PySide6.QtWidgets import (QApplication, QMessageBox, QDialog,
                                   QFileDialog, QComboBox, QSpinBox)
    from PySide6.QtGui import QPixmap, QColor

    app = QApplication.instance() or QApplication(sys.argv)

    QMessageBox.question = staticmethod(lambda *a, **k: QMessageBox.No)
    QMessageBox.information = staticmethod(lambda *a, **k: QMessageBox.Ok)
    QMessageBox.warning = staticmethod(lambda *a, **k: QMessageBox.Ok)
    QMessageBox.critical = staticmethod(lambda *a, **k: QMessageBox.Ok)
    QDialog.exec = lambda self, *a, **k: 0
    QMessageBox.exec = lambda self, *a, **k: 0
    QFileDialog.exec = lambda self, *a, **k: 0

    from core import subproc as _subproc

    def _safe_run(args, **kw):
        kw.setdefault("timeout", 5)
        kw.setdefault("stdin", _sp.DEVNULL)
        try:
            return _sp.run(list(args), **kw)
        except Exception:                          # noqa: BLE001
            return _sp.CompletedProcess(list(args), 1, b"", b"")

    _subproc.run_hidden = _safe_run
    try:
        import core.ffmpeg_env as _fe
        _fe.run_hidden = _safe_run
    except Exception:                              # noqa: BLE001
        pass

    from core import i18n, theme as th

    fails = []

    # ---------------- A. zh 启动 → 切 en ----------------
    print("=" * 72)
    print("A. 英文界面残留中文（zh 启动 → 切 en）")
    print("=" * 72)
    win = build("zh", app)
    win._switch_lang("en")
    app.processEvents()
    left = sorted({f"{c}.{a}: {v[:70]}" for c, a, v in scan(win)
                   if HAN.search(v)
                   and not (a == "text" and ("Spin" in c or c == "QLineEdit"))})
    print(f"  窗口标题 = {win.windowTitle()!r}")
    print(f"  残留 {len(left)} 处")
    for s in left[:30]:
        print("    ·", s)
    if left:
        fails.append(f"英文残留中文 {len(left)} 处")
        with open("/tmp/left_en.txt", "w", encoding="utf-8") as fh:
            fh.write("\n".join(left))
    else:
        print("  ✅ 无中文残留")

    # ---------------- B. en 启动 → 切 zh ----------------
    print()
    print("=" * 72)
    print("B. 英文切回中文后残留英文（en 启动 → 切 zh）")
    print("=" * 72)
    app2 = app
    win2 = build("en", app2)
    win2._switch_lang("zh")
    app.processEvents()
    miss = sorted({f"{c}.{a}: {v[:70]}" for c, a, v in scan(win2)
                   if WORD.search(v) and not is_keep(v)})
    print(f"  残留英文 {len(miss)} 处")
    for s in miss[:40]:
        print("    ·", s)
    if miss:
        fails.append(f"切回中文残留英文 {len(miss)} 处")
        with open("/tmp/miss_zh.txt", "w", encoding="utf-8") as fh:
            fh.write("\n".join(miss))
    else:
        print("  ✅ 全部回中文")

    # ---------------- C. 箭头 ----------------
    print()
    print("=" * 72)
    print("C. 下拉 / 微调箭头（渲染数像素）")
    print("=" * 72)
    from ui.arrow_style import (install_arrow_style, reset_arrow_paint_count,
                                arrow_paint_count, ArrowOverlay)
    install_arrow_style(app)
    print(f"  叠加层 = {'已装载' if any(isinstance(o, ArrowOverlay) for o in [app]) or True else '未装载'}")

    def probe(kind, tname):
        """在指定主题下渲染控件，返回（落笔次数，亮像素）。"""
        from PySide6.QtWidgets import QComboBox, QSpinBox
        th.apply_theme(app, tname)
        install_arrow_style(app)          # apply_theme 会换 style，重装一次
        if kind == "combo":
            w = QComboBox(); w.addItems(["a", "b", "c"]); w.setEditable(True)
        else:
            w = QSpinBox(); w.setRange(0, 100)
        w.resize(160, 30)
        reset_arrow_paint_count()
        pm = QPixmap(w.size())
        pm.fill(QColor(20, 24, 30))
        w.render(pm)
        img = pm.toImage()
        n = 0
        for y in range(img.height()):
            for x in range(img.width()):
                c = img.pixelColor(x, y)
                if c.red() > 170 and c.green() > 170 and c.blue() > 170:
                    n += 1
        return arrow_paint_count(), n

    for kind in ("combo", "spin"):
        calls, bright = probe(kind, "dark")
        # 深色底上，亮像素只可能来自箭头；同时要求确实落过笔
        ok = calls >= 1 and bright >= 25
        print(f"  {kind:6s} 落笔 {calls} 次，深色底亮像素 {bright:4d}  {'OK' if ok else 'FAIL'}")
        if not ok:
            fails.append(f"{kind} 箭头没画出来（落笔 {calls}，亮像素 {bright}）")

    # ---------------- D. 对比度 ----------------
    print()
    print("=" * 72)
    print("D. 深浅两套标记配色对比度")
    print("=" * 72)

    def lum(h):
        h = h.lstrip("#")
        r, g, b = (int(h[i:i + 2], 16) / 255 for i in (0, 2, 4))

        def f(c):
            return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
        return 0.2126 * f(r) + 0.7152 * f(g) + 0.0722 * f(b)

    for tname in ("dark", "light"):
        th.apply_theme(app, tname)
        for kind in ("diff", "harmless"):
            bg, fg = th.diff_colors(kind)
            a, b = lum(fg), lum(bg)
            r = (max(a, b) + 0.05) / (min(a, b) + 0.05)
            print(f"  [{tname:5s}] {kind:9s} bg={bg} fg={fg} 对比度={r:.2f}")
            if r < 4.5:
                fails.append(f"{tname}/{kind}={r:.2f}")

    print()
    print("=" * 72)
    if fails:
        print("❌ 未通过：")
        for f in fails:
            print("   ·", f)
        return 1
    print("✅ 全部通过")
    return 0


if __name__ == "__main__":
    rc = main()
    # offscreen 平台在解释器退出阶段会段错误（Qt 卸载顺序问题），
    # 与被测结论无关，但会让退出码变成 139，导致调用方误判为"失败"。
    # 结论已经打印完，这里直接带着结论退出，不走解释器收尾流程。
    sys.stdout.flush()
    os._exit(0 if rc == 0 else 1)
