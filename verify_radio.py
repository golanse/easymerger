"""验证单选框 indicator 在深浅主题下是否真的画得出来（像素级）。

以前主题里只定义了 QCheckBox::indicator，QRadioButton::indicator 缺失，
单选框走 Qt 默认绘制 → 深色下几乎看不见。
这里把单选框渲染成图，统计 indicator 区域的像素对比度。
"""
import sys

sys.path.insert(0, "/data/workspace/pkg33")

from PySide6.QtCore import Qt, QRect
from PySide6.QtGui import QColor
from PySide6.QtWidgets import QApplication, QWidget, QVBoxLayout, QRadioButton


def lum(c):
    def f(v):
        v = v / 255.0
        return v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4
    return 0.2126 * f(c.red()) + 0.7152 * f(c.green()) + 0.0722 * f(c.blue())


def contrast(a, b):
    la, lb = lum(a), lum(b)
    hi, lo = max(la, lb), min(la, lb)
    return (hi + 0.05) / (lo + 0.05)


def measure(theme_key):
    app = QApplication.instance() or QApplication(sys.argv)
    from core import theme
    theme.apply_theme(app, theme_key)

    host = QWidget()
    lay = QVBoxLayout(host)
    rb_on = QRadioButton("CRF")
    rb_off = QRadioButton("Bitrate")
    rb_on.setChecked(True)
    rb_off.setChecked(False)
    lay.addWidget(rb_on)
    lay.addWidget(rb_off)
    host.resize(200, 80)
    host.show()
    app.processEvents()

    # 抓一块空白区域当作页面背景色
    page_img = host.grab().toImage()
    page_bg = QColor(page_img.pixel(page_img.width() - 3,
                                    page_img.height() - 3))

    out = {}
    for name, rb in (("checked", rb_on), ("unchecked", rb_off)):
        img = rb.grab().toImage()
        # indicator 在控件左侧区域
        region = QRect(0, (img.height() - 16) // 2, 16, 16)
        region = region.intersected(QRect(0, 0, img.width(), img.height()))
        colors = {}
        for y in range(region.top(), region.bottom()):
            for x in range(region.left(), region.right()):
                c = QColor(img.pixel(x, y))
                colors[c.rgb()] = colors.get(c.rgb(), 0) + 1
        # 参照系必须是**页面背景**，不能取 indicator 区域内最多的颜色 ——
        # 选中时圆心（亮色）面积最大，会被误当成背景，
        # 导致「亮环 vs 白心」被当成对比度，算出偏低的 2.47。
        bg = page_bg
        # indicator 与背景的最大对比度
        best = 0.0
        best_c = None
        for rgb, n in colors.items():
            if n < 3:          # 忽略抗锯齿杂点
                continue
            c = QColor(rgb)
            cr = contrast(c, bg)
            if cr > best:
                best, best_c = cr, c
        out[name] = (best, bg.name(), best_c.name() if best_c else "-",
                     len(colors))
    return out


def main():
    app = QApplication.instance() or QApplication(sys.argv)
    ok = True
    for key, label in (("dark", "深色"), ("light", "浅色")):
        res = measure(key)
        print(f"\n===== {label} 主题 =====")
        for state, (cr, bg, fg, ncol) in res.items():
            flag = "OK " if cr >= 3.0 else "!! "
            print(f"  {flag}{state:9s} 对比度={cr:5.2f}  底色={bg}  "
                  f"最亮={fg}  颜色数={ncol}")
            if cr < 3.0:
                ok = False
    print("\n结论:", "单选框在两种主题下均清晰可见" if ok else "仍不可见")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
