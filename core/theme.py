"""界面主题（浅色 / 深色）。

用 QSS（Qt Style Sheet，语法接近 CSS）实现，配合 Fusion 风格，
不依赖系统主题，Windows / Linux / macOS 上表现一致。

设计原则：
- 只定义**配色 + 圆角 + 间距**，不改动任何布局代码；
- 两套主题用同一套结构，只换颜色变量，方便后续加第三套；
- 表格、分组框、按钮、下拉框这些"最影响观感"的控件重点调过。
"""

from __future__ import annotations

__all__ = ["THEMES", "apply_theme", "qss_for", "load_theme_from_config",
           "save_theme_to_config", "current_theme"]

THEMES = {"light": "浅色", "dark": "深色"}

_theme = "dark"

# ==================================================================
# 浅色
# ==================================================================
_LIGHT = """
/* ---------- 全局 ---------- */
QWidget {
    background-color: #f7f8fa;
    color: #1f2328;
    font-size: 13px;
}
QMainWindow, QDialog {
    background-color: #f7f8fa;
}

/* ---------- 分组框 ---------- */
QGroupBox {
    border: 1px solid #d8dce3;
    border-radius: 8px;
    margin-top: 14px;
    padding-top: 8px;
    background-color: #ffffff;
    font-weight: 600;
}
QGroupBox::title {
    subcontrol-origin: margin;
    subcontrol-position: top left;
    left: 12px;
    padding: 0 6px;
    color: #2c6bed;
}

/* ---------- 按钮 ---------- */
QPushButton {
    background-color: #ffffff;
    border: 1px solid #ccd2da;
    border-radius: 6px;
    padding: 6px 14px;
    min-height: 20px;
}
QPushButton:hover {
    background-color: #eef4ff;
    border-color: #2c6bed;
}
QPushButton:pressed {
    background-color: #dbe7ff;
}
QPushButton:disabled {
    color: #9aa3ad;
    background-color: #f2f3f5;
    border-color: #e2e5ea;
}

/* ---------- 输入控件 ---------- */
QLineEdit, QSpinBox, QDoubleSpinBox, QComboBox {
    background-color: #ffffff;
    border: 1px solid #ccd2da;
    border-radius: 6px;
    padding: 4px 8px;
    min-height: 20px;
}
QLineEdit:focus, QSpinBox:focus, QDoubleSpinBox:focus, QComboBox:focus {
    border-color: #2c6bed;
}
QComboBox::drop-down {
    border: none;
    width: 18px;
}
QComboBox QAbstractItemView {
    background-color: #ffffff;
    border: 1px solid #ccd2da;
    selection-background-color: #eef4ff;
    selection-color: #1f2328;
    outline: 0px;
}

/* ---------- 表格 ---------- */
QTableWidget, QTableView {
    background-color: #ffffff;
    alternate-background-color: #fafbfc;
    gridline-color: #e8ebef;
    border: 1px solid #dfe3e8;
    border-radius: 6px;
    selection-background-color: #dbe7ff;
    selection-color: #1f2328;
    outline: 0px;
}
QHeaderView::section {
    background-color: #eff1f4;
    color: #4a5158;
    border: none;
    border-right: 1px solid #e2e5ea;
    border-bottom: 1px solid #dfe3e8;
    padding: 5px 8px;
    font-weight: 600;
}

/* ---------- 页签 ---------- */
QTabWidget::pane {
    border: 1px solid #dfe3e8;
    border-radius: 6px;
    background-color: #ffffff;
    top: -1px;
}
QTabBar::tab {
    background-color: #eff1f4;
    border: 1px solid #dfe3e8;
    padding: 6px 16px;
    margin-right: 2px;
    border-top-left-radius: 6px;
    border-top-right-radius: 6px;
}
QTabBar::tab:selected {
    background-color: #ffffff;
    color: #2c6bed;
    font-weight: 600;
    border-bottom-color: #ffffff;
}

/* ---------- 滚动条 ---------- */
QScrollBar:vertical {
    background: #f2f3f5;
    width: 10px;
    margin: 0;
    border: none;
}
QScrollBar::handle:vertical {
    background: #c4cad2;
    border-radius: 5px;
    min-height: 24px;
}
QScrollBar::handle:vertical:hover { background: #a8b0ba; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
QScrollBar:horizontal {
    background: #f2f3f5;
    height: 10px;
    border: none;
}
QScrollBar::handle:horizontal {
    background: #c4cad2;
    border-radius: 5px;
    min-width: 24px;
}
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal { width: 0; }

/* ---------- 其它 ---------- */
QCheckBox, QRadioButton { spacing: 6px; }
QCheckBox::indicator {
    width: 15px; height: 15px;
    border: 1px solid #b6bdc6;
    border-radius: 3px;
    background-color: #ffffff;
}
QCheckBox::indicator:checked {
    background-color: #2c6bed;
    border-color: #2c6bed;
}
/* 单选框指示器（浅色）：与深色版成对维护，保证两套主题下圆圈都看得见。 */
QRadioButton::indicator {
    width: 15px; height: 15px;
    border: 2px solid #5f6873;
    border-radius: 8px;
    background-color: #ffffff;
}
QRadioButton::indicator:hover {
    border-color: #5b6570;
}
QRadioButton::indicator:checked {
    border: 2px solid #2c6bed;
    background: qradialgradient(cx:0.5, cy:0.5, radius:0.5,
                fx:0.5, fy:0.5,
                stop:0.0 #ffffff, stop:0.34 #ffffff,
                stop:0.35 #2c6bed, stop:1.0 #2c6bed);
}
QRadioButton::indicator:disabled {
    border-color: #c3c9d0;
    background-color: #f2f4f6;
}
QProgressBar {
    border: 1px solid #dfe3e8;
    border-radius: 6px;
    background-color: #ffffff;
    text-align: center;
    height: 18px;
}
QProgressBar::chunk {
    background-color: #2c6bed;
    border-radius: 5px;
}
QSplitter::handle { background-color: #e2e5ea; }
QMenuBar { background-color: #f7f8fa; border-bottom: 1px solid #e2e5ea; }
QMenuBar::item:selected { background-color: #eef4ff; border-radius: 4px; }
QMenu {
    background-color: #ffffff;
    border: 1px solid #d8dce3;
    border-radius: 6px;
    padding: 4px;
}
QMenu::item {
    padding: 6px 24px;
    border-radius: 4px;
}
QMenu::item:selected { background-color: #eef4ff; color: #1f2328; }
QStatusBar { background-color: #eff1f4; color: #4a5158; }
QToolTip {
    background-color: #ffffff;
    color: #1f2328;
    border: 1px solid #ccd2da;
    padding: 4px 8px;
}

/* ---------- 下拉箭头 / 微调箭头（显式绘制，避免主题下看不见） ---------- */
QComboBox::drop-down {
    subcontrol-origin: padding;
    subcontrol-position: center right;
    width: 26px;
    border: none;
    border-left: 1px solid #ccd2da;
}
QComboBox::down-arrow {
    width: 14px;
    height: 14px;
    image: url(data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAABAAAAAQCAYAAAAf8/9hAAAAY0lEQVR4nGNgGGjACGP8////P0kaGRkZUQyAGSKrpEFQ8+N7N+AGMJFiK7pmDAMYGRkZH9+7QZKBJLkA3XasBpDqCqJdgM12nAaQ4gqiXIDLdrwGkBMjGOA/FFBsCGXOoDUAAHk1K++Jwy7XAAAAAElFTkSuQmCC);
}
QComboBox::down-arrow:disabled {
    image: none;
}
QSpinBox::up-button, QSpinBox::down-button,
QDoubleSpinBox::up-button, QDoubleSpinBox::down-button {
    subcontrol-origin: border;
    width: 22px;
    background-color: #f2f3f5;
    border-left: 1px solid #ccd2da;
}
QSpinBox::up-button, QDoubleSpinBox::up-button {
    subcontrol-position: top right;
    border-bottom: 1px solid #ccd2da;
}
QSpinBox::down-button, QDoubleSpinBox::down-button {
    subcontrol-position: bottom right;
}
QSpinBox::up-arrow, QDoubleSpinBox::up-arrow {
    width: 13px;
    height: 13px;
    image: url(data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAABAAAAAQCAYAAAAf8/9hAAAAWUlEQVR4nM2OMQ6AMAwDbf7RpVP//0GzgmjjWkiIrEnuDvj9SFK1P948WwAAtD5K0BIgSa0Px/cFrmIK2LVvF1QVD0BijwpWFTdAao8LZhW82hMQSfqrL+YENi4zifbdIOAAAAAASUVORK5CYII=);
}
QSpinBox::down-arrow, QDoubleSpinBox::down-arrow {
    width: 13px;
    height: 13px;
    image: url(data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAABAAAAAQCAYAAAAf8/9hAAAAY0lEQVR4nGNgGGjACGP8////P0kaGRkZUQyAGSKrpEFQ8+N7N+AGMJFiK7pmDAMYGRkZH9+7QZKBJLkA3XasBpDqCqJdgM12nAaQ4gqiXIDLdrwGkBMjGOA/FFBsCGXOoDUAAHk1K++Jwy7XAAAAAElFTkSuQmCC);
}
QSpinBox::up-button:hover, QSpinBox::down-button:hover,
QDoubleSpinBox::up-button:hover, QDoubleSpinBox::down-button:hover {
    background-color: #e8ecf1;
}

"""

# ==================================================================
# 深色
# ==================================================================
_DARK = """
/* ---------- 全局 ---------- */
QWidget {
    background-color: #1b1e23;
    color: #dfe3e8;
    font-size: 13px;
}
QMainWindow, QDialog {
    background-color: #1b1e23;
}
QLabel { background-color: transparent; }

/* ---------- 分组框 ---------- */
QGroupBox {
    border: 1px solid #333941;
    border-radius: 8px;
    margin-top: 14px;
    padding-top: 8px;
    background-color: #22262c;
    font-weight: 600;
    color: #e6e9ed;
}
QGroupBox::title {
    subcontrol-origin: margin;
    subcontrol-position: top left;
    left: 12px;
    padding: 0 6px;
    color: #6ea8fe;
}

/* ---------- 按钮 ---------- */
QPushButton {
    background-color: #2b3138;
    border: 1px solid #3c434c;
    border-radius: 6px;
    padding: 6px 14px;
    min-height: 20px;
    color: #dfe3e8;
}
QPushButton:hover {
    background-color: #343b44;
    border-color: #6ea8fe;
}
QPushButton:pressed {
    background-color: #3d4550;
}
QPushButton:disabled {
    color: #6b737d;
    background-color: #252a30;
    border-color: #333941;
}

/* ---------- 输入控件 ---------- */
QLineEdit, QSpinBox, QDoubleSpinBox, QComboBox, QTextEdit, QPlainTextEdit {
    background-color: #262b31;
    border: 1px solid #3c434c;
    border-radius: 6px;
    padding: 4px 8px;
    min-height: 20px;
    color: #dfe3e8;
    selection-background-color: #2c6bed;
}
QLineEdit:focus, QSpinBox:focus, QDoubleSpinBox:focus, QComboBox:focus {
    border-color: #6ea8fe;
}
QComboBox::drop-down {
    border: none;
    width: 18px;
}
QComboBox QAbstractItemView {
    background-color: #262b31;
    border: 1px solid #3c434c;
    selection-background-color: #2c6bed;
    selection-color: #ffffff;
    outline: 0px;
}

/* ---------- 表格 ---------- */
QTableWidget, QTableView {
    background-color: #22262c;
    alternate-background-color: #262b31;
    gridline-color: #333941;
    border: 1px solid #333941;
    border-radius: 6px;
    selection-background-color: #2c6bed;
    selection-color: #ffffff;
    outline: 0px;
    color: #dfe3e8;
}
QHeaderView::section {
    background-color: #2b3138;
    color: #b6bdc6;
    border: none;
    border-right: 1px solid #333941;
    border-bottom: 1px solid #333941;
    padding: 5px 8px;
    font-weight: 600;
}

/* ---------- 页签 ---------- */
QTabWidget::pane {
    border: 1px solid #333941;
    border-radius: 6px;
    background-color: #22262c;
    top: -1px;
}
QTabBar::tab {
    background-color: #2b3138;
    color: #b6bdc6;
    border: 1px solid #333941;
    padding: 6px 16px;
    margin-right: 2px;
    border-top-left-radius: 6px;
    border-top-right-radius: 6px;
}
QTabBar::tab:selected {
    background-color: #22262c;
    color: #6ea8fe;
    font-weight: 600;
    border-bottom-color: #22262c;
}

/* ---------- 滚动条 ---------- */
QScrollBar:vertical {
    background: #22262c;
    width: 10px;
    margin: 0;
    border: none;
}
QScrollBar::handle:vertical {
    background: #454d57;
    border-radius: 5px;
    min-height: 24px;
}
QScrollBar::handle:vertical:hover { background: #565f6b; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
QScrollBar:horizontal {
    background: #22262c;
    height: 10px;
    border: none;
}
QScrollBar::handle:horizontal {
    background: #454d57;
    border-radius: 5px;
    min-width: 24px;
}
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal { width: 0; }

/* ---------- 其它 ---------- */
QCheckBox, QRadioButton { spacing: 6px; background-color: transparent; }
QCheckBox::indicator {
    width: 15px; height: 15px;
    border: 1px solid #4d555f;
    border-radius: 3px;
    background-color: #262b31;
}
QCheckBox::indicator:checked {
    background-color: #2c6bed;
    border-color: #2c6bed;
}
/* 单选框指示器：之前两套主题都只写了 QCheckBox::indicator，
   漏了 QRadioButton::indicator —— 于是「CRF 恒定质量 / 固定码率」前面的
   圆圈走 Qt 默认绘制，深色底上几乎看不见。
   这里用径向渐变画出「蓝底 + 白色实心内点」，未选中时给亮边框保证可见。 */
QRadioButton::indicator {
    width: 15px; height: 15px;
    border: 2px solid #a2acba;
    border-radius: 8px;
    background-color: #262b31;
}
QRadioButton::indicator:hover {
    border-color: #a8b2bf;
}
QRadioButton::indicator:checked {
    border: 2px solid #2c6bed;
    background: qradialgradient(cx:0.5, cy:0.5, radius:0.5,
                fx:0.5, fy:0.5,
                stop:0.0 #ffffff, stop:0.34 #ffffff,
                stop:0.35 #2c6bed, stop:1.0 #2c6bed);
}
QRadioButton::indicator:disabled {
    border-color: #4a515a;
    background-color: #22262b;
}
QProgressBar {
    border: 1px solid #333941;
    border-radius: 6px;
    background-color: #262b31;
    text-align: center;
    height: 18px;
    color: #dfe3e8;
}
QProgressBar::chunk {
    background-color: #2c6bed;
    border-radius: 5px;
}
QSplitter::handle { background-color: #333941; }
QMenuBar {
    background-color: #1b1e23;
    border-bottom: 1px solid #333941;
    color: #dfe3e8;
}
QMenuBar::item:selected { background-color: #2b3138; border-radius: 4px; }
QMenu {
    background-color: #262b31;
    border: 1px solid #3c434c;
    border-radius: 6px;
    padding: 4px;
    color: #dfe3e8;
}
QMenu::item {
    padding: 6px 24px;
    border-radius: 4px;
}
QMenu::item:selected { background-color: #2c6bed; color: #ffffff; }
QStatusBar { background-color: #22262c; color: #b6bdc6; }
QToolTip {
    background-color: #2b3138;
    color: #dfe3e8;
    border: 1px solid #3c434c;
    padding: 4px 8px;
}

/* ---------- 下拉箭头 / 微调箭头（显式绘制，避免主题下看不见） ---------- */
QComboBox::drop-down {
    subcontrol-origin: padding;
    subcontrol-position: center right;
    width: 26px;
    border: none;
    border-left: 1px solid #3c434c;
}
QComboBox::down-arrow {
    width: 14px;
    height: 14px;
    image: url(data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAABAAAAAQCAYAAAAf8/9hAAAAaElEQVR4nGNgGGjACGMIiEj8J0XjhzcvGFEMgBny4MEDgpoVFBTgBjCRYiu6ZgwDPrx5waigoECSgSS5AN12rAaQ6gqiXYDNdpwGkOIKolyAy3a8BpATIxhAQETiP6kpFKshlDmD1gAAxDMn7EYdKE8AAAAASUVORK5CYII=);
}
QComboBox::down-arrow:disabled {
    image: none;
}
QSpinBox::up-button, QSpinBox::down-button,
QDoubleSpinBox::up-button, QDoubleSpinBox::down-button {
    subcontrol-origin: border;
    width: 18px;
    background-color: #2f353d;
    border-left: 1px solid #3c434c;
}
QSpinBox::up-button, QDoubleSpinBox::up-button {
    subcontrol-position: top right;
    border-bottom: 1px solid #3c434c;
}
QSpinBox::down-button, QDoubleSpinBox::down-button {
    subcontrol-position: bottom right;
}
QSpinBox::up-arrow, QDoubleSpinBox::up-arrow {
    width: 13px;
    height: 13px;
    image: url(data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAABAAAAAQCAYAAAAf8/9hAAAAX0lEQVR4nGNgGPRAQETiPz55Jko0EzSAgYGB4cGDB3gNwmmAgIjE/wcPHhAyn7ALCLkCqwHE2k60C/C5AsMAUmwnyQW4XIFiAKm2k+wCbK5gRLadFIM+vHnBSFgVPQAAdt4pP+fkWxEAAAAASUVORK5CYII=);
}
QSpinBox::down-arrow, QDoubleSpinBox::down-arrow {
    width: 13px;
    height: 13px;
    image: url(data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAABAAAAAQCAYAAAAf8/9hAAAAaElEQVR4nGNgGGjACGMIiEj8J0XjhzcvGFEMgBny4MEDgpoVFBTgBjCRYiu6ZgwDPrx5waigoECSgSS5AN12rAaQ6gqiXYDNdpwGkOIKolyAy3a8BpATIxhAQETiP6kpFKshlDmD1gAAxDMn7EYdKE8AAAAASUVORK5CYII=);
}
QSpinBox::up-button:hover, QSpinBox::down-button:hover,
QDoubleSpinBox::up-button:hover, QDoubleSpinBox::down-button:hover {
    background-color: #3a424b;
}

"""

_QSS = {"light": _LIGHT, "dark": _DARK}


def qss_for(theme: str) -> str:
    return _QSS.get(theme, _LIGHT)


def current_theme() -> str:
    return _theme


def apply_theme(app, theme: str = "dark") -> str:
    """给整个应用套主题。QSS 必须设在 QApplication 上才会全局生效。"""
    global _theme
    theme = theme if theme in _QSS else "dark"
    _theme = theme
    try:
        app.setStyle("Fusion")
        app.setStyleSheet(qss_for(theme))
    except Exception:  # noqa: BLE001
        pass
    # 关键：setStyle("Fusion") 会把已装的箭头 ProxyStyle 顶掉，
    # 所以每次换主题都要重新装一遍，否则下拉 / 微调箭头又消失了。
    try:
        from ui.arrow_style import install_arrow_style
        install_arrow_style(app)
    except Exception:  # noqa: BLE001
        pass
    return theme


# ==================================================================
# 持久化
# ==================================================================
def _config_path() -> str:
    import os
    return os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "config.json")


def load_theme_from_config() -> str:
    import json
    import os
    p = _config_path()
    if not os.path.exists(p):
        return "dark"
    try:
        with open(p, "r", encoding="utf-8") as fh:
            v = json.load(fh).get("ui_theme", "dark")
        return v if v in _QSS else "dark"
    except Exception:  # noqa: BLE001
        return "dark"


def save_theme_to_config(theme: str) -> None:
    import json
    import os
    p = _config_path()
    data = {}
    if os.path.exists(p):
        try:
            with open(p, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except Exception:  # noqa: BLE001
            data = {}
    data["ui_theme"] = theme
    try:
        with open(p, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)
    except Exception:  # noqa: BLE001
        pass


# ---------------------------------------------------------------------------
# 语义色：深浅两套
#
# 之前多处在界面代码里**硬编码**文字颜色（#b06000 棕、#c00000 暗红、
# #1e7a32 深绿……）。这些颜色在浅色主题下没问题，切到深色主题后
# 就成了"暗色字压深底" —— 用户反馈的"标记看不清"就是这么来的。
#
# 统一从这里取色，深浅各一套，保证对比度。
# ---------------------------------------------------------------------------
_SEMANTIC = {
    #           浅色（暗字压浅底）   深色（亮字压深底）
    "warn":     ("#8a5300",          "#ffcc66"),
    "error":    ("#b3261e",          "#ff8a80"),
    "ok":       ("#1e7a32",          "#5ddb8a"),
    "info":     ("#1a5fb4",          "#7fb3ff"),
    "diff_bg":  ("#fff4c2",          "#5c4a00"),
    "diff_fg":  ("#1a1a1a",          "#ffe9a8"),
    "safe_bg":  ("#e8f0fe",          "#1e3a5f"),
    "safe_fg":  ("#1a1a1a",          "#a8c8ff"),
    # 提示卡片（HintCard）的底色与标题色。
    # 原来这些色值硬编码在 ui/hint_card.py 里且只有浅色一套，
    # 深色主题下就成了"浅底 + 近黑字" —— 完全看不清。
    "hint_info_bg":  ("#eef4fd",     "#1b2c47"),
    "hint_ok_bg":    ("#e9f6ec",     "#16351f"),
    "hint_warn_bg":  ("#fdf6e3",     "#3a2f08"),
    "hint_error_bg": ("#fdecea",     "#411d1a"),
    "hint_title_fg": ("#202124",     "#e8eaed"),
}


def semantic_color(kind: str) -> str:
    """按当前主题返回语义色。kind 见 _SEMANTIC 的键。"""
    dark = (current_theme() == "dark")
    pair = _SEMANTIC.get(kind)
    if not pair:
        return "#1a1a1a" if not dark else "#dfe3e8"
    return pair[1] if dark else pair[0]


def diff_colors(kind: str = "diff"):
    """返回 (背景色, 前景色)。kind: 'diff' 真差异 / 'harmless' 无害差异。"""
    if kind == "harmless":
        return semantic_color("safe_bg"), semantic_color("safe_fg")
    return semantic_color("diff_bg"), semantic_color("diff_fg")


# 内联样式（写在 QLabel 的 HTML / setStyleSheet 里的那些）
# 之前界面里到处硬编码 background:#eef6ff、color:#666 这类浅色方案。
# 深色主题管不到内联样式 → 浅底配浅字，正是"提示看不清"的来源。
_BOX = {
    #        边框（浅 / 深）              底（浅 / 深）
    "info":  (("#4a90d9", "#5b9bd5"), ("#eef6ff", "#16263c")),
    "ok":    (("#4a9d5f", "#4fa863"), ("#eefaf0", "#14301f")),
    "warn":  (("#d9a400", "#d9a400"), ("#fff8e6", "#3a2f08")),
}


def box_css(kind: str = "info", padding: str = "6px") -> str:
    """返回一段可直接塞进 style='' 的 CSS：底色+文字色+左边框，深浅两套。"""
    dark = (current_theme() == "dark")
    i = 1 if dark else 0
    bd, bg = _BOX.get(kind, _BOX["info"])
    fg = semantic_color("warn" if kind == "warn" else
                        ("ok" if kind == "ok" else "info"))
    return (f"background:{bg[i]}; color:{fg}; "
            f"border-left:4px solid {bd[i]}; padding:{padding};")


def inline_swatch_css(kind: str = "diff") -> str:
    """图例里那种「标黄 / 标蓝」小色块的 CSS（背景+文字色，深浅两套）。"""
    bg, fg = diff_colors(kind)
    return f"background:{bg}; color:{fg};"


def muted_fg() -> str:
    """辅助说明文字的颜色。原来是写死的 #666，深色下几乎看不见。"""
    return "#9aa0a6" if current_theme() == "dark" else "#666666"
