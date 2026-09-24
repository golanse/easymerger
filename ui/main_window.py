"""主窗口：导入素材 / 编辑参数 / 多任务排队 / 进度总览。"""

from __future__ import annotations

import copy
import os
import subprocess
import sys
from typing import Dict, List, Optional, Sequence

from PySide6.QtCore import Qt, QThread, QTimer, QUrl, Signal
from PySide6.QtGui import QAction, QColor, QDesktopServices, QKeySequence
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QCheckBox, QComboBox, QFileDialog, QFrame, QGroupBox,
    QHBoxLayout, QHeaderView, QLabel, QMainWindow, QMessageBox, QProgressBar, QPushButton,
    QScrollArea, QSplitter, QStatusBar, QTableWidget, QTableWidgetItem, QTabWidget, QTextEdit,
    QVBoxLayout, QWidget,
)

from core.compat import check_lossless
from core.config import AppConfig, load_config, save_config
from core.ffmpeg_env import FFmpegEnv
from core.models import STATUS, EncodeParams, MergeTask, OutputParams, VideoInfo
from core.natsort import natural_key
from core.normalize import normalize_for_merge
from core.probe import probe, scan_folder

from .encode_panel import EncodePanel
from .ffmpeg_dialog import FFmpegDialog
from .output_panel import OutputPanel
from .task_dialog import TaskDialog
from .worker import QueueWorker

VIDEO_EXTS = {".mp4", ".m4v", ".mov", ".mkv"}
# 主界面文件列表的列，与任务详情「① 源视频参数」保持一致。
# 之前这里只有 8 列，看不到 Profile / 音频 Profile / 码率 / SAR 等，
# 而"哪些文件能无损合并"恰恰取决于这些 —— 用户得打开任务详情才能比对，
# 很不方便。现在两边列完全一致，直接在列表里就能看出哪个文件不一样。
FILE_HEADERS = [
    "文件名", "时长", "分辨率", "视频编码", "Profile", "Level", "像素格式", "帧率",
    "视频码率", "SAR", "DAR", "旋转",
    "音频编码", "音频 Profile", "采样率", "声道", "音频码率", "整体码率", "大小",
]
QUEUE_HEADERS = ["任务名", "文件数", "总时长", "合并方式", "状态", "进度", "输出文件"]

#: 与 core.align.BEST_EFFORT_KEYS 对应 —— 这些列不一致**不影响**
#: 无损合并，只是"看着不一样"。必须与那边保持同步（有测试守着）。
#:
#: 典型是 Level：x265 完全忽略 -level/--level-idc，始终按内容自动算，
#: 转码也统一不了；而实测 concat 不要求 level 一致（level 51 与 31
#: 合并后解码完全正常）。所以标成"有问题"纯属误导。
HARMLESS_DIFF_HEADERS = {"Level"}

#: 无害差异用中性色（蓝灰），与"真·阻断"的黄色区分开。
#: 只改文案不够 —— 用户扫一眼先看颜色，黄色仍会让人以为要处理。
HARMLESS_DIFF_COLOR = "#e8f0fe"




def _sem(kind: str) -> str:
    """按当前主题取语义色（深浅两套），避免深色主题下标记看不清。"""
    try:
        from core.theme import semantic_color
        return semantic_color(kind)
    except Exception:
        return {"warn": "#8a5300", "error": "#b3261e", "ok": "#1e7a32",
                "info": "#1a5fb4"}.get(kind, "#1a1a1a")

class CandidateScanThread(QThread):
    """后台枚举本机所有 ffmpeg 候选。

    必须放后台：这个操作会为每个候选启动两次子进程
    （-encoders / -version），而 Windows 上的 ffmpeg.exe 动辄上百 MB。
    放在主线程里就是"启动后卡住几秒"的元凶。
    """

    sig_done = Signal(object)

    def __init__(self, current: str, parent=None):
        super().__init__(parent)
        self.current = current

    def run(self) -> None:
        try:
            from core.ffmpeg_env import candidate_ffmpegs
            self.sig_done.emit(candidate_ffmpegs(self.current))
        except Exception:  # noqa: BLE001
            self.sig_done.emit([])


class ProbeThread(QThread):
    """后台探测视频参数，避免导入大量文件时界面卡死。"""

    sig_progress = Signal(int, int, str)
    sig_done = Signal(list)
    sig_error = Signal(str)

    def __init__(self, paths: Sequence[str], ffprobe: str, parent=None):
        super().__init__(parent)
        self.paths = list(paths)
        self.ffprobe = ffprobe

    def run(self) -> None:  # noqa: D102
        infos: List[VideoInfo] = []
        total = len(self.paths)
        for i, p in enumerate(self.paths):
            infos.append(probe(p, self.ffprobe))
            self.sig_progress.emit(i + 1, total, os.path.basename(p))
        self.sig_done.emit(infos)


def _norm_title(t: str) -> str:
    """标题专用：英文界面下把全角空格换成半角，避免夹在英文里显得突兀。"""
    try:
        from core.i18n import current_lang, _normalize_punct
        if current_lang() != "zh":
            return _normalize_punct(t)
    except Exception:  # noqa: BLE001
        pass
    return t


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.config: AppConfig = load_config()
        self.env: FFmpegEnv = FFmpegEnv(config=self.config)
        self.infos: List[VideoInfo] = []
        self.tasks: List[MergeTask] = []
        self.dialogs: Dict[str, TaskDialog] = {}
        self.worker: Optional[QueueWorker] = None
        self._probe_thread: Optional[ProbeThread] = None
        self._pending_files: List[str] = []
        self._batch_queue: List[tuple] = []
        self._batch_done: int = 0

        # 标题带上版本号：一眼就能确认跑的是不是新 exe
        self._refresh_title()
        self.resize(1300, 880)   # 参数区可滚动，不必靠撑高窗口来显示全部参数
        self._build_ui()
        self._apply_ui_prefs()
        # 首次启动时编码器要按本机能力挑最强的，
        # 否则会落在通用默认值上（nonfree 构建也只选到原生 aac）。
        self._first_run = not bool(getattr(self.config, "last_params", None))
        self._restore_settings()
        QTimer.singleShot(300, self._check_ffmpeg_on_start)

    def _refresh_title(self) -> None:
        """重建窗口标题。

        标题里夹着版本号，整句查不到字典，所以只翻译 slogan 那一段。
        必须在切语言时**重新调用** —— 原先只在 __init__ 里设一次，
        于是中文启动后切英文，左上角始终还是中文。
        """
        try:
            from core.app_meta import FULL_TITLE
            from core.i18n import tr
            slogan = "无损拼接 / 转码拼接 / 批量排队"
            self.setWindowTitle(_norm_title(FULL_TITLE.replace(slogan, tr(slogan))))
        except Exception:  # noqa: BLE001
            pass

    # ==================================================================
    # 界面
    # ==================================================================
    def _build_ui(self) -> None:
        central = QWidget()
        root = QVBoxLayout(central)
        root.setContentsMargins(8, 8, 8, 8)

        # ---- 顶部工具条 ----
        top = QHBoxLayout()
        self.btn_add_folder = QPushButton("导入文件夹")
        self.btn_add_folder.setToolTip("选择一个文件夹，自动按文件名自然排序导入其中的 MP4 视频")
        self.btn_add_files = QPushButton("导入文件")
        self.btn_add_batch = QPushButton("批量导入（每文件夹一个任务）")
        self.btn_add_batch.setToolTip("选择父文件夹，其下每个含视频的子文件夹会自动生成一个排队任务")
        self.chk_recursive = QCheckBox("含子文件夹")
        self.btn_remove = QPushButton("移除选中")
        self.btn_up = QPushButton("↑ 上移")
        self.btn_down = QPushButton("↓ 下移")
        self.btn_resort = QPushButton("恢复自然排序")
        self.btn_align = QPushButton("⚡ 一键对齐异类")
        self.btn_align.setToolTip(
            "有少数几个文件参数和大家不一样时，只转换这几个。\n"
            "转换后整批就能无损合并（几分钟、画质零损失），\n"
            "而不是把全部文件重新编码一遍。")
        self.btn_clear = QPushButton("清空列表")
        self.btn_ffmpeg = QPushButton("ffmpeg 状态")
        self.btn_ffmpeg.clicked.connect(self.open_ffmpeg_dialog)
        for b in (self.btn_add_folder, self.btn_add_files, self.btn_add_batch, self.btn_remove,
                  self.btn_up, self.btn_down, self.btn_resort, self.btn_align, self.btn_clear):
            top.addWidget(b)
        top.addWidget(self.chk_recursive)
        top.addStretch(1)
        top.addWidget(self.btn_ffmpeg)
        root.addLayout(top)

        self.btn_add_folder.clicked.connect(self.add_folder)
        self.btn_add_files.clicked.connect(self.add_files)
        self.btn_add_batch.clicked.connect(self.add_folders_as_tasks)
        self.btn_remove.clicked.connect(self.remove_selected)
        self.btn_up.clicked.connect(lambda: self.move_selected(-1))
        self.btn_down.clicked.connect(lambda: self.move_selected(1))
        self.btn_resort.clicked.connect(self.resort_files)
        self.btn_align.clicked.connect(self.open_align_dialog)
        self.btn_clear.clicked.connect(self.clear_files)

        # ---- 主体 ----
        self.splitter_main = QSplitter(Qt.Vertical)
        upper = QSplitter(Qt.Horizontal)

        # 左：文件列表 + 选中文件参数
        left = QWidget()
        ll = QVBoxLayout(left)
        ll.setContentsMargins(0, 0, 0, 0)
        self.tbl_files = QTableWidget(0, len(FILE_HEADERS))
        self.tbl_files.setHorizontalHeaderLabels(FILE_HEADERS)
        self.tbl_files.verticalHeader().setVisible(False)
        self.tbl_files.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.tbl_files.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.tbl_files.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.tbl_files.setAlternatingRowColors(True)
        self.tbl_files.setAcceptDrops(True)
        self.tbl_files.setDragDropMode(QAbstractItemView.DropOnly)
        self.tbl_files.dragEnterEvent = self._drag_enter  # type: ignore[assignment]
        self.tbl_files.dropEvent = self._drop_files  # type: ignore[assignment]
        hh = self.tbl_files.horizontalHeader()
        hh.setSectionResizeMode(0, QHeaderView.Stretch)
        for c in range(1, len(FILE_HEADERS)):
            hh.setSectionResizeMode(c, QHeaderView.ResizeToContents)
        self.tbl_files.itemSelectionChanged.connect(self.on_file_selected)
        self.tbl_files.doubleClicked.connect(self._reveal_selected)
        ll.addWidget(self.tbl_files, 3)

        gb_sel = QGroupBox("选中文件的参数（用于参考设置转码参数）")
        vl = QVBoxLayout(gb_sel)
        self.tbl_selected = QTableWidget(0, 2)
        self.tbl_selected.setHorizontalHeaderLabels(["参数", "值"])
        self.tbl_selected.verticalHeader().setVisible(False)
        self.tbl_selected.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.tbl_selected.setSelectionMode(QAbstractItemView.NoSelection)
        self.tbl_selected.setAlternatingRowColors(True)
        self.tbl_selected.horizontalHeader().setStretchLastSection(True)
        self.tbl_selected.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeToContents)
        vl.addWidget(self.tbl_selected)
        row_sel = QHBoxLayout()
        self.btn_apply_params = QPushButton("用此文件参数填充右侧编码参数")
        self.btn_apply_params.setEnabled(False)
        self.btn_apply_params.clicked.connect(self.apply_selected_to_params)
        row_sel.addWidget(self.btn_apply_params)
        row_sel.addStretch(1)
        vl.addLayout(row_sel)
        ll.addWidget(gb_sel, 2)
        upper.addWidget(left)

        # 右：参数 Tab
        self.tabs = QTabWidget()
        self.encode_panel = EncodePanel()
        self.output_panel = OutputPanel()

        # 编码参数放进滚动区。
        # 不加滚动的话，面板的 sizeHint 有 700+ px，会把上方区域"撑"起来，
        # splitter 再怎么分配，总高度都压不下去（实测 1094 px，一屏放不下）。
        # 放进 QScrollArea 后面板不再有硬性高度下限，窗口想多矮就多矮，
        # 参数多时滚动查看即可。
        self.scroll_encode = QScrollArea()
        self.scroll_encode.setWidgetResizable(True)
        self.scroll_encode.setFrameShape(QScrollArea.NoFrame)
        self.scroll_encode.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.scroll_encode.setWidget(self.encode_panel)

        self.scroll_output = QScrollArea()
        self.scroll_output.setWidgetResizable(True)
        self.scroll_output.setFrameShape(QScrollArea.NoFrame)
        self.scroll_output.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.scroll_output.setWidget(self.output_panel)

        self.tabs.addTab(self.scroll_encode, "编码参数")
        self.tabs.addTab(self.scroll_output, "输出设置")

        # 语言设置保留在菜单栏「视图 → 语言」，不再单独占一个标签页
        # 编码器变化时，让输出面板检查容器兼容性（如 AV1 装不进 MOV）
        self.encode_panel.cmb_vcodec.currentTextChanged.connect(
            lambda *_: self.output_panel.sync_with_vcodec(self.encode_panel.params.vcodec))
        self.output_panel.cmb_container.currentTextChanged.connect(
            lambda *_: self.output_panel.sync_with_vcodec(self.encode_panel.params.vcodec))
        upper.addWidget(self.tabs)
        upper.setStretchFactor(0, 3)
        upper.setStretchFactor(1, 2)
        self.splitter_main.addWidget(upper)
        self.upper = upper

        # 下：任务队列
        gb_queue = QGroupBox("任务队列（按加入顺序依次处理）")
        self.gb_queue = gb_queue
        ql = QVBoxLayout(gb_queue)
        self.tbl_queue = QTableWidget(0, len(QUEUE_HEADERS))
        self.tbl_queue.setHorizontalHeaderLabels(QUEUE_HEADERS)
        self.tbl_queue.verticalHeader().setVisible(False)
        self.tbl_queue.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.tbl_queue.setSelectionBehavior(QAbstractItemView.SelectRows)
        # 支持 Ctrl / Shift 多选（和上方文件列表一致）。
        # 之前是 SingleSelection，用户按住 Ctrl 点第二行时选择直接跳走，
        # 想一次移除好几个任务只能一个个点，很别扭。
        self.tbl_queue.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.tbl_queue.setAlternatingRowColors(True)
        qh = self.tbl_queue.horizontalHeader()
        qh.setSectionResizeMode(0, QHeaderView.Stretch)
        qh.setSectionResizeMode(5, QHeaderView.Stretch)
        for c in (1, 2, 3, 4):
            qh.setSectionResizeMode(c, QHeaderView.ResizeToContents)
        self.tbl_queue.doubleClicked.connect(lambda *_: self.open_task_dialog())
        # 行高放宽一点，进度条和文字不至于挤在一起
        self.tbl_queue.verticalHeader().setDefaultSectionSize(26)
        ql.addWidget(self.tbl_queue)

        # 队列区默认占 42% 高度。
        # 注意必须用 setSizes 显式指定 —— setStretchFactor 只在有
        # 剩余空间可分配时才起作用，初始布局时上下都按 sizeHint 分配，
        # 结果队列区只拿到 15%，一屏只能看到 5 条，很不好用。
        self._queue_ratio = 0.42

        qrow = QHBoxLayout()
        self.btn_add_task = QPushButton("+ 加入队列")
        self.btn_add_task.clicked.connect(self.add_task)
        self.btn_start = QPushButton("▶ 开始队列")
        self.btn_start.clicked.connect(self.start_queue)
        self.btn_pause = QPushButton("⏸ 暂停")
        self.btn_pause.clicked.connect(self.toggle_pause)
        self.btn_pause.setEnabled(False)
        self.btn_stop = QPushButton("⏹ 停止")
        self.btn_stop.clicked.connect(self.stop_queue)
        self.btn_stop.setEnabled(False)
        self.btn_detail = QPushButton("任务详情")
        self.btn_detail.clicked.connect(self.open_task_dialog)
        self.btn_remove_task = QPushButton("移除任务")
        self.btn_remove_task.setToolTip(
            "移除选中的任务。可以按住 Ctrl 点选多个、按住 Shift 选连续一段。")
        self.btn_remove_task.clicked.connect(self.remove_task)
        self.btn_clear_done = QPushButton("清空已完成")
        self.btn_clear_done.clicked.connect(self.clear_finished)
        self.chk_auto_open = QCheckBox("任务开始时自动打开详情")
        for w in (self.btn_add_task, self.btn_start, self.btn_pause, self.btn_stop,
                  self.btn_detail, self.btn_remove_task, self.btn_clear_done, self.chk_auto_open):
            qrow.addWidget(w)
        qrow.addStretch(1)
        ql.addLayout(qrow)
        self.splitter_main.addWidget(gb_queue)
        self.splitter_main.setStretchFactor(0, 3)
        self.splitter_main.setStretchFactor(1, 2)
        root.addWidget(self.splitter_main, 1)

        self._build_menu()
        self.setCentralWidget(central)

        # ---- 状态栏 ----
        sb = QStatusBar()
        self.setStatusBar(sb)
        self.lbl_status = QLabel("就绪")
        self.lbl_ff = QLabel()
        sb.addWidget(self.lbl_status, 1)
        sb.addPermanentWidget(self.lbl_ff)
        self.refresh_ffmpeg_label()

        # 快捷键
        QAction("删除", self, shortcut=QKeySequence.Delete, triggered=self.remove_selected)

        # 启动后稍等再检查（要枚举并查询多个 ffmpeg，放在 0.6s 后避免拖慢启动）
        QTimer.singleShot(3000, self._maybe_suggest_better_ffmpeg)
        # 窗口显示后再分配高度，此时 splitter 才有真实尺寸可分
        QTimer.singleShot(0, self._apply_splitter_sizes)

    # ==================================================================
    # 菜单栏 / 主题 / 语言 / 关于
    # ==================================================================
    def _build_menu(self) -> None:
        mb = self.menuBar()

        # ---- 视图：主题 ----
        m_view = mb.addMenu("视图")
        m_theme = m_view.addMenu("主题")
        self._theme_actions = {}
        for key, label in (("light", "浅色"), ("dark", "深色")):
            act = QAction(label, self, checkable=True)
            act.triggered.connect(lambda _=False, k=key: self._switch_theme(k))
            m_theme.addAction(act)
            self._theme_actions[key] = act
        m_view.addSeparator()

        # ---- 视图：语言 ----
        from core.i18n import LANGUAGES
        m_lang = m_view.addMenu("语言")
        self._lang_actions = {}
        for key, label in LANGUAGES.items():
            act = QAction(label, self, checkable=True)
            act.triggered.connect(lambda _=False, k=key: self._switch_lang(k))
            m_lang.addAction(act)
            self._lang_actions[key] = act

        # ---- 帮助 ----
        m_help = mb.addMenu("帮助")
        act_about = QAction("关于", self)
        act_about.triggered.connect(self.open_about)
        m_help.addAction(act_about)
        act_home = QAction("打开项目主页", self)
        act_home.triggered.connect(self.open_homepage)
        m_help.addAction(act_home)

    def _apply_ui_prefs(self) -> None:
        """启动时套用上次的主题与语言。"""
        from core import i18n, theme
        lang = i18n.load_lang_from_config()
        th = theme.load_theme_from_config()
        i18n.set_lang(lang)
        theme.apply_theme(QApplication.instance(), th)
        self._sync_menu_checks()
        if lang != "zh":
            i18n.apply_translation(self)

    def _sync_menu_checks(self) -> None:
        from core import i18n, theme
        cur_lang, cur_theme = i18n.current_lang(), theme.current_theme()
        for k, act in self._theme_actions.items():
            act.setChecked(k == cur_theme)
        for k, act in self._lang_actions.items():
            act.setChecked(k == cur_lang)

    def _switch_theme(self, key: str) -> None:
        from core import theme
        theme.apply_theme(QApplication.instance(), key)
        theme.save_theme_to_config(key)
        self._sync_menu_checks()
        # 关键：表格里的差异高亮是在**填充行时**就写死颜色的。
        # 不重绘的话，切换主题后旧行仍保留上一套配色 ——
        # 用户切到深色，看到的还是浅黄底 + 深色字，于是"看不清"。
        self._repaint_themed_tables()
        # 提示卡片（HintCard）的颜色是运行时按主题取进去的富文本，
        # 切主题后必须重建，否则旧卡片仍带着上一套配色。
        try:
            from core import i18n as _i18n
            _i18n.apply_translation(self)
        except Exception:
            pass

    def _repaint_themed_tables(self) -> None:
        """重绘所有带语义配色的表格，让新主题生效。"""
        try:
            if getattr(self, "infos", None):
                self.refresh_file_table()
        except Exception:
            pass
        try:
            self.refresh_queue_table()
        except Exception:
            pass
        try:
            self.tbl_files.viewport().update()
            self.tbl_queue.viewport().update()
        except Exception:
            pass

    def _switch_lang(self, key: str) -> None:
        from core import i18n
        i18n.set_lang(key)
        i18n.save_lang_to_config(key)
        i18n.apply_translation(self)
        self.retranslate_dynamic()

        # 已经打开的对话框也要跟着换
        for dlg in list(self.dialogs.values()):
            try:
                i18n.apply_translation(dlg)
            except Exception:  # noqa: BLE001
                pass
        self._sync_menu_checks()
        # 标签页里的单选框也要跟着变（set_checked 内部屏蔽了信号，不会回环）
        panel = getattr(self, "lang_panel", None)
        if panel is not None:
            panel.set_checked(key)
        name = {"zh": "简体中文", "en": "English"}.get(key, key)
        # 两种语言各写整句，不用拼接片段 —— 片段很难被词典覆盖，
        # 拼出来的句子会一半英文一半中文（"语言已切换为 English"）。
        if key == "en":
            self.set_status(f"Language switched to {name}")
        else:
            self.set_status(f"语言已切换为 {name}")

    def retranslate_dynamic(self) -> None:
        """重建**运行时拼接**出来的文本。

        像状态栏「ffmpeg 4.4（来源：系统 PATH）」这种是探测时就拼好
        存进 status.message 的，切换语言时它不会自动变 —— 界面上就
        留下一句中文。这里在切语言后重新生成一遍。
        """
        try:
            self._refresh_title()
        except Exception:
            pass
        try:
            if hasattr(self.env, "refresh_status_text"):
                self.env.refresh_status_text()
        except Exception:
            pass
        try:
            self.refresh_ffmpeg_label()
        except Exception:
            pass
        # 状态栏：用记下来的重建函数重新生成一遍
        try:
            _b = getattr(self, "_status_i18n", None)
            if callable(_b):
                self.set_status(_b(), i18n_parts=_b)
        except Exception:
            pass

    def open_about(self) -> None:
        from ui.about_dialog import AboutDialog
        dlg = AboutDialog(self, getattr(self.env.status, "ffmpeg", ""),
                          getattr(self.env.status, "ffprobe", ""))
        dlg.exec()

    def open_homepage(self) -> None:
        import os
        from PySide6.QtGui import QDesktopServices
        from PySide6.QtCore import QUrl
        url = os.environ.get("EASYMERGER_HOME", "https://github.com/")
        QDesktopServices.openUrl(QUrl(url))

    # ==================================================================
    # 导入
    # ==================================================================
    def _drag_enter(self, event):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def _drop_files(self, event):
        paths = []
        for url in event.mimeData().urls():
            p = url.toLocalFile()
            if not p:
                continue
            if os.path.isdir(p):
                paths.extend(scan_folder(p, self.chk_recursive.isChecked(), VIDEO_EXTS))
            elif os.path.splitext(p)[1].lower() in VIDEO_EXTS:
                paths.append(p)
        if paths:
            self._import_paths(paths)

    def add_folder(self) -> None:
        d = QFileDialog.getExistingDirectory(self, "选择包含视频的文件夹",
                                             self.config.last_input_dir or os.path.expanduser("~"))
        if not d:
            return
        self.config.last_input_dir = d
        paths = scan_folder(d, self.chk_recursive.isChecked(), VIDEO_EXTS)
        if not paths:
            QMessageBox.information(self, "没有找到视频", "该文件夹内没有找到 mp4/m4v/mov/mkv 视频文件。")
            return
        self._import_paths(paths, source_dir=d)

    def add_files(self) -> None:
        files, _ = QFileDialog.getOpenFileNames(
            self, "选择视频文件", self.config.last_input_dir or os.path.expanduser("~"),
            "视频文件 (*.mp4 *.m4v *.mov *.mkv);;所有文件 (*)")
        if files:
            self._import_paths(files)

    def add_folders_as_tasks(self) -> None:
        """批量导入：父目录下每个含视频的子文件夹 → 一个独立任务直接入队。"""
        parent = QFileDialog.getExistingDirectory(
            self, "选择父文件夹（其下每个子文件夹会生成一个任务）",
            self.config.last_input_dir or os.path.expanduser("~"))
        if not parent:
            return
        self.config.last_input_dir = parent
        subs = []
        for name in sorted(os.listdir(parent), key=natural_key):
            d = os.path.join(parent, name)
            if os.path.isdir(d):
                subs.append(d)
        # 父目录自身也检查
        candidates = [parent] + subs
        # 逐个文件夹**串行**处理，不要并发。
        #
        # 原来的写法是在循环里连续调 _probe_and_queue，每次都执行
        #   self._probe_thread = ProbeThread(...)
        # 后一个线程对象覆盖了前一个的**唯一强引用**，于是前一个还在运行的
        # QThread 被 Python 垃圾回收 → 回调丢失 / 结果错乱。
        # 表现就是：第一次导入参数正确，第二次却拿到了别的文件夹的参数
        # （实测：并发下 _on_probe_done 收到的 infos 与 name 会错配）。
        #
        # 改成：先收集，再用单个线程排队处理，全部完成后统一提示。
        batch: List[tuple] = []
        for d in candidates:
            paths = scan_folder(d, self.chk_recursive.isChecked(), VIDEO_EXTS)
            if not paths:
                continue
            batch.append((paths, d, os.path.basename(d.rstrip(os.sep))))

        if not batch:
            QMessageBox.information(self, "没有找到视频", "所选目录及其子文件夹中没有找到视频文件。")
            return

        self._batch_queue = batch
        self._batch_done = 0
        self.set_status(f"批量导入：共 {len(batch)} 个文件夹，开始读取…")
        self._batch_next()

    def _import_paths(self, paths: Sequence[str], source_dir: str = "") -> None:
        """导入到当前文件列表（左侧）。"""
        self._pending_files = list(paths)
        self._pending_source_dir = source_dir or os.path.dirname(paths[0])
        self._start_probe(paths, to_list=True, name=os.path.basename(
            (source_dir or os.path.dirname(paths[0])).rstrip(os.sep)))

    # ---- 批量导入：串行队列 ----
    def _batch_next(self) -> None:
        """处理批量队列里的下一个文件夹。"""
        queue = getattr(self, "_batch_queue", None)
        if not queue:
            self._batch_finish()
            return
        paths, source_dir, name = queue.pop(0)
        self._batch_done = getattr(self, "_batch_done", 0) + 1
        total = self._batch_done + len(queue)
        self.set_status(f"批量导入 {self._batch_done}/{total}：正在读取「{name}」…")
        self._start_probe(paths, to_list=False, name=name, source_dir=source_dir)

    def _batch_finish(self) -> None:
        """批量导入全部处理完，统一提示。"""
        count = getattr(self, "_batch_done", 0)
        self._batch_queue = []
        # 只统计本次批量导入产生的任务（用自动备注区分）
        auto = [(t.name, len(getattr(t, "auto_notes", []) or [])) for t in self.tasks
                if getattr(t, "auto_notes", None)]
        if auto:
            detail = "；".join(f"{n}({c}项)" for n, c in auto[-8:])
            more = f"，以及另外 {len(auto) - 8} 个" if len(auto) > 8 else ""
            QMessageBox.information(
                self, "批量导入完成",
                f"已加入 {count} 个任务。\n\n"
                f"其中 {len(auto)} 个任务的源视频参数不一致，软件已自动统一"
                f"（需要转码时以各文件夹首个视频为基准）：\n  {detail}{more}\n\n"
                "双击队列里的任务 →「② 编码设置参数」可以看到每个任务实际生效的参数。")
        else:
            self.set_status(f"已批量加入 {count} 个任务")

    def _probe_and_queue(self, paths: Sequence[str], source_dir: str, name: str) -> None:
        """探测完成后直接作为一个任务入队（批量导入用）。

        注意：不要在一个循环里连续调用它 —— 那会覆盖 self._probe_thread，
        让正在运行的线程被回收。批量场景请走 _batch_next 串行队列。
        """
        self._start_probe(paths, to_list=False, name=name, source_dir=source_dir)

    def _start_probe(self, paths: Sequence[str], to_list: bool, name: str, source_dir: str = "") -> None:
        if not self.env.status.ffprobe:
            QMessageBox.warning(self, "缺少 ffprobe", "未找到 ffprobe，无法读取视频参数。请先在右上角「ffmpeg 状态」里安装或指定。")
            return
        self._probe_thread = ProbeThread(paths, self.env.status.ffprobe, self)
        self._probe_thread.sig_progress.connect(
            lambda i, t, n: self.set_status(f"正在读取视频参数 {i}/{t}：{n}"))
        self._probe_thread.sig_done.connect(
            lambda infos: self._on_probe_done(infos, to_list, name, source_dir))
        self._probe_thread.start()
        self.set_status(f"正在读取 {len(paths)} 个文件的参数…")

    def _on_probe_done(self, infos: List[VideoInfo], to_list: bool, name: str, source_dir: str) -> None:
        bad = [i for i in infos if i.error]
        if len(bad) == len(infos):
            QMessageBox.warning(self, "读取失败", f"全部 {len(bad)} 个文件都无法解析：\n{bad[0].error}")
            return
        for b in bad:
            self.set_status(f"跳过无法解析的文件：{b.name}（{b.error[:60]}）")
        good = [i for i in infos if not i.error]

        if to_list:
            self.infos.extend(good)
            self.resort_files()
            self.refresh_file_table()
            self.output_panel.apply_source_defaults(
                source_dir or (os.path.dirname(good[0].path) if good else ""), name)
            self.set_status(f"已导入 {len(good)} 个文件（总时长 {self.total_duration_str()}）")
        else:
            task = self._make_task(good, name, source_dir or os.path.dirname(good[0].path),
                                   baseline_first=True)
            self._enqueue(task)
            self.set_status(f"任务「{name}」已加入队列（{len(good)} 个文件）")
            # 批量导入：处理完这一个，接着处理下一个
            if getattr(self, "_batch_queue", None):
                self._batch_next()
                return

    # ==================================================================
    # 重新读取文件参数（对齐替换原文件后需要）
    # ==================================================================
    def _reload_file_params(self) -> None:
        """重新 probe 列表里所有文件，刷新显示的参数。

        一键对齐勾选"替换源文件"后，磁盘上的文件已经变了，
        但列表里还是旧参数 —— 不刷新的话用户会以为对齐没生效，
        而实际上只是没重新读取。
        """
        if not self.infos:
            return
        try:
            from core.probe import probe
        except Exception:  # noqa: BLE001
            return
        fp = getattr(self.env.status, "ffprobe", "")
        if not fp:
            return
        fresh, failed = [], []
        for info in self.infos:
            try:
                fresh.append(probe(info.path, fp))
            except Exception:  # noqa: BLE001
                failed.append(info.name)
                fresh.append(info)
        self.infos = fresh
        self.refresh_file_table()
        if failed:
            self.set_status(f"已刷新参数（{len(failed)} 个读取失败）")
        else:
            self.set_status(f"已刷新 {len(self.infos)} 个文件的参数")

    # ==================================================================
    # 编码器能力：本机到底能用哪些
    # ==================================================================
    def _available_encoders(self) -> list:
        """本机 ffmpeg 支持的编码器名（软件 + 实测可用的硬件）。

        多处要用：填充编码参数、一键对齐选编码器。
        硬件编码器以"实测能编码"为准 —— 列表里有但一跑就失败的
        （没 N 卡的 h264_nvenc 之类）不应被选中。
        """
        avail: list = []
        try:
            avail = list(self.env.encoders())
        except Exception:  # noqa: BLE001
            pass
        hw: list = []
        try:
            hw = list(self.encode_panel.probe.usable_hardware_encoders())
        except Exception:  # noqa: BLE001
            pass
        for name in hw:
            if name not in avail:
                avail.append(name)
        return avail

    # ==================================================================
    # 一键对齐异类
    # ==================================================================
    def open_align_dialog(self) -> None:
        """只转换"和大家不一样"的那几个文件。

        合并的前提是各分片参数一致。只要有一个文件不一样，整批就得
        全部重编码 —— 2 小时素材重编一遍，画质还要再损失一次。

        理性做法是只把那一个异类转过来。但对普通用户来说，
        "去下个软件、把第26集转成 1920x1080@30 / HE-AACv2" 不可行：
        他们不知道参数在哪、该填什么。所以做成一键。
        """
        if not getattr(self, "infos", None) or len(self.infos) < 2:
            QMessageBox.information(self, "对齐异类",
                                    "请先导入至少 2 个视频文件。")
            return
        try:
            from ui.align_dialog import AlignDialog
        except Exception as e:  # noqa: BLE001
            QMessageBox.critical(self, "错误", f"无法打开对齐功能：{e}")
            return

        # 把"本机真正能用哪些编码器"告诉对话框。
        # 不传的话它无法按目标格式挑编码器 —— 目标是 HEVC 却用
        # libx264 去转，命令能成功但产出 H.264，跟多数派还是不一致，
        # 等于白转一遍（V1.3 的 bug）。
        avail: list = []
        try:
            avail = list(self.env.encoders())
        except Exception:  # noqa: BLE001
            pass
        hw: list = []
        try:
            hw = list(self.encode_panel.probe.usable_hardware_encoders())
        except Exception:  # noqa: BLE001
            pass
        for name in hw:
            if name not in avail:
                avail.append(name)

        # 当前 ffmpeg 真正能**输出**哪些 AAC profile。
        #
        # 关键：必须用"最强的 AAC 编码器"去探测能力。
        # 只用原生 aac 探测的话，它只能输出 AAC-LC，于是软件永远
        # 认为"不支持 HE-AAC" —— 即使用户装的是带 libfdk_aac 的
        # nonfree 构建。这正是用户报的 "Profile not supported"。
        usable_prof: list = []
        aac_enc = "aac"
        try:
            from core.audio_profile import best_usable_profiles
            aac_enc, usable_prof = best_usable_profiles(
                self.env.status.ffmpeg, self.env.status.ffprobe,
                avail)
        except Exception:  # noqa: BLE001
            usable_prof = []

        try:
            dlg = AlignDialog(self.infos, self.env.status.ffmpeg, self,
                              available_encoders=avail,
                              usable_aac_profiles=usable_prof,
                              aac_encoder=aac_enc,
                              prefer_hw=self.encode_panel.chk_prefer_hw.isChecked(),
                              gpu_vendor=(self.encode_panel.gpu_vendor()
                                          if hasattr(self.encode_panel, "gpu_vendor")
                                          else "auto"))
            # 对齐可能替换了原文件 → 关闭后必须重读参数，
            # 否则列表里显示的还是旧参数，用户会以为没生效
            dlg.sig_refresh_requested.connect(self._reload_file_params)
            dlg.exec()
            self._reload_file_params()
        except Exception as e:  # noqa: BLE001
            QMessageBox.critical(self, "错误", f"对齐功能出错：{e}")

    # ==================================================================
    # 文件列表
    # ==================================================================
    def refresh_file_table(self) -> None:
        self.tbl_files.setRowCount(0)

        # 以第一个文件为基准，把"和它不一致"的单元格标黄。
        # 用户的核心诉求是"一眼看出哪些文件参数不一样"（不一样就不能无损合并），
        # 光把所有参数列出来还不够 —— 19 列 × 10 行靠肉眼比对太累。
        base = self.infos[0].table_row() if self.infos else None
        # 这些列本来就该各不相同，标出来只会干扰判断：
        #   0 文件名、1 时长、8 视频码率、16 音频码率、17 整体码率、18 文件大小
        #
        # 注意：**分辨率（2）绝不能 skip**。
        # 之前注释写"已由宽高列体现"，但 FILE_HEADERS 里根本没有
        # 独立的宽/高列 —— 第 2 列就是分辨率本身。
        # skip 掉它的后果：分辨率不同（**最常见**的不能无损合并原因）
        # 完全不标黄，用户根本发现不了为什么合并要走转码。
        skip = {0, 1, 8, 16, 17, 18}

        for r, info in enumerate(self.infos):
            self.tbl_files.insertRow(r)
            row = info.table_row()
            for c, val in enumerate(row):
                item = QTableWidgetItem(val)
                item.setToolTip(info.path)
                if (base is not None and r > 0 and c not in skip
                        and c < len(base) and val != base[c]):
                    header = FILE_HEADERS[c] if c < len(FILE_HEADERS) else ""
                    if header in HARMLESS_DIFF_HEADERS:
                        # 不影响合并的差异：说清楚"不用管"，
                        # 并明确"转码也统一不了"，免得用户白折腾。
                        _bg, _fg = diff_colors("harmless")
                        item.setBackground(QColor(_bg))
                        item.setForeground(QColor(_fg))
                        item.setToolTip(
                            f"{info.path}\n\n"
                            f"「{header}」与第一个文件不同：\n"
                            f"  第一个：{base[c]}\n"
                            f"  本文件：{val}\n\n"
                            "此项由编码器按内容自动计算，无法手动指定，\n"
                            "转码也不会与多数派变得一致。\n\n"
                            "✅ 不影响无损合并，无需处理。")
                    else:
                        # 与第一个文件不同 → 标黄，提示"这一项不一致"
                        _bg, _fg = diff_colors("diff")
                        item.setBackground(QColor(_bg))
                        item.setForeground(QColor(_fg))
                        item.setToolTip(
                            f"{info.path}\n\n"
                            f"「{header}」与第一个文件不同：\n"
                            f"  第一个：{base[c]}\n"
                            f"  本文件：{val}\n\n"
                            "参数不一致 → 无法无损合并，需要转码。")
                self.tbl_files.setItem(r, c, item)
        from core.i18n import tr
        self.set_status(tr("当前列表：") + f"{len(self.infos)}"
                        + tr(" 个文件，总时长 ") + f"{self.total_duration_str()}"
                        + tr("，总大小 ") + f"{self.total_size_str()}")

    def total_duration_str(self) -> str:
        total = sum(i.duration or 0 for i in self.infos)
        h = int(total // 3600)
        m = int((total % 3600) // 60)
        s = total % 60
        return f"{h:d}:{m:02d}:{s:05.2f}" if h else f"{m:02d}:{s:05.2f}"

    def total_size_str(self) -> str:
        total = float(sum(i.size or 0 for i in self.infos))
        for unit in ("B", "KB", "MB", "GB", "TB"):
            if total < 1024 or unit == "TB":
                return f"{total:.1f} {unit}"
            total /= 1024.0
        return "-"

    def resort_files(self) -> None:
        self.infos.sort(key=lambda i: natural_key(i.name))

    def remove_selected(self) -> None:
        rows = sorted({idx.row() for idx in self.tbl_files.selectedIndexes()}, reverse=True)
        for r in rows:
            if 0 <= r < len(self.infos):
                self.infos.pop(r)
        self.refresh_file_table()

    def move_selected(self, delta: int) -> None:
        row = self.tbl_files.currentRow()
        new = row + delta
        if row < 0 or not (0 <= new < len(self.infos)):
            return
        self.infos[row], self.infos[new] = self.infos[new], self.infos[row]
        self.refresh_file_table()
        self.tbl_files.selectRow(new)

    def clear_files(self) -> None:
        self.infos.clear()
        self.refresh_file_table()
        self.tbl_selected.setRowCount(0)
        self.btn_apply_params.setEnabled(False)

    def on_file_selected(self) -> None:
        """需求：选中某个视频 → 显示它的完整参数，便于照着设置转码参数。"""
        rows = {idx.row() for idx in self.tbl_files.selectedIndexes()}
        self.tbl_selected.setRowCount(0)
        if len(rows) != 1:
            self.btn_apply_params.setEnabled(False)
            if len(rows) > 1:
                self._set_selected_rows([["已选中多个文件", f"共 {len(rows)} 个，单个选中可查看详细参数"]])
            return
        info = self.infos[list(rows)[0]]
        self._set_selected_rows(info.detail_items())
        self.btn_apply_params.setEnabled(True)

    def _set_selected_rows(self, rows: Sequence[Sequence[str]]) -> None:
        self.tbl_selected.setRowCount(0)
        for r, (k, v) in enumerate(rows):
            self.tbl_selected.insertRow(r)
            self.tbl_selected.setItem(r, 0, QTableWidgetItem(str(k)))
            self.tbl_selected.setItem(r, 1, QTableWidgetItem(str(v)))
        self.tbl_selected.resizeRowsToContents()

    def apply_selected_to_params(self) -> None:
        rows = {idx.row() for idx in self.tbl_files.selectedIndexes()}
        if len(rows) != 1:
            return
        info = self.infos[list(rows)[0]]
        self.encode_panel.fill_from_video(info, self._available_encoders())
        self.tabs.setCurrentIndex(0)
        hw = self.encode_panel.chk_prefer_hw.isChecked()
        got = self.encode_panel.params.vcodec
        self.set_status(f"已按「{info.name}」的参数填充编码设置"
                        f"（编码器：{got}"
                        f"{'，硬件' if hw else '，软件'}）")

    def _reveal_selected(self) -> None:
        row = self.tbl_files.currentRow()
        if row < 0 or row >= len(self.infos):
            return
        path = self.infos[row].path
        folder = os.path.dirname(os.path.abspath(path))
        if sys.platform.startswith("win"):
            subprocess.Popen(["explorer", "/select,", os.path.normpath(path)])
        elif sys.platform == "darwin":
            subprocess.Popen(["open", "-R", path])
        else:
            subprocess.Popen(["xdg-open", folder])

    # ==================================================================
    # 任务队列
    # ==================================================================
    def _make_task(self, infos: List[VideoInfo], name: str, source_dir: str,
                   baseline_first: bool = False) -> MergeTask:
        params: EncodeParams = copy.deepcopy(self.encode_panel.params)
        output: OutputParams = copy.deepcopy(self.output_panel.output)
        if not output.out_dir:
            output.out_dir = source_dir
        if not output.out_name:
            output.out_name = name or "合并输出"
        if output.extract_audio:
            if not output.audio_dir:
                output.audio_dir = output.out_dir
            if not output.audio_name or output.sync_audio_name:
                output.audio_name = output.out_name
        # 转码路径下，若「保持原始」类参数遇到源参数不一致，自动统一
        # （否则转码完依然不一致，拼出来的文件在多数播放器里会花屏）
        will_transcode = True
        if params.check_lossless:
            compat = check_lossless(infos)
            will_transcode = not compat.compatible
        else:
            compat = None

        task_notes: List[str] = []
        if baseline_first and infos and will_transcode:
            # 批量导入：以该文件夹第一个视频的参数为基准统一转码
            params = copy.deepcopy(params)
            # 传硬件候选，否则批量导入只会选出软编码器，
            # 与「用此文件参数填充」的结果不一致（那边能选到 hevc_amf）
            try:
                params.fill_from_video_baseline(infos[0],
                                                self._available_encoders())
            except TypeError:  # noqa: BLE001
                params.fill_from_video_baseline(infos[0])
            first = infos[0]
            task_notes.append(
                f"以首个文件「{first.name}」的参数为转码基准："
                f"{first.width}x{first.height}"
                + (f" @ {first.fps:g}fps" if first.fps else "")
                + (f"，音频 {first.a_sample_rate}Hz/{first.a_channels}ch"
                   if first.has_audio and first.a_sample_rate else ""))

        norm = normalize_for_merge(params, infos, will_transcode=will_transcode)
        if norm.changed:
            params = norm.params
            task_notes.extend(norm.notes)

        task = MergeTask(name=name or output.out_name, files=list(infos),
                         params=params, output=output)
        if compat is not None:
            task.compat = compat
        if task_notes:
            task.auto_notes = task_notes
            for n in task_notes:
                task.add_log(f"[参数自动统一] {n}")
        return task

    def add_task(self) -> None:
        if not self.infos:
            QMessageBox.information(self, "列表为空", "请先导入视频文件。")
            return
        if not self.env.status.ok:
            QMessageBox.warning(self, "ffmpeg 未就绪", self.env.status.message)
            return
        source_dir = os.path.dirname(self.infos[0].path)
        name = self.output_panel.edit_name.text().strip() or os.path.basename(source_dir.rstrip(os.sep))
        task = self._make_task(copy.deepcopy(self.infos), name, source_dir)
        self._enqueue(task)
        self.set_status(f"任务「{task.name}」已加入队列，位置 #{len(self.tasks)}"
                        + (f"（无损检测：{task.compat.summary}）" if task.compat.checked else ""))
        # 加入后自动清空列表，方便继续添加下一批
        self.clear_files()
        self.output_panel.reset_for_new_source("", "")

    def _enqueue(self, task: MergeTask) -> None:
        self.tasks.append(task)
        self.refresh_queue_table()

    def refresh_queue_table(self) -> None:
        from core import theme as _theme_mod  # 本函数要用 current_theme()，必须在此导入
        sel_id = self._selected_task_id()
        self.tbl_queue.setRowCount(0)
        for r, t in enumerate(self.tasks):
            self.tbl_queue.insertRow(r)
            values = [t.name, f"{len(t.files)} 个", t.total_duration_str, t.mode_str, t.status, "",
                      os.path.basename(t.output_path) if t.output_path else
                      f"{t.output.out_name}.{t.output.container}"]
            for c, val in enumerate(values):
                item = QTableWidgetItem(str(val))
                item.setData(Qt.UserRole, t.id)
                if c == 4:
                    # 两套配色：深色主题下原来的深绿/深蓝压在深底上几乎看不见。
                    # 顺带修一个老问题 —— 这里算出的 color 原本根本没被用上
                    # （写死成黑色），状态圆点一直显示不出颜色，等于白标。
                    color = ({
                        STATUS.DONE: "#188038", STATUS.FAILED: "#d93025",
                        STATUS.RUNNING: "#1a73e8", STATUS.PAUSED: "#e8710a",
                    } if _theme_mod.current_theme() != "dark" else {
                        STATUS.DONE: "#5ddb8a", STATUS.FAILED: "#ff8a80",
                        STATUS.RUNNING: "#7fb3ff", STATUS.PAUSED: "#ffb066",
                    }).get(t.status)
                    if color:
                        item.setForeground(QColor(color))
                        item.setText(f"● {val}")
                    item.setToolTip(t.error or "")
                self.tbl_queue.setItem(r, c, item)
            bar = QProgressBar()
            bar.setRange(0, 100)
            bar.setValue(int(t.progress * 100))
            bar.setTextVisible(True)
            bar.setFormat(f"{t.progress * 100:.1f}%  {t.stage}")
            self.tbl_queue.setCellWidget(r, 5, bar)
            if sel_id and t.id == sel_id:
                self.tbl_queue.selectRow(r)
        self._update_queue_buttons()

    def _selected_task_id(self) -> Optional[str]:
        row = self.tbl_queue.currentRow()
        if row < 0 or row >= len(self.tasks):
            return None
        return self.tasks[row].id

    def get_task(self, task_id: str) -> Optional[MergeTask]:
        for t in self.tasks:
            if t.id == task_id:
                return t
        return None

    def _update_queue_buttons(self) -> None:
        running = self.worker is not None and self.worker.isRunning()
        has_pending = any(t.status == STATUS.PENDING for t in self.tasks)
        self.btn_start.setEnabled(not running and has_pending)
        self.btn_pause.setEnabled(running)
        from core.i18n import tr as _tr
        self.btn_pause.setText(_tr("⏵ 继续") if (self.worker and self.worker.is_paused) else _tr("⏸ 暂停"))
        self.btn_stop.setEnabled(running)

    # ------------------------------------------------------------------
    def start_queue(self) -> None:
        if not self.env.status.ok:
            QMessageBox.warning(self, "ffmpeg 未就绪", self.env.status.message)
            return
        if self.worker is not None and self.worker.isRunning():
            return
        if not any(t.status == STATUS.PENDING for t in self.tasks):
            QMessageBox.information(self, "队列为空", "没有等待中的任务。请先把文件加入队列。")
            return
        self.worker = QueueWorker(self.env.status.ffmpeg, self.env.status.ffprobe,
                                  self._next_pending, self)
        self.worker.sig_status.connect(self.on_task_status)
        self.worker.sig_stage.connect(self.on_task_stage)
        self.worker.sig_progress.connect(self.on_task_progress)
        self.worker.sig_log.connect(self.on_task_log)
        self.worker.sig_finished.connect(self.on_queue_finished)
        self.worker.start()
        self.set_status("队列开始处理…")
        self._update_queue_buttons()

    def _next_pending(self) -> Optional[MergeTask]:
        for t in self.tasks:
            if t.status == STATUS.PENDING:
                return t
        return None

    def toggle_pause(self) -> None:
        if not self.worker:
            return
        self.worker.set_paused(not self.worker.is_paused)
        self.set_status("已暂停（当前文件处理完后暂停）" if self.worker.is_paused else "继续处理")
        self._update_queue_buttons()

    def stop_queue(self) -> None:
        if not self.worker:
            return
        reply = QMessageBox.question(self, "停止队列", "停止后当前任务会被取消，已生成的输出文件会保留。确定停止吗？")
        if reply != QMessageBox.Yes:
            return
        self.worker.request_stop()
        self.set_status("正在停止…")

    def _selected_task_ids(self) -> List[str]:
        """当前选中的任务 id 列表（按行号升序）。

        用 selectedIndexes 而不是 selectedItems：
        用户可能只点了某一行的某一格，也可能整行选中，
        取索引再按行号去重最稳。
        """
        rows = sorted({idx.row() for idx in self.tbl_queue.selectedIndexes()})
        ids = []
        for r in rows:
            if 0 <= r < len(self.tasks):
                tid = self.tasks[r].id
                if tid not in ids:
                    ids.append(tid)
        return ids

    def remove_task(self) -> None:
        """移除选中的任务（支持多选）。"""
        tids = self._selected_task_ids()
        if not tids:
            return
        # 先看有没有正在跑的，有的话要问一句 —— 移除等于取消
        running = [t for t in (self.get_task(i) for i in tids)
                   if t and t.status == STATUS.RUNNING]
        if running:
            n = len(tids)
            reply = QMessageBox.question(
                self, "任务正在运行",
                f"选中的 {n} 个任务里有 {len(running)} 个正在处理，移除会取消它们。\n"
                "确定移除吗？")
            if reply != QMessageBox.Yes:
                return

        for tid in tids:
            task = self.get_task(tid)
            if not task:
                continue
            if task.status == STATUS.RUNNING and self.worker:
                self.worker.cancel_task(tid)
                task.status = STATUS.CANCELED
            dlg = self.dialogs.pop(tid, None)
            if dlg:
                dlg.close()

        removing = set(tids)
        self.tasks = [t for t in self.tasks if t.id not in removing]
        self.refresh_queue_table()
        self.set_status(f"已移除 {len(removing)} 个任务")

    def clear_finished(self) -> None:
        before = len(self.tasks)
        self.tasks = [t for t in self.tasks if not STATUS.finished(t.status)]
        removed = before - len(self.tasks)
        self.refresh_queue_table()

        # 队列清空 = 这批素材处理完了。
        # 此时**必须**清掉输出名/目录，否则残留的旧值会被下一批任务
        # 继承 —— 新任务被写成上一批的名字、存到上一批的目录。
        # 这正是用户反馈的"输出设置残留"。
        reset_hint = ""
        if removed and not self.tasks:
            self.output_panel.reset_output_settings()
            reset_hint = "（已清空输出名/目录，避免下一批沿用）"

        from core.i18n import tr as _tr

        # 状态栏是一次性 setText，切语言时不会自己重翻 —— 界面上会残留
        # 一句中文。这里把"怎么重建这句话"记下来，交给 retranslate_dynamic。
        def _build():
            _h = ""
            if removed and not self.tasks:
                _h = _tr("（已清空输出名/目录，避免下一批沿用）")
            return _tr("已清理") + f" {removed} " + _tr("个已结束的任务") + _h

        self.set_status(_build(), i18n_parts=_build)

    # ------------------------------------------------------------------
    def open_task_dialog(self) -> None:
        """打开任务详情。

        选中多个时打开「当前行」那个（也就是最后点到的那行），
        并提示一下：不会因为多选而报错或什么都不做。
        """
        tid = self._selected_task_id()
        if not tid:
            QMessageBox.information(self, "请选择任务", "先在队列里选中一个任务。")
            return
        if len(self._selected_task_ids()) > 1:
            self.set_status("选中了多个任务，这里打开的是最后点击的那个；"
                            "双击某一行可直接打开该任务。")
        self._open_dialog_by_id(tid)

    def _open_dialog_by_id(self, tid: str) -> None:
        dlg = self.dialogs.get(tid)
        task = self.get_task(tid)
        if task is None:
            return
        if dlg is None:
            dlg = TaskDialog(task, self)
            dlg.sig_cancel_task.connect(self._cancel_task_by_id)
            self.dialogs[tid] = dlg
        dlg.refresh_all()
        dlg.show()
        dlg.raise_()
        dlg.activateWindow()

    def _on_task_dialog_closed(self, task_id: str) -> None:
        self.dialogs.pop(task_id, None)

    def _cancel_task_by_id(self, tid: str) -> None:
        task = self.get_task(tid)
        if not task:
            return
        if task.status == STATUS.RUNNING and self.worker:
            self.worker.cancel_task(tid)
            self.set_status(f"正在取消任务「{task.name}」…")
        else:
            task.status = STATUS.CANCELED
            self.refresh_queue_table()

    # ==================================================================
    # worker 回调
    # ==================================================================
    def on_task_status(self, task_id: str, status: str) -> None:
        task = self.get_task(task_id)
        if task:
            task.status = status
            self.refresh_queue_row(task_id)
        dlg = self.dialogs.get(task_id)
        if dlg:
            dlg.on_status(status)
        if status == STATUS.RUNNING and self.chk_auto_open.isChecked():
            # 延迟到事件循环空闲再创建窗口，避免在信号回调中做重量级 UI 操作
            QTimer.singleShot(0, lambda tid=task_id: self._open_dialog_by_id(tid))
        if status in (STATUS.DONE, STATUS.FAILED, STATUS.CANCELED):
            self._notify_task_end(task, status)
        self._update_queue_buttons()

    def on_task_stage(self, task_id: str, stage: str) -> None:
        task = self.get_task(task_id)
        if task:
            self.refresh_queue_row(task_id)
        dlg = self.dialogs.get(task_id)
        if dlg:
            dlg.on_stage(stage)
        if task:
            self.set_status(f"[{task.name}] {stage}")

    def on_task_progress(self, task_id: str, value: float, eta: str) -> None:
        task = self.get_task(task_id)
        if task:
            self.refresh_queue_row(task_id)
        dlg = self.dialogs.get(task_id)
        if dlg:
            dlg.on_progress(value, eta)

    def on_task_log(self, task_id: str, line: str) -> None:
        dlg = self.dialogs.get(task_id)
        if dlg:
            dlg.on_log(line)

    def on_queue_finished(self, done: int, failed: int, canceled: int) -> None:
        self.set_status(f"队列处理结束：完成 {done} 个，失败 {failed} 个，取消 {canceled} 个")
        self.refresh_queue_table()
        self._update_queue_buttons()
        if failed == 0 and done > 0:
            QMessageBox.information(self, "队列完成",
                                    f"全部完成：{done} 个任务\n输出文件在各自设置的输出文件夹中。")

    def _notify_task_end(self, task: Optional[MergeTask], status: str) -> None:
        if task is None:
            return
        if status == STATUS.FAILED:
            self.set_status(f"任务「{task.name}」失败：{task.error[:120]}")
        elif status == STATUS.DONE:
            extra = f"，音频 {os.path.basename(task.audio_path)}" if task.audio_path else ""
            self.set_status(f"任务「{task.name}」完成 → {os.path.basename(task.output_path)}{extra}")

    def refresh_queue_row(self, task_id: str) -> None:
        for r, t in enumerate(self.tasks):
            if t.id == task_id:
                if r >= self.tbl_queue.rowCount():
                    self.refresh_queue_table()
                    return
                item = self.tbl_queue.item(r, 3)
                if item:
                    item.setText(t.mode_str)
                item = self.tbl_queue.item(r, 4)
                if item:
                    item.setText(f"● {t.status}")
                    item.setToolTip(t.error or "")
                bar = self.tbl_queue.cellWidget(r, 5)
                if isinstance(bar, QProgressBar):
                    bar.setValue(int(t.progress * 100))
                    bar.setFormat(f"{t.progress * 100:.1f}%  {t.stage}")
                item = self.tbl_queue.item(r, 6)
                if item:
                    item.setText(os.path.basename(t.output_path) if t.output_path
                                 else f"{t.output.out_name}.{t.output.container}")
                break

    # ==================================================================
    # ffmpeg / 设置 / 其它
    # ==================================================================
    def open_ffmpeg_dialog(self) -> None:
        """打开 ffmpeg 组件窗口。

        关闭后**不要**无条件刷新编码面板 —— 实测 `_apply_env_to_panels()`
        要花 1.1 秒（它会重新枚举全部编码器，而每个编码器都可能
        触发一次子进程调用）。这就是为什么"点开组件窗口再关闭会卡一下"。

        只有 ffmpeg 真的变了才需要刷新；没变就什么都不做，关闭瞬间完成。
        """
        before = (self.env.status.ffmpeg, self.env.status.ffprobe)
        dlg = FFmpegDialog(self.env, self.config, self)
        dlg.exec()
        self.refresh_ffmpeg_label()
        after = (self.env.status.ffmpeg, self.env.status.ffprobe)
        if after != before:
            self._apply_env_to_panels()   # 确实换了 ffmpeg，重建编码器列表

    def _apply_splitter_sizes(self) -> None:
        """给队列区留出足够高度。

        只用 setSizes 是没用的：splitter 会按子控件的 minimumHeight
        夹逼，上方编码参数区内容多、最小高度大，队列区就被挤到只剩 15%。
        所以这里显式给队列区设一个最小高度（不低于窗口的 34%，
        且至少 240 px），splitter 就必须给它腾出空间。
        """
        # 用固定下限而非百分比：按比例算会把窗口撑得比屏幕还高
        # （上方编码参数区最小高度固定，队列再按比例加码就溢出了）。
        min_down = 220
        self.gb_queue.setMinimumHeight(min_down)
        total = sum(self.splitter_main.sizes())
        if total > 0:
            down = max(min_down, min(int(total * self._queue_ratio),
                                     max(min_down + 60, int(total * 0.45))))
            self.splitter_main.setSizes([max(1, total - down), down])

    def _maybe_suggest_better_ffmpeg(self) -> None:
        """启动时若发现 PATH 里有硬件编码能力更强的 ffmpeg，主动提示。

        这是解决"换了几个 ffmpeg 都没效果"的最后一道保险：
        软件默认用自带的 vendor 版本，而它可能不带硬编，
        用户根本意识不到自己 PATH 里那个更好的没被用上。
        """
        cfg = self.config
        if getattr(cfg, "hw_switch_hinted", False):
            return
        if not self.env.status.ok:
            return

        # 关键：candidate_ffmpegs 会为每个候选启动子进程，绝不能在主线程跑。
        # 放到后台线程，完成后再用信号回到主线程决定是否弹窗。
        th = CandidateScanThread(self.env.status.ffmpeg, self)
        th.sig_done.connect(self._on_candidates_scanned)
        self._cand_thread = th          # 持有引用，防止被 GC
        th.start()

    def _on_candidates_scanned(self, cands) -> None:
        if not cands:
            return
        self._cand_thread = None
        cur = next((c for c in cands if c.get("is_current")), None)
        if cur is None:
            return
        # 关键：按**家族**比，不能比数量。
        # 自带的可能有 16 个硬编（nvenc/qsv/vaapi），用户 PATH 里只有 3 个，
        # 但对 AMD 用户来说，那 3 个全是 AMF 才是有用的。
        from core.ffmpeg_env import missing_families
        better = []
        for c in cands:
            if c.get("is_current"):
                continue
            miss = missing_families(list(cur["hw"]), list(c["hw"]))
            if miss:
                better.append((len(miss), c, miss))
        if not better:
            return
        better.sort(key=lambda t: -t[0])
        _n, best, miss = better[0]
        names = ", ".join(str(x) for x in list(best["hw"])[:6])
        fam_cn = {"amf": "AMD (AMF)", "nvenc": "NVIDIA (NVENC)",
                  "qsv": "Intel (QSV)", "vaapi": "VAAPI",
                  "videotoolbox": "Apple"}
        miss_cn = "、".join(fam_cn.get(m, m) for m in miss)
        ans = QMessageBox.question(
            self, "发现功能更全的 ffmpeg",
            f"软件当前用的是这个：\n"
            f"  {cur['path']}\n"
            f"  （{cur['source']}，共 {cur['hw_count']} 个硬件编码器）\n"
            f"  但它不支持：{miss_cn}\n\n"
            f"这台机器上还有一个支持 {miss_cn} 的：\n"
            f"  {best['path']}\n"
            f"  （{best['source']}，版本 {best['version']}）\n"
            f"  硬件编码器：{names}\n\n"
            f"这正是「换了几个 ffmpeg 都没效果」的典型原因 —— "
            f"软件一直在用它自带的那个，没用上你 PATH 里的。\n\n"
            f"要切换过去吗？（之后可随时在「ffmpeg 状态」→"
            f"「选择用哪个 ffmpeg…」改回来）",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.Yes)
        cfg = self.config
        cfg.hw_switch_hinted = True
        cfg.save()
        if ans != QMessageBox.Yes:
            return
        from core.ffmpeg_sources import _find_ffprobe
        path = str(best["path"])
        self.config.ffmpeg_path = path
        self.config.ffprobe_path = _find_ffprobe(path)
        self.config.save()
        self.env.ffmpeg_path = path
        self.env.ffprobe_path = self.config.ffprobe_path
        self.env.refresh()
        self.refresh_ffmpeg_label()
        self._apply_env_to_panels()
        self.set_status(f"已切换 ffmpeg：{path}")

    def _apply_env_to_panels(self) -> None:
        if self.env.status.ok:
            self.encode_panel.set_ffmpeg(self.env.status.ffmpeg,
                                        self.env.status.ffprobe)
            self.encode_panel.set_video_encoders(self.env.available_video_encoders())
            self.encode_panel.set_audio_encoders(
                self.env.available_audio_encoders(),
                prefer_best=getattr(self, "_first_run", False))

    def refresh_ffmpeg_label(self) -> None:
        # tr 必须在这里（函数开头）导入：写在 if 分支里会让它变成局部变量，
        # ffmpeg 未就绪走 else 分支时就 UnboundLocalError —— 软件在没装
        # ffmpeg 的机器上一打开就崩。
        from core.i18n import tr, _normalize_punct, current_lang
        st = self.env.status
        if st.ok:
            # 把实际路径一起显示出来：很多人换了 ffmpeg 却不知道软件
            # 到底在用哪个，路径摆在明面上才不会白折腾。
            # 右括号是硬编码的全角，英文界面下会留一个孤零零的「）」，
            # 所以整句拼完后再统一规范化一次标点。
            _txt = f"ffmpeg {st.version}" + tr("（来源：") \
                + tr(st.source or "") + "）"
            if current_lang() != "zh":
                _txt = _normalize_punct(_txt)
            self.lbl_ff.setText(_txt)
            self.lbl_ff.setToolTip(
                tr("软件实际使用的 ffmpeg：") + "\n"
                f"  {st.ffmpeg}\n"
                f"  {st.ffprobe}\n\n"
                + tr("如果你想换成另一个 ffmpeg，点这里 →「手动选择 ffmpeg」"
                     "或「从其他软件借用 ffmpeg」。"))
            self.lbl_ff.setStyleSheet(f"color:{_sem('ok')};")
        else:
            self.lbl_ff.setText(tr("ffmpeg 未就绪 —— 点击右上角「ffmpeg 状态」安装"))
            self.lbl_ff.setStyleSheet(f"color:{_sem('error')};")

    def _check_ffmpeg_on_start(self) -> None:
        if not self.env.status.ok:
            reply = QMessageBox.question(
                self, "未找到 ffmpeg",
                f"{self.env.status.message}\n\n是否现在打开组件安装窗口？（可自动下载，也可以手动指定已有 ffmpeg）")
            if reply == QMessageBox.Yes:
                self.open_ffmpeg_dialog()
        else:
            self._apply_env_to_panels()

    def set_status(self, text: str, *, i18n_parts=None) -> None:
        """i18n_parts：可调用对象，返回当前语言下的完整句子。

        不传就当作一次性文案（比如报错原文），切换语言时不会去动它，
        免得把一条本来就正确的话改坏。
        """
        self._status_i18n = i18n_parts
        self.lbl_status.setText(text)

    # ------------------------------------------------------------------
    def _restore_settings(self) -> None:
        cfg = self.config
        if cfg.last_params:
            try:
                self.encode_panel.set_params(EncodeParams.from_dict(cfg.last_params))
            except Exception:  # noqa: BLE001
                pass
        if cfg.last_output:
            try:
                valid = set(OutputParams.__dataclass_fields__)  # type: ignore[attr-defined]
                self.output_panel.set_output(
                    OutputParams(**{k: v for k, v in cfg.last_output.items() if k in valid}))
            except Exception:  # noqa: BLE001
                pass
        self.chk_recursive.setChecked(cfg.recursive_scan)
        if cfg.window_geometry:
            self.restoreGeometry(bytes.fromhex(cfg.window_geometry))

    def closeEvent(self, event) -> None:  # noqa: N802
        if self.worker is not None and self.worker.isRunning():
            reply = QMessageBox.question(self, "正在处理任务",
                                         "队列中还有任务正在处理，确定要退出吗？（当前任务会被取消）")
            if reply != QMessageBox.Yes:
                event.ignore()
                return
            self.worker.request_stop()
            self.worker.wait(5000)
        self.config.last_params = self.encode_panel.params.to_dict()
        valid = set(OutputParams.__dataclass_fields__)  # type: ignore[attr-defined]
        out = self.output_panel.output
        self.config.last_output = {k: v for k, v in out.__dict__.items() if k in valid}
        self.config.recursive_scan = self.chk_recursive.isChecked()
        self.config.window_geometry = self.saveGeometry().toHex().data().decode()
        self.config.save()
        for dlg in list(self.dialogs.values()):
            dlg.close()
        # 等所有后台线程收尾，否则退出时会报
        # "QThread: Destroyed while thread is still running"
        try:
            if hasattr(self, "encode_panel"):
                self.encode_panel.stop_probe()
        except Exception:  # noqa: BLE001
            pass
        # 候选 ffmpeg 扫描线程（candidate_ffmpegs 会为每个候选启动子进程，
        # 每个都可能耗时几百毫秒，不能强行中断）
        th = getattr(self, "_cand_thread", None)
        if th is not None and th.isRunning():
            th.wait(10000)
            self._cand_thread = None
        event.accept()


# --- 差异标记的配色（背景与前景必须成对给） ---
# 之前写死浅色底（#fff4c2 浅黄 / #e8f0fe 浅蓝），且从不设置前景色。
# 深色主题把表格正文改成浅色字后 → 浅底压浅字，肉眼完全看不清。
def diff_colors(kind="diff"):
    """返回 (背景色, 前景色)。kind: 'diff' 真差异 / 'harmless' 无害差异。"""
    try:
        from core.theme import diff_colors as _dc
        return _dc(kind)
    except Exception:
        # 兜底也不能写死浅色：默认主题是深色，浅底压浅字会看不见
        try:
            from core.theme import current_theme as _ct
            _dark = (_ct() == "dark")
        except Exception:
            _dark = True
        if kind == "diff":
            return ("#5c4a00", "#ffe9a8") if _dark else ("#fff4c2", "#1a1a1a")
        return ("#1e3a5f", "#a8c8ff") if _dark else ("#e8f0fe", "#1a1a1a")
