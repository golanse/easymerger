"""语言设置面板（独立的「语言」标签页）。

与「视图 → 语言」菜单是同一件事的两个入口：
菜单方便老用户随手切换，标签页则让新用户一眼就能看到这个设置。
两处共用同一套切换逻辑（MainWindow._switch_lang）。
"""

from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (QButtonGroup, QFrame, QHBoxLayout, QLabel,
                               QRadioButton, QVBoxLayout, QWidget)

from core.i18n import LANGUAGES, detect_system_lang, system_locale_raw


class LanguagePanel(QWidget):
    """界面语言切换面板。"""

    # 用户改了语言 → 交给主窗口统一处理（写配置 + 刷新全部界面）
    sig_lang_changed = Signal(str)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._updating = False          # 防止 set_checked 触发信号回环
        self._radios: dict[str, QRadioButton] = {}

        root = QVBoxLayout(self)
        root.setContentsMargins(12, 12, 12, 12)
        root.setSpacing(10)

        title = QLabel("界面语言")
        title.setStyleSheet("font-size:14px; font-weight:600;")
        root.addWidget(title)

        desc = QLabel("选择界面显示语言，切换后立即生效。\n"
                      "首次启动会自动跟随系统语言，之后以你的选择为准。")
        desc.setWordWrap(True)
        try:
            from core.theme import muted_fg as _mf
            desc.setStyleSheet(f"color:{_mf()};")
        except Exception:  # noqa: BLE001
            # 兜底也跟随主题：#666 在深色下几乎看不见
            try:
                from core.theme import current_theme as _ct
                _c = "#9aa0a6" if _ct() == "dark" else "#666666"
            except Exception:  # noqa: BLE001
                _c = "#666666"
            desc.setStyleSheet(f"color:{_c};")
        root.addWidget(desc)

        line = QFrame()
        line.setFrameShape(QFrame.HLine)
        try:
            from core.theme import muted_fg as _mf2
            line.setStyleSheet(f"color:{_mf2()};")
        except Exception:  # noqa: BLE001
            line.setStyleSheet("color:#ddd;")
        root.addWidget(line)

        self.group = QButtonGroup(self)
        self.group.setExclusive(True)
        for key, label in LANGUAGES.items():
            rb = QRadioButton(label)
            rb.toggled.connect(lambda checked, k=key: self._on_toggle(k, checked))
            self.group.addButton(rb)
            self._radios[key] = rb
            root.addWidget(rb)

        # 显示检测结果：用户如果对"为什么默认是英文"有疑问，这里能自证
        raw = system_locale_raw()
        detected = LANGUAGES.get(detect_system_lang(), "")
        shown = f"{detected}（{raw}）" if raw else detected
        self.lbl_detected = QLabel("当前系统语言：" + shown)
        self.lbl_detected.setWordWrap(True)
        self.lbl_detected.setStyleSheet("color:#888; font-size:11px;")
        root.addWidget(self.lbl_detected)

        root.addStretch(1)

    # ------------------------------------------------------------------
    def _on_toggle(self, key: str, checked: bool) -> None:
        if self._updating or not checked:
            return
        self.sig_lang_changed.emit(key)

    def set_checked(self, key: str) -> None:
        """外部（如菜单切换后）同步选中状态，不触发信号。"""
        rb = self._radios.get(key)
        if rb is None:
            return
        self._updating = True
        try:
            rb.setChecked(True)
        finally:
            self._updating = False
