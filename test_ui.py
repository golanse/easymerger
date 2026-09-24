"""界面冒烟测试（offscreen，不需要显示器）：
构造主窗口 → 导入测试文件夹 → 选中文件 → 建任务 → 跑队列 → 检查结果。
"""

import os
import sys
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from PySide6.QtCore import QEventLoop, QTimer, Qt  # noqa: E402
from PySide6.QtWidgets import QApplication, QMessageBox, QProgressBar  # noqa: E402

# 自动化测试：让所有模态对话框立即返回，避免无人点击时阻塞
QMessageBox.information = staticmethod(lambda *a, **k: QMessageBox.Ok)
QMessageBox.warning = staticmethod(lambda *a, **k: QMessageBox.Ok)
QMessageBox.critical = staticmethod(lambda *a, **k: QMessageBox.Ok)
QMessageBox.question = staticmethod(lambda *a, **k: QMessageBox.Yes)

from core.compat import check_lossless  # noqa: E402
from core.models import STATUS  # noqa: E402
from core.probe import probe, scan_folder  # noqa: E402
from make_testdata import ensure_testdata  # noqa: E402
from ui.main_window import MainWindow  # noqa: E402

BASE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_testdata")
ensure_testdata(BASE, "")   # ffmpeg 路径留空 → 内部自动查找
FOLDER_SAME = os.path.join(BASE, "同参数测试")
FOLDER_DIFF = os.path.join(BASE, "不同参数测试")

app = QApplication(sys.argv)
win = MainWindow()
print("窗口标题:", win.windowTitle())
print("ffmpeg:", win.env.status.message)

# --- 1. 导入（同步灌入，跳过线程） ---
paths = scan_folder(FOLDER_SAME)
infos = [probe(p, win.env.status.ffprobe) for p in paths]
win.infos = infos
win.resort_files()
win.refresh_file_table()
win.output_panel.apply_source_defaults(FOLDER_SAME, os.path.basename(FOLDER_SAME))
print("文件列表行数:", win.tbl_files.rowCount(),
      "顺序:", [win.tbl_files.item(r, 0).text() for r in range(win.tbl_files.rowCount())])
print("默认输出目录:", win.output_panel.edit_dir.text(), "| 默认文件名:", win.output_panel.edit_name.text())

# --- 2. 选中文件显示参数（需求 6） ---
win.tbl_files.selectRow(1)
QApplication.processEvents()
print("选中文件参数行数:", win.tbl_selected.rowCount())
for r in range(min(6, win.tbl_selected.rowCount())):
    print("   ", win.tbl_selected.item(r, 0).text(), "=", win.tbl_selected.item(r, 1).text())
assert win.tbl_selected.rowCount() > 10, "选中文件未显示参数"
assert win.btn_apply_params.isEnabled()

# --- 3. 用选中文件填充编码参数 ---
win.apply_selected_to_params()
p = win.encode_panel.params
print("填充后参数: 分辨率模式=", p.scale_mode, f"{p.scale_w}x{p.scale_h}", "pix_fmt=", p.pix_fmt)
assert p.scale_mode == "value" and p.scale_w == 640

# --- 4. 无损检测（需求 1） ---
rep = check_lossless(infos)
print("无损检测:", rep.summary)
assert rep.compatible

# --- 5. 加入队列（同参数 → 无损） ---
outdir = os.path.join(BASE, "ui_out")
os.makedirs(outdir, exist_ok=True)
win.output_panel.edit_dir.setText(outdir)
win.output_panel.edit_name.setText("UI无损输出")
win.output_panel.box_audio.setChecked(True)
win.output_panel.edit_audio_name.setText("UI无损输出")
win.encode_panel.chk_lossless.setChecked(True)
win.add_task()
print("队列任务数:", len(win.tasks), "队列行数:", win.tbl_queue.rowCount())

# --- 6. 第二个任务：参数不一致 → 转码 ---
infos2 = [probe(p, win.env.status.ffprobe) for p in scan_folder(FOLDER_DIFF)]
win.infos = infos2
win.resort_files()
win.refresh_file_table()
win.output_panel.edit_dir.setText(outdir)
win.output_panel.edit_name.setText("UI转码输出")
win.output_panel.edit_audio_name.setText("UI转码输出")
win.encode_panel.set_params(win.encode_panel.params.__class__(
    check_lossless=True, vcodec="libx264", crf=30, preset="ultrafast",
    fps_mode="value", fps_value=30, scale_mode="value", scale_w=640, scale_h=360))
win.add_task()
print("队列任务数:", len(win.tasks))
assert len(win.tasks) == 2

# --- 7. 运行队列 ---
win.chk_auto_open.setChecked(True)   # 顺带验证自动打开详情对话框
win.start_queue()
deadline = time.time() + 150
last_tick = 0.0
while time.time() < deadline:
    QApplication.processEvents()
    if all(t.status in (STATUS.DONE, STATUS.FAILED, STATUS.CANCELED) for t in win.tasks):
        break
    now = time.time()
    if now - last_tick > 3:
        last_tick = now
        print("  [轮询]", ", ".join(f"{t.name}={t.status}/{t.progress:.0%}/{t.stage}"
                                     for t in win.tasks),
              "worker=", win.worker.isRunning() if win.worker else None)
    time.sleep(0.05)
QApplication.processEvents()
time.sleep(0.5)
QApplication.processEvents()

print("\n--- 队列结果 ---")
for t in win.tasks:
    bar = win.tbl_queue.cellWidget(win.tasks.index(t), 5)
    print(f"  {t.name}: 状态={t.status} 模式={t.mode} 进度={t.progress:.1%} "
          f"输出={os.path.basename(t.output_path) if t.output_path else '-'} "
          f"音频={os.path.basename(t.audio_path) if t.audio_path else '-'}")
    assert t.status == STATUS.DONE, f"{t.name} 未成功：{t.error}"
print("任务对话框已打开:", list(win.dialogs.keys()))

# --- 8. 验证输出文件真实存在且时长正确 ---
for t in win.tasks:
    assert os.path.isfile(t.output_path), t.output_path
    info = probe(t.output_path, win.env.status.ffprobe)
    print(f"  {t.name} 输出: {info.resolution} {info.fps_str}fps {info.v_codec}/{info.a_codec} "
          f"时长={info.duration_str}")
    if t.audio_path:
        assert os.path.isfile(t.audio_path), t.audio_path

# --- 9. 缓存清理 ---
left = [f for d in (FOLDER_SAME, FOLDER_DIFF) for f in os.listdir(d) if f.startswith(".mp4merger")]
print("残留缓存:", left)
assert not left

# --- 10. 打开任务详情对话框 ---
win.tbl_queue.selectRow(0)
win.open_task_dialog()
QApplication.processEvents()
dlg = list(win.dialogs.values())[0]
print("对话框标题:", dlg.windowTitle())
print("对话框 Tab 数:", dlg.tabs.count(),
      "源参数行数:", dlg.tbl_sources.rowCount(),
      "编码参数行数:", dlg.tbl_params.rowCount(),
      "日志行数:", dlg.txt_log.document().blockCount())
assert dlg.tbl_sources.rowCount() == len(win.tasks[0].files)
assert dlg.tbl_params.rowCount() >= 10

# --- 11. 配置保存 ---
win.close()
print("\n✅ 界面冒烟测试通过")
