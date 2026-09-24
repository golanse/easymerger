"""结构化提示卡片。

为什么需要这个组件
-------------------
之前的提示是往 QLabel 里塞一大段纯文本，靠手敲的
「—— 标题 ——」和「  · 项目」来模拟结构。问题是：

  · 没有视觉层次 —— 一大片灰字挤在一起，扫一眼抓不到重点
  · 伪列表靠空格缩进，一旦换行就不对齐了
  · 长篇说明全铺开，把参数表单撑得很长，要滚很久才看到下面的参数

现在改成卡片：

    结论（加粗、带状态图标）
    · 要点一
    · 要点二
    ▸ 为什么风扇不狂转？      ← 点开才是长篇说明

状态色条一眼区分「能用 / 有问题 / 仅供参考」。
"""

from __future__ import annotations

from typing import List, Optional, Sequence

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QFrame, QHBoxLayout, QLabel, QSizePolicy, QToolButton, QVBoxLayout, QWidget,
)

__all__ = ["HintCard"]


# level → 图标（配色不再写死在这里）
#
# 以前这张表连背景色和主色一起写死，而且只有浅色一套。深色主题下
# 卡片仍是浅蓝底 + 近黑标题字 —— 用户反馈的"提示标签看不清"正是这里。
# 现在颜色统一从 core.theme 按当前主题取，深浅各一套。
_ICONS = {
    "info":  "ℹ",
    "ok":    "✔",
    "warn":  "⚠",
    "error": "✘",
}
_LEVELS = tuple(_ICONS)


# 固定浅色配色。
# 曾经改过"跟随深浅主题取色"，但用户并未提出该诉求，且反馈颜色
# 被改得看不清 —— 这里按用户要求回退为统一的固定配色。
_FIXED = {
    "info":  ("#eef4fd", "#1a66d8", "ℹ"),
    "ok":    ("#e9f6ec", "#1a7f37", "✔"),
    "warn":  ("#fdf6e3", "#9a6700", "⚠"),
    "error": ("#fdecea", "#c5221f", "✘"),
}
_TITLE_FG = "#202124"
# 底色是**固定浅色**，所以文字也必须**固定深色**，
# 绝不能跟随主题 —— 否则深色主题下会变成浅字压浅底，完全看不见。
_TEXT_FG = "#202124"


def _style(level: str):
    """返回 (底色, 主色, 图标)，固定不随主题变化。"""
    return _FIXED.get(level, _FIXED["info"])

# 富文本里反复用到的段落样式。
# 用 text-indent 负值做悬挂缩进 —— 列表符号凸出来，文字左边界对齐，
# 这样换行后依然整齐（纯文本靠敲空格是做不到的）。
_BULLET = ("<p style='margin:0 0 2px 0; padding-left:15px; text-indent:-15px; color:{fg};'>"
           "<span style='color:{color};'>•</span>&nbsp; {text}</p>")
_PLAIN = "<p style='margin:0 0 3px 0; color:{fg};'>{text}</p>"


def _esc(text: str) -> str:
    """转义 HTML 特殊字符，避免内容里的 < > & 破坏排版。"""
    return (text.replace("&", "&amp;")
                .replace("<", "&lt;")
                .replace(">", "&gt;"))


class HintCard(QFrame):
    """一张提示卡片。

    用法：
        card.set_hint(
            level="ok",
            title="hevc_amf 可用（已实测编码成功）",
            points=["速度快、CPU 占用低", "同码率下画质略逊于 libx264"],
            details=["长篇说明第一段", "第二段"],
            details_label="为什么风扇不狂转？",
        )
        card.clear()      # 隐藏
    """

    # 详情展开/收起时会发出，方便调用方在需要时重新布局
    toggled = Signal()
    # 用户点了右上角 ✕ 关闭卡片（调用方可据此记录"不再提示"）
    closed = Signal()

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setObjectName("HintCard")
        self.setFrameShape(QFrame.NoFrame)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Maximum)

        self._level = "info"
        self._expanded = False
        self._points: List[str] = []
        self._details: List[str] = []
        self._details_label = "详细说明"
        # 原始（未翻译）内容，供切语言/切主题时重建
        self._raw: Optional[dict] = None
        # 用户是否手动关掉了这张卡片。
        # 切语言/切主题会重建卡片，若不记住这个状态，
        # 关掉的提示会自己又冒出来 —— 等于关闭按钮白点。
        self._dismissed = False

        root = QHBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # 左侧色条：状态一眼可见
        self._bar = QFrame(self)
        self._bar.setFixedWidth(3)
        self._bar.setFrameShape(QFrame.NoFrame)
        root.addWidget(self._bar)

        # 内容区
        body = QWidget(self)
        body.setAutoFillBackground(True)
        self._body = body
        bl = QVBoxLayout(body)
        bl.setContentsMargins(9, 7, 9, 7)
        bl.setSpacing(4)

        # 标题行（右侧带关闭按钮）
        # 提示卡片是"看完就不需要了"的信息，之前只能一直占着地方。
        # 用户要求能手动关掉 —— 关掉后本次会话内不再自动弹出，
        # 除非内容真的变了（set_hint 会重置）。
        _trow = QHBoxLayout()
        _trow.setContentsMargins(0, 0, 0, 0)
        _trow.setSpacing(4)
        self._title = QLabel()
        self._title.setWordWrap(True)
        self._title.setTextFormat(Qt.RichText)
        self._title.setTextInteractionFlags(Qt.TextSelectableByMouse)
        _trow.addWidget(self._title, 1)
        self._close = QToolButton()
        self._close.setObjectName("HintClose")
        self._close.setText("✕")
        self._close.setAutoRaise(True)
        self._close.setCursor(Qt.PointingHandCursor)
        self._close.setToolTip("关闭此提示")
        self._close.setFixedSize(18, 18)
        self._close.clicked.connect(self._on_close)
        _trow.addWidget(self._close, 0, Qt.AlignmentFlag.AlignTop)
        bl.addLayout(_trow)

        # 要点/正文
        self._text = QLabel()
        self._text.setWordWrap(True)
        # 底色固定浅色 → 文字固定深色，不跟随主题
        self._text.setStyleSheet(f"color:{_TEXT_FG};")
        self._text.setTextFormat(Qt.RichText)
        self._text.setTextInteractionFlags(Qt.TextSelectableByMouse)
        bl.addWidget(self._text)

        # 详情折叠按钮
        self._toggle = QToolButton()
        self._toggle.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        self._toggle.setArrowType(Qt.RightArrow)
        self._toggle.setAutoRaise(True)
        # 折叠按钮的蓝色以前也是写死的，深色下同样看不清
        self._toggle.setObjectName("HintToggle")
        self._toggle.clicked.connect(self._on_toggle)
        self._toggle.hide()
        bl.addWidget(self._toggle)

        root.addWidget(body, 1)
        self._apply_style()
        self.hide()

    # ------------------------------------------------------------------
    def _apply_style(self) -> None:
        bg, fg, _icon = _style(self._level)
        self._bar.setStyleSheet(f"background:{fg};")
        self._body.setStyleSheet(
            f"background:{bg};"
            f"border-top-right-radius:4px;border-bottom-right-radius:4px;")
        try:
            self._toggle.setStyleSheet(
                f"QToolButton{{border:none;padding:0 2px;color:{fg};}}"
                f"QToolButton:hover{{text-decoration:underline;}}")
            # 关闭按钮同样用主色，保证在浅底卡片上清晰可见；
            # hover 时加浅灰底，让用户知道这里可以点。
            self._close.setStyleSheet(
                f"QToolButton{{border:none;color:{fg};font-weight:700;}}"
                f"QToolButton:hover{{background:rgba(0,0,0,0.10);"
                f"border-radius:2px;}}")
        except Exception:
            pass

    def _on_close(self) -> None:
        """关闭（隐藏）这张提示卡片。"""
        self._dismissed = True
        self.hide()
        try:
            self.closed.emit()
        except Exception:
            pass

    def _on_toggle(self) -> None:
        """展开/收起详情。

        为什么用"重新 setText"而不是"隐藏一个 QLabel"：
        Qt 布局在子控件隐藏后**不会收缩**（实测 121 → 197 → 197，
        收不回去，折叠了却留一大块空白）。隐藏的那个 QLabel 虽然
        isVisible() 已经是 False，sizeHint 却仍返回旧值。

        改成把详情合并进同一个 QLabel 的富文本里再 setText，
        内容变了 sizeHint 必然跟着变，折叠才真正收得回去。
        """
        self._expanded = not self._expanded
        self._toggle.setArrowType(
            Qt.DownArrow if self._expanded else Qt.RightArrow)
        self._render()
        self.toggled.emit()

    # ------------------------------------------------------------------
    def set_hint(self,
                 level: str = "info",
                 title: str = "",
                 points: Sequence[str] = (),
                 details: Sequence[str] = (),
                 details_label: str = "详细说明",
                 keep_dismissed: bool = False) -> None:
        """设置卡片内容。

        level    info / ok / warn / error —— 决定配色与图标
        title    第一行结论（加粗显示）
        points   要点列表（自动加圆点符号，悬挂缩进对齐）
        details  长篇说明，默认折叠，点按钮才展开

        keep_dismissed  True 时保留"用户已关闭"状态（切语言/切主题
                        重建卡片时用它），False 表示这是新内容、重新显示。
        """
        self._level = level if level in _ICONS else "info"
        _bg, fg, icon = _style(self._level)
        self._apply_style()

        if not keep_dismissed:
            self._dismissed = False

        # tooltip 在这里主动翻译，而不是等 apply_translation 扫描：
        # 卡片可能还没挂到父控件上（先建后插），那时扫描覆盖不到，
        # 关闭按钮的提示就会一直是中文。
        try:
            self._close.setToolTip(self._tr("关闭此提示"))
        except Exception:
            pass

        # 把**原始中文**留下来：切语言时要用它去查字典重新渲染。
        # 只记一次（first run），之后重复 set_hint 会自然覆盖。
        self._raw = {
            "title": title,
            "points": list(points),
            "details": list(details),
            "details_label": details_label,
        }

        if title:
            self._title.setText(
                f"<span style='color:{fg};font-weight:600;'>{icon}</span>&nbsp; "
                f"<b style='color:{_TITLE_FG};'>{_esc(self._tr(title))}</b>")
            self._title.show()
        else:
            self._title.hide()

        self._points = list(points)
        self._details = list(details)
        self._details_label = details_label

        # 有详情才显示折叠按钮
        if self._details:
            self._toggle.setText(f" {self._tr(details_label)}")
            self._toggle.show()
        else:
            self._toggle.hide()

        self._render()
        # 用户手动关掉的卡片不要再冒出来（切语言/切主题重建时会走到这里）
        if self._dismissed:
            self.hide()
        else:
            self.show()

    # ------------------------------------------------------------------
    def _render(self) -> None:
        """把 points（+ 展开时的 details）渲染进同一个 QLabel。"""
        _bg, fg, _icon = _style(self._level)

        html: List[str] = []
        for p in self._points:
            if p == "":            # 空串当作段落间隔
                html.append("<p style='margin:0;font-size:3px;'>&nbsp;</p>")
            else:
                html.append(_BULLET.format(color=fg, fg=_TEXT_FG, text=_esc(self._tr(p))))

        if self._details and self._expanded:
            # 详情与要点之间留一点呼吸，并给个小标题做区分
            html.append(
                f"<p style='margin:5px 0 3px 0;color:{fg};font-weight:600;'>"
                f"{_esc(self._tr(self._details_label))}</p>")
            for d in self._details:
                html.append(_PLAIN.format(fg=_TEXT_FG, text=_esc(self._tr(d))))

        if html:
            self._text.setText("".join(html))
            self._text.show()
        else:
            self._text.clear()
            self._text.hide()

    # ------------------------------------------------------------------
    @staticmethod
    def _tr(text: str) -> str:
        try:
            from core.i18n import tr
            return tr(text)
        except Exception:
            return text

    def retranslate_dynamic(self) -> None:
        """切语言 / 切主题后重建卡片内容。

        卡片的文案是运行时拼进富文本的，颜色也是运行时按主题取的；
        两者都不会随语言或主题自动更新 —— 不重建就会留下
        「中文 + 浅色配色」这种在深色下根本看不清的卡片。
        """
        raw = getattr(self, "_raw", None)
        try:
            if raw:
                self.set_hint(
                    level=self._level,
                    title=raw.get("title", ""),
                    points=raw.get("points", ()),
                    details=raw.get("details", ()),
                    details_label=raw.get("details_label", "详细说明"),
                    keep_dismissed=True,
                )
            else:
                self._apply_style()
                self._render()
        except Exception:
            pass

    def clear(self) -> None:
        """清空并隐藏。"""
        self._title.clear()
        self._text.clear()
        self._points = []
        self._details = []
        self._expanded = False
        self._raw = None
        self._dismissed = False
        self.hide()
