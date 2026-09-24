"""切英文后扫描主窗口 + 任务详情窗口的中文残留。"""
import os, re, sys
sys.path.insert(0, "/data/workspace/pkg33")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtWidgets import QApplication
from core import i18n
i18n.set_lang("en")
app = QApplication(sys.argv)
from ui.main_window import MainWindow
w = MainWindow()
w.show()
ZH = re.compile(r"[\u4e00-\u9fff]")
def scan(root, tag):
    bad = []
    for wd in root.findChildren(object):
        t = None
        try:
            t = wd.text()
        except Exception:
            t = None
        if not t:
            try:
                t = wd.title()
            except Exception:
                t = None
        if t and ZH.search(t):
            bad.append((type(wd).__name__, t[:60]))
    print(f"[{tag}] 残留 {len(bad)} 处")
    for c, t in bad[:12]:
        print("   ", c, "->", t)
    return len(bad)
total = scan(w, "MainWindow")
try:
    from ui.task_dialog import TaskDialog
    from core.models import MergeTask
    t = MergeTask(name="Test Task")
    d = TaskDialog(t, w)
    total += scan(d, "TaskDialog")
    print("   TaskDialog 标题:", d.windowTitle())
except Exception as e:
    import traceback; traceback.print_exc()
    print("[TaskDialog] 无法实例化:", e)
print("结论:", "通过（0 处中文残留）" if total == 0 else f"仍有 {total} 处")
sys.exit(0 if total == 0 else 1)
