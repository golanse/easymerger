# -*- coding: utf-8 -*-
"""下拉 / 微调箭头：用 QProxyStyle 直接画，不依赖 QSS 的 image。

为什么不用样式表
----------------
原先在 QSS 里写

    QComboBox::down-arrow { image: url(data:image/png;base64,...) }
    QSpinBox::up-arrow    { image: url(data:image/png;base64,...) }

语法没错，但**图片是否真的画出来取决于平台**：离屏渲染下验证过，
三种写法（::down-arrow / ::drop-down / ::up-button）一个像素都没画出来。
用户反馈"箭头基本不能肉眼识别"，正是这个不靠谱的路径导致的。

这里改成用代码直接画三角形：不加载图片、不看平台脸色，
而且能精确控制尺寸（14px）和颜色（跟随主题、高对比）。
"""

from __future__ import annotations

"""下拉 / 微调箭头：直接往控件上叠加绘制，绕开 Qt 样式系统。

为什么不用 QSS image
--------------------
    QComboBox::down-arrow { image: url(data:image/png;base64,...) }
语法没错，但实测（深色 QSS 渲染 4387 像素、箭头图 0 像素）证明：
**图片一个像素都没画出来**。用户反馈"箭头基本不能肉眼识别"就是它。

为什么不用 QProxyStyle
----------------------
QProxyStyle 在不挂样式表时有效（落笔 1/2 次），但一旦
`QApplication.setStyleSheet()` 生效（本程序换主题必走这一步），
Qt 会用内部的样式表样式接管，`app.setStyle(proxy)` 形同虚设——
`app.style()` 仍返回 QCommonStyle，我们的 drawComplexControl 一次都不进。
实测落笔次数 0。

所以这里改走**应用级事件过滤器**：控件自己画完之后，我们再在它上面
补一个三角形。这条路不经过 QStyle、不看 QSS 脸色，也不管平台差异。
"""

from PySide6.QtCore import (QEvent, QObject, QPoint, QRect, Qt)
from PySide6.QtGui import (QBrush, QColor, QPainter, QPolygon, QPen)
from PySide6.QtWidgets import (QApplication, QComboBox, QDoubleSpinBox,
                               QSpinBox, QStyle, QStyleOptionComboBox,
                               QStyleOptionSpinBox)

__all__ = ["ArrowOverlay", "install_arrow_style", "arrow_paint_count",
           "reset_arrow_paint_count"]

# 跟随主题的两套箭头颜色（深底用近白，浅底用近黑）
_ARROW_DARK = QColor(238, 242, 247)
_ARROW_LIGHT = QColor(30, 35, 41)

_ARROW_SIZE = 14      # 三角形边长（像素）—— 比原来的 10~12 更大更醒目
_ARROW_MIN = 9        # 空间不足时的下限

# 实际落笔次数。验证脚本靠它判断"箭头到底画没画"——
# 数像素的办法不可靠（背景色常常和箭头色接近，数出来几千都算不了数），
# 直接数落笔次数才是硬证据。
_PAINT_COUNT = 0


def reset_arrow_paint_count() -> None:
    global _PAINT_COUNT
    _PAINT_COUNT = 0


def arrow_paint_count() -> int:
    return _PAINT_COUNT


def _is_dark() -> bool:
    """按当前主题取箭头颜色。取不到就按背景亮度判断。

    优先用**运行时当前主题**而不是配置文件：apply_theme() 之后配置
    可能还没落盘（或者用户只是预览没保存），读配置会拿到上一套主题，
    于是深色界面画出深色箭头 —— 正好是"看不清"的另一种成因。
    """
    try:
        from core import theme
        cur = theme.current_theme()
        if cur in ("dark", "light"):
            return cur == "dark"
        return theme.load_theme_from_config() == "dark"
    except Exception:  # noqa: BLE001
        pass
    try:
        app = QApplication.instance()
        if app is not None:
            bg = app.palette().color(app.palette().ColorRole.Window)
            return bg.lightness() < 128
    except Exception:  # noqa: BLE001
        pass
    return True


def _tri(rect: QRect, up: bool) -> QPolygon:
    """在 rect 中心生成一个等腰三角形（顶点朝上或朝下）。"""
    side = min(_ARROW_SIZE, rect.width() - 4, rect.height() - 4)
    side = max(side, _ARROW_MIN)
    cx = rect.center().x()
    cy = rect.center().y()
    h = int(side * 0.62)          # 高度略小于边长，视觉上更稳
    if up:
        return QPolygon([QPoint(cx - side // 2, cy + h // 2),
                         QPoint(cx + side // 2, cy + h // 2),
                         QPoint(cx, cy - h // 2)])
    return QPolygon([QPoint(cx - side // 2, cy - h // 2),
                     QPoint(cx + side // 2, cy - h // 2),
                     QPoint(cx, cy + h // 2)])


class ArrowOverlay(QObject):
    """应用级事件过滤器：控件画完自己之后，补画高对比三角箭头。

    装在 QApplication 上，因此**后建的控件自动生效**——切换语言 /
    重建面板时新生成的下拉框、微调框不用再单独挂一遍。
    """

    def __init__(self, app: QApplication):
        super().__init__(app)
        self._busy: set[int] = set()

    # ---------------- 事件过滤 ----------------
    def eventFilter(self, obj, ev):  # noqa: N802
        try:
            if (ev.type() == QEvent.Type.Paint
                    and isinstance(obj, (QComboBox, QSpinBox, QDoubleSpinBox))
                    and obj.isEnabled()):
                key = id(obj)
                if key in self._busy:
                    return False          # 我们自己派发的那一次，放行
                self._busy.add(key)
                try:
                    # 先让控件按自己的方式画完（含 QSS 的背景、边框）
                    QApplication.sendEvent(obj, ev)
                finally:
                    self._busy.discard(key)
                self._draw(obj)
                return True               # 已经画过了，别再派发一次
        except Exception:  # noqa: BLE001
            return False
        return False

    # ---------------- 取箭头区域 ----------------
    @staticmethod
    def _rects(obj):
        """用当前 style 求子控件区域（QSS 下依然准确）。"""
        st = obj.style()
        out = []
        try:
            if isinstance(obj, QComboBox):
                opt = QStyleOptionComboBox()
                opt.initFrom(obj)
                r = st.subControlRect(QStyle.ComplexControl.CC_ComboBox, opt,
                                      QStyle.SubControl.SC_ComboBoxArrow, obj)
                if r.isValid() and r.width() >= 6 and r.height() >= 6:
                    out.append((r, False))
            else:
                opt = QStyleOptionSpinBox()
                opt.initFrom(obj)
                cc = QStyle.ComplexControl.CC_SpinBox
                ru = st.subControlRect(cc, opt, QStyle.SubControl.SC_SpinBoxUp, obj)
                if ru.isValid() and ru.height() >= 6:
                    out.append((ru, True))
                rd = st.subControlRect(cc, opt, QStyle.SubControl.SC_SpinBoxDown, obj)
                if rd.isValid() and rd.height() >= 6:
                    out.append((rd, False))
        except Exception:  # noqa: BLE001
            pass
        return out

    # ---------------- 实际落笔 ----------------
    def _draw(self, obj) -> None:
        global _PAINT_COUNT
        rects = self._rects(obj)
        if not rects:
            return
        color = _ARROW_DARK if _is_dark() else _ARROW_LIGHT
        painter = QPainter(obj)
        try:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QBrush(color))
            for r, up in rects:
                painter.drawPolygon(_tri(r, up))
                _PAINT_COUNT += 1
        finally:
            painter.end()


_OVERLAY = None


def install_arrow_style(app: QApplication) -> bool:
    """装上箭头叠加层。幂等：重复调用不会层层叠加。"""
    global _OVERLAY
    try:
        if _OVERLAY is not None and _OVERLAY.parent() is app:
            return True
        _OVERLAY = ArrowOverlay(app)
        app.installEventFilter(_OVERLAY)
        return True
    except Exception:  # noqa: BLE001
        return False
