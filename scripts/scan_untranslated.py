#!/usr/bin/env python3
"""扫描 UI 中所有仍为中文的文本（切到英文后）。

真正启动 Qt（offscreen），构造主窗口与各子对话框，
递归遍历控件，收集仍含中文的属性值 —— 这些就是字典里缺的条目。
"""
from __future__ import annotations

import os
import sys
import re
import json

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

CJK = re.compile(r"[\u4e00-\u9fff]")

PROPS = ["text", "title", "toolTip", "placeholderText",
         "statusTip", "whatsThis", "windowTitle"]

# 这些属性值不应被翻译（技术标识符），扫描时跳过
SKIP_PATTERNS = re.compile(
    r"^(aac|libfdk|libmp3lame|ac3|flac|libx264|libx265|hevc|h264|av1|"
    r"copy|main|high|auto|amd|nvidia|intel|apple|mkv|mp4|mov|ts)$",
    re.IGNORECASE)


def scan(widget, out: dict, label: str):
    """递归遍历，收集含中文的属性值。"""
    from PySide6.QtWidgets import (QTabWidget, QTableWidget, QTreeWidget,
                                   QListWidget, QComboBox, QMenu, QHeaderView)
    try:
        for w in [widget] + widget.findChildren(object):
            try:
                # 窗口标题
                t = w.windowTitle() if hasattr(w, "windowTitle") else ""
                if t and CJK.search(t) and not SKIP_PATTERNS.match(t.strip()):
                    out.setdefault("windowTitle", []).append(t)
            except Exception:
                pass
            for p in PROPS:
                try:
                    getter = getattr(w, p, None)
                    if getter is None:
                        continue
                    v = getter() if callable(getter) else None
                    if not isinstance(v, str) or not v.strip():
                        continue
                    if not CJK.search(v):
                        continue
                    if SKIP_PATTERNS.match(v.strip()):
                        continue
                    out.setdefault(p, []).append(v)
                except Exception:
                    pass
            # 下拉框选项
            if isinstance(w, QComboBox):
                try:
                    for i in range(w.count()):
                        v = w.itemText(i)
                        if v and CJK.search(v) and not SKIP_PATTERNS.match(v.strip()):
                            out.setdefault("itemText", []).append(v)
                        d = w.itemData(i)
                        if isinstance(d, str) and CJK.search(d) and not SKIP_PATTERNS.match(d.strip()):
                            out.setdefault("itemData", []).append(d)
                except Exception:
                    pass
            # 表格表头
            hdr = None
            if isinstance(w, QTableWidget):
                hdr = w.horizontalHeader()
            elif isinstance(w, QTreeWidget):
                hdr = w.header()
            if hdr is not None:
                try:
                    for i in range(hdr.count()):
                        v = hdr.model().headerData(i, 1) if hdr.model() else None
                        if isinstance(v, str) and CJK.search(v) and not SKIP_PATTERNS.match(v.strip()):
                            out.setdefault("header", []).append(v)
                except Exception:
                    pass
            # 标签页
            if isinstance(w, QTabWidget):
                try:
                    for i in range(w.count()):
                        v = w.tabText(i)
                        if v and CJK.search(v) and not SKIP_PATTERNS.match(v.strip()):
                            out.setdefault("tabText", []).append(v)
                        tp = w.tabToolTip(i)
                        if tp and CJK.search(tp) and not SKIP_PATTERNS.match(tp.strip()):
                            out.setdefault("tabToolTip", []).append(tp)
                except Exception:
                    pass
            # 菜单项
            if isinstance(w, QMenu):
                try:
                    for act in w.actions():
                        v = act.text()
                        if v and CJK.search(v) and not SKIP_PATTERNS.match(v.strip()):
                            out.setdefault("menuAction", []).append(v)
                except Exception:
                    pass
    except Exception:
        pass


def main():
    from PySide6.QtWidgets import QApplication
    app = QApplication(sys.argv)

    import core.i18n as i18n
    i18n.set_lang("en")
    i18n.apply_translation(app)

    found: dict = {}

    from ui.main_window import MainWindow
    win = MainWindow()
    win.show()
    app.processEvents()
    # MainWindow.__init__ 会从 config 重新 set_lang，覆盖上面的设置，
    # 因此必须在构造之后再切英文。
    i18n.set_lang("en")
    i18n.apply_translation(win)
    app.processEvents()
    scan(win, found, "main_en")

    # 尝试打开各子对话框
    try:
        from ui.about_dialog import AboutDialog
        dlg = AboutDialog(win)
        app.processEvents()
        i18n.set_lang("en")
        i18n.apply_translation(dlg)
        app.processEvents()
        scan(dlg, found, "about")
    except Exception as exc:
        print(f"[skip] about: {type(exc).__name__}: {exc}")

    # AlignDialog：需要 AlignPlan
    try:
        from ui.align_dialog import AlignDialog
        from core.align import AlignPlan
        plan = AlignPlan(total=3, majority_count=2, odd_files=[],
                         odd_reasons={}, target={}, target_desc=[],
                         feasible=True, reason="")
        dlg = AlignDialog(infos=[], ffmpeg="ffmpeg", parent=win,
                          available_encoders=["libx265"],
                          usable_aac_profiles=["aac_low"], aac_encoder="aac")
        app.processEvents()
        i18n.set_lang("en")
        i18n.apply_translation(dlg)
        app.processEvents()
        scan(dlg, found, "align")
    except Exception as exc:
        print(f"[skip] align: {type(exc).__name__}: {exc}")

    # TaskDialog：需要 MergeTask
    try:
        from ui.task_dialog import TaskDialog
        from core.models import MergeTask
        t = MergeTask(name="demo")
        dlg = TaskDialog(t, win)
        app.processEvents()
        i18n.set_lang("en")
        i18n.apply_translation(dlg)
        app.processEvents()
        scan(dlg, found, "task")
    except Exception as exc:
        print(f"[skip] task: {type(exc).__name__}: {exc}")

    # ffmpeg 对话框（若存在独立入口）
    try:
        import ui.ffmpeg_dialog as fdm
        for cls_name in dir(fdm):
            kls = getattr(fdm, cls_name)
            if isinstance(kls, type) and cls_name.endswith("Dialog") \
                    and kls.__module__ == fdm.__name__:
                try:
                    if cls_name == "FFmpegDialog":
                        from core.ffmpeg_env import FFmpegEnv
                        dlg = kls(FFmpegEnv(), {}, win)
                    else:
                        dlg = kls(win)
                    app.processEvents()
                    i18n.set_lang("en")
                    i18n.apply_translation(dlg)
                    app.processEvents()
                    scan(dlg, found, cls_name)
                except Exception as exc:
                    print(f"[skip] {cls_name}: {type(exc).__name__}: {exc}")
    except Exception as exc:
        print(f"[skip] ffmpeg_dialog: {type(exc).__name__}: {exc}")

    # 汇总去重
    summary = {}
    for k, v in found.items():
        uniq = []
        seen = set()
        for s in v:
            if s not in seen:
                seen.add(s)
                uniq.append(s)
        summary[k] = uniq

    total = sum(len(v) for v in summary.values())
    print(f"\n=== 未翻译条目: {total} 条（按类别） ===")
    for k in sorted(summary):
        print(f"\n--- {k} ({len(summary[k])}) ---")
        for s in summary[k]:
            print(f"  {s!r}")

    with open("/tmp/untranslated.json", "w", encoding="utf-8") as fh:
        json.dump(summary, fh, ensure_ascii=False, indent=2)
    print(f"\n已写入 /tmp/untranslated.json，总计 {total}")


if __name__ == "__main__":
    main()
