"""自然排序（Natural Sort）：文件名中的数字按数值大小比较。

例：1.mp4 < 2.mp4 < 10.mp4 < 100.mp4（而不是按字符串的 1 < 10 < 100 < 2）
"""

import re
from typing import Any, List, Sequence

__all__ = ["natural_key", "natural_sorted", "natural_key_func"]

_DIGIT_RE = re.compile(r"(\d+)")


def natural_key(text: Any) -> List[Any]:
    """把字符串拆成「文本块 / 数字块」序列，数字块转成 int 参与比较。

    同时保证不同类型（str / int）混合比较不会崩：统一用 (0, 文本) (1, 数字) 标记。
    """
    s = str(text)
    parts: List[Any] = []
    for chunk in _DIGIT_RE.split(s):
        if chunk == "":
            continue
        if chunk.isdigit():
            parts.append((1, int(chunk), ""))
        else:
            # 中文/英文混排时按本地化习惯：统一小写比较，避免大小写导致的错序
            parts.append((0, 0, chunk.lower(), chunk))
    return parts


def natural_key_func(text: Any):
    """sorted(..., key=natural_key_func) 的便捷写法。"""
    return natural_key(text)


def natural_sorted(seq: Sequence[Any], key=None) -> List[Any]:
    """按自然排序返回新列表。key 默认取元素本身（路径对象会转成字符串）。"""
    if key is None:
        return sorted(seq, key=lambda x: natural_key(x))
    return sorted(seq, key=lambda x: natural_key(key(x)))


if __name__ == "__main__":  # 小自测
    data = ["clip10.mp4", "clip2.mp4", "clip1.mp4", "Clip_1.mp4", "clip100.mp4", "第2集.mp4", "第10集.mp4"]
    print(natural_sorted(data))
