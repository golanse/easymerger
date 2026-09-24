"""子进程输出的文本解码 —— 全项目统一入口。

为什么需要它
------------
Python 的 subprocess 在 text=True 时用 locale.getpreferredencoding()
解码子进程输出，中文 Windows 上得到的是 **cp936（GBK）**。

而 ffmpeg 官方构建（gyan.dev / BtbN / 新版原生）输出的是 **UTF-8**。
用 GBK 去解 UTF-8 的中文，结果就是日志里一片乱码 —— 任务其实
跑得好好的，只是你看不懂它说了什么。

实测这个错位只影响中文（和CJK）字符，英文数字照常显示，
所以很容易被误认为"只是个别字符问题"而忽略。

统一在这里定死 UTF-8 + errors="replace"，保证：
  · 中文正常显示（绝大多数情况）
  · 万一真遇到非 UTF-8 输出，也只是个别字符变成替代符，不会抛异常
"""

from __future__ import annotations

__all__ = ["SUBPROC_ENCODING", "decode_bytes", "safe_text"]

# ffmpeg / ffprobe 输出的编码。官方构建一律 UTF-8。
SUBPROC_ENCODING = "utf-8"


def decode_bytes(data, primary: str = SUBPROC_ENCODING) -> str:
    """解码子进程输出的字节串，优先 UTF-8，失败回退 GBK / latin-1。

    latin-1 作为最后兜底：它能解码任意字节（不会抛异常），
    虽然中文会变成乱码，但至少不会让整个探测流程崩掉。
    """
    if isinstance(data, str):
        return data
    if data is None:
        return ""
    for enc in (primary, "gbk", "cp936", "latin-1"):
        try:
            return data.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return data.decode("utf-8", errors="replace")


def safe_text(data) -> str:
    """同 decode_bytes，语义上更明确的名字。"""
    return decode_bytes(data)
