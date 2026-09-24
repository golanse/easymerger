"""V1.47 真实界面验证：提示卡片关闭按钮 + 对齐对话框全屏/自由缩放。

必须在**真实 Qt 界面**下跑（QT_QPA_PLATFORM=offscreen）。
任何一项不达标就 sys.exit(1)，不允许"假装通过"。
"""

from __future__ import annotations

import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

app = QApplication.instance() or QApplication(sys.argv)

from core.i18n import set_lang  # noqa: E402
from core.models import VideoInfo  # noqa: E402
from ui.hint_card import HintCard  # noqa: E402

FAILS: list[str] = []


def check(ok: bool, name: str, detail: str = "") -> bool:
    print(("  PASS  " if ok else "  FAIL  ") + name + (f"  ({detail})" if detail else ""))
    if not ok:
        FAILS.append(name)
    return ok


# ==================================================================
print("A. 提示卡片关闭按钮")
# ==================================================================
set_lang("zh")
card = HintCard()
card.set_hint(level="warn", title="测试提示",
              points=["要点一"], details=["长说明"], details_label="详细说明")
card.show()
app.processEvents()

btn = getattr(card, "_close", None)
check(btn is not None, "存在关闭按钮")
check(bool(btn.isVisible()) and not btn.isHidden(), "关闭按钮可见")

w0 = card.width()
btn.click()
app.processEvents()
check(not card.isVisibleTo(card.parent()) if card.parent() else card.isHidden(),
      "点击后卡片隐藏", f"hidden={card.isHidden()}")

# 切语言重建：必须保持关闭状态，否则关闭按钮白点
set_lang("en")
card.retranslate_dynamic()
app.processEvents()
check(card.isHidden(), "切语言重建后仍保持隐藏")

# 英文下 tooltip 要是英文
tip_en = btn.toolTip()
check("Dismiss" in tip_en or "dismiss" in tip_en, "英文下关闭按钮提示为英文", tip_en)

# 新内容（keep_dismissed=False）应重新显示
card.set_hint(level="ok", title="新的提示", points=["新要点"])
app.processEvents()
check(not card.isHidden(), "设置新内容后重新显示")
set_lang("zh")
card.retranslate_dynamic()
app.processEvents()

# 反向验证：屏蔽关闭逻辑后，点击 ✕ 不应隐藏 ——
# 证明上面"点击后隐藏"确实由 _on_close 生效，而不是断言恒真
card2 = HintCard()
card2.set_hint(level="ok", title="反向验证", points=["要点"])
card2.show()
app.processEvents()
card2._on_close = lambda: None
card2._close.click()
app.processEvents()
check(not card2.isHidden(), "反向：屏蔽关闭逻辑后点击不隐藏")

# ==================================================================
print("B. 对齐对话框：全屏 / 自由缩放")
# ==================================================================
from ui.align_dialog import AlignDialog  # noqa: E402

MAXH = Qt.WindowType.WindowMaximizeButtonHint
MINH = Qt.WindowType.WindowMinimizeButtonHint


def _mk(n: int, odd_from: int):
    """造 n 个文件，其中 odd_from 之后的 2 个参数不同。"""
    out = []
    for i in range(n):
        odd = i >= odd_from
        out.append(VideoInfo(
            path=f"/tmp/v{i:03d}.mp4", name=f"第{i:03d}集.mp4",
            duration=100.0, container="mp4",
            has_video=True, v_codec="hevc", v_profile="Main",
            v_level="4.0", v_pix_fmt="yuv420p",
            width=1884 if odd else 1920, height=1080,
            sar="1:1", dar="16:9", fps=30.0, fps_raw="30/1",
            v_bitrate=600000, rotation=0,
            has_audio=True, a_codec="aac",
            a_profile="LC" if odd else "HE-AACv2",
            a_sample_rate=44100, a_channels=2,
            a_bitrate=64000, overall_bitrate=700000))
    return out


fit = AlignDialog._fit_to_screen(980, 800)
scr = app.primaryScreen()
ag = scr.availableGeometry() if scr else None
if ag is not None:
    check(fit[0] <= int(ag.width() * 0.92) + 1 and fit[1] <= int(ag.height() * 0.92) + 1,
          "默认尺寸不超过屏幕可用区域 92%", f"fit={fit} avail={ag.width()}x{ag.height()}")
    check(fit[0] >= 640 and fit[1] >= 420, "默认尺寸不低于最小尺寸", str(fit))

dlg = AlignDialog(_mk(6, 4), ffmpeg="ffmpeg")
dlg.show()
app.processEvents()

fl = dlg.windowFlags()
check(bool(fl & MAXH), "有最大化按钮", f"flags={int(fl)}")
check(bool(fl & MINH), "有最小化按钮")
check(dlg.minimumWidth() <= 640 and dlg.minimumHeight() <= 420,
      "最小尺寸足够小，能自由缩小",
      f"{dlg.minimumWidth()}x{dlg.minimumHeight()}")

# 放大窗口 → 内容表格要跟着变宽（自由缩放时布局自适应）
tbl = getattr(dlg, "tbl", None)
w_before = tbl.viewport().width() if tbl is not None else -1
dlg.resize(dlg.width() + 300, dlg.height() + 200)
app.processEvents()
w_after = tbl.viewport().width() if tbl is not None else -1
check(tbl is not None and w_after > w_before,
      "窗口放大后清单表格跟着变宽", f"{w_before} → {w_after}")

# 全屏
dlg.showMaximized()
app.processEvents()
check(dlg.isMaximized(), "可以最大化（全屏）")
check(dlg.width() >= (ag.width() - 8) if ag else True,
      "最大化后铺满可用宽度", f"{dlg.width()} vs {ag.width() if ag else '?'}")

# 缩小
dlg.showNormal()
app.processEvents()
dlg.resize(700, 480)
app.processEvents()
check(dlg.width() == 700 and dlg.height() == 480, "能缩到 700x480",
      f"{dlg.width()}x{dlg.height()}")

# 英文下标题不能有中文（复用 V1.46 的修复，确认没被本轮改坏）
set_lang("en")
from core.i18n import apply_translation  # noqa: E402
apply_translation(dlg)
# 真实路径是 showEvent 里 apply_translation + retranslate_dynamic，
# 这里显式补上，确保拼接出来的富文本也被重建
dlg.retranslate_dynamic()
app.processEvents()
import re  # noqa: E402
bad = [w for w in dlg.findChildren(object)
       if isinstance(getattr(w, "text", None), object)]
cn = []
for w in dlg.findChildren(object):
    t = getattr(w, "text", None)
    if callable(t):
        try:
            s = t()
        except Exception:
            continue
        if isinstance(s, str) and re.search(r"[一-鿿]", s):
            cn.append(s)
check(not cn, "英文下对话框无中文残留", str(cn[:3]))
dlg.close()

print()
if FAILS:
    print(f"FAILED: {len(FAILS)} 项 -> {FAILS}")
    sys.exit(1)
print("全部通过")
sys.exit(0)
