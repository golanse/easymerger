"""音频编码 Profile 的实测探测。

## 为什么需要实测

ffmpeg 原生 `aac` 编码器的 `-profile:a aac_he` / `aac_he_v2` 是**坏的**：

实测（ffmpeg 4.4 / 6.x 原生 aac 编码器）：
    · -profile:a aac_he  → 输出 AOT 21 流
    · 装 ADTS/MPEG-TS    → [adts] MPEG-4 AOT 21 is not allowed in ADTS（直接失败）
    · 装 MP4/MKV         → 能写进去，但 ffprobe 读不出任何参数，
                           解码报 Function not implemented

也就是说：单段 HE-AAC 就已经是损坏的，不经过合并也坏。
合并只是把坏码流原样拷贝，问题不在 concat。

根源是 ffmpeg 原生 aac 编码器把 HE-AAC 映射成了 AOT 21（ER-AAC-ELD 类），
既不能装 ADTS，也没有对应的解码器实现。
标准的 HE-AAC 应该是 AOT 2（AAC-LC）+ SBR 扩展，libfdk_aac 才是这个行为。

## 处理办法

不猜、不查表，直接实测：编码一小段 → 尝试完整解码 → 能解才算可用。
跟硬件编码器的探测思路一致（见 hwaccel.py）。
"""

from __future__ import annotations

import os
import subprocess

from .subproc import popen_hidden, run_hidden
import tempfile
import threading
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple, Sequence

from .ffmpeg_env import _hidden_startupinfo
from .textio import SUBPROC_ENCODING

__all__ = [
    "AAC_PROFILES",
    "ProfileInfo",
    "AudioProfileProbe",
    "normalize_aac_profile",
    "aac_profile_label",
]


# ffprobe 报上来的 profile 名 → 内部 key。
# 不同 ffmpeg 版本写法不一致（"LC" / "AAC-LC" / "AAC-LC (Low Complexity)"），
# 这里做归一化，避免把同一个 profile 误判成两个不同的。
_PROFILE_ALIASES = {
    "lc": "aac_low",
    "aac-lc": "aac_low",
    "aac_lc": "aac_low",
    # 注意：下面这行必须补。
    # normalize 会先把 "_" 统一成 "-"，所以 ffmpeg 内部写法 "aac_low"
    # 到这里已经变成 "aac-low" —— 表里若没有这个 key 就查不中，
    # 于是 "aac_low" 与 "AAC-LC" 被判成两个不同的 profile，
    # 造成"明明一样却报不一致"的误判（参数假一致的变体）。
    "aac-low": "aac_low",
    "low": "aac_low",
    "main": "aac_main",
    "ssr": "aac_ssr",
    "ltp": "aac_ltp",
    "he-aac": "aac_he",
    "he_aac": "aac_he",
    "aac-he": "aac_he",
    "aac-he": "aac_he",
    "heaac": "aac_he",
    "aac-he": "aac_he",
    "sbr": "aac_he",
    "he-aacv2": "aac_he_v2",
    "he-aac v2": "aac_he_v2",
    "he-aacv2": "aac_he_v2",
    "aac-he-v2": "aac_he_v2",
    "he-aac-v2": "aac_he_v2",
    "he_aac_v2": "aac_he_v2",
    "heaacv2": "aac_he_v2",
    "aac-he-v2": "aac_he_v2",
    "ps": "aac_he_v2",
}


#: ffmpeg 里能输出 AAC 的编码器，按"能力从强到弱"排序。
#: libfdk_aac 是唯一能输出 HE-AAC / HE-AACv2 的（原生 aac 只能 LC），
#: 所以只要它存在就应该优先用 —— 否则用户明明装了 nonfree 构建，
#: 软件却还在用原生 aac，永远输出不了 HE-AAC。
# aac_mf = Windows MediaFoundation AAC。它调用系统组件，
# 二进制里不含任何 AAC 实现，因此可以随 GPL 构建合规分发；
# 且它能输出 HE-AAC（v1），强于原生 aac（只能 LC）。
# 排在最强的 libfdk_aac / aac_at 之后、原生 aac 之前。
AAC_ENCODER_PRIORITY = ("libfdk_aac", "aac_at", "aac_mf", "aac")


def pick_aac_encoder(available: Optional[Sequence[str]] = None) -> str:
    """为本机挑一个最合适的 AAC 编码器。

    这是 V1.7 的核心 bug 所在：之前**硬编码用原生 `aac`**，
    而原生 aac **只能输出 AAC-LC**，做不了 HE-AAC / HE-AACv2。
    于是即使用户装了带 libfdk_aac 的 nonfree 构建，软件照样报
    "Profile not supported"，然后悄悄降级成 AAC-LC ——
    对齐功能因此永远达不到"参数一致"的目的。

    现在按能力排序挑选：libfdk_aac > aac_at > aac。
    """
    avail = set(available or ())
    for e in AAC_ENCODER_PRIORITY:
        if e in avail:
            return e
    # 探测不到（available 为空）时保守用原生 aac —— 它一定有
    return "aac"


def is_aac_encoder(name: str) -> bool:
    """是不是 AAC 家族编码器（用 -aac_profile 之类参数才需要它）。"""
    return (name or "").strip().lower() in (
        "aac", "libfdk_aac", "libfaac", "aac_at", "aac_mf")


def best_usable_profiles(ffmpeg: str, ffprobe: str,
                         available: Optional[Sequence[str]] = None
                         ) -> tuple:
    """返回 (最优 AAC 编码器, 该编码器能输出的 profile 列表)。

    用于对齐等"必须达成某种音频格式"的场景：先看最强的那个编码器
    能不能输出目标格式，而不是只问最弱的原生 aac。
    """
    enc = pick_aac_encoder(available)
    try:
        probe = AudioProfileProbe(ffmpeg, ffprobe)
        return enc, list(probe.usable_profiles(enc))
    except Exception:  # noqa: BLE001
        return enc, []


def normalize_aac_profile(raw: str) -> str:
    """把 ffprobe 报的 profile 名归一化为内部 key。

    返回 aac_low / aac_he / aac_he_v2 / aac_main / aac_ssr / aac_ltp，
    无法识别时原样小写返回（至少保证「一致的字符串」能判为一致）。
    """
    if not raw:
        return ""
    s = str(raw).strip().lower()
    # 去掉括号里的补充说明："AAC-LC (Low Complexity)" → "AAC-LC"
    if "(" in s:
        s = s.split("(")[0].strip()
    s = s.replace("_", "-").replace(" ", "")
    s = s.replace("aac-lc", "lc")  # 先把 aac-lc 收敛成 lc
    return _PROFILE_ALIASES.get(s, s)


#: aac_mf 的 -profile:a 认**数字**（ffmpeg 的 FF_PROFILE_AAC 枚举），
#: 不认 "aac_he_v2" 这种名字 —— 传名字会直接报
#:   Undefined constant or missing '(' in 'aac_he_v2'
#: 这是它与 libfdk_aac 最容易踩混的地方。
AAC_MF_PROFILE_NUM = {"aac_low": 1, "aac_he": 4, "aac_he_v2": 28}


def aac_profile_value(encoder: str, key: str) -> str:
    """"返回该编码器 -profile:a 应传的取值（aac_mf 用数字，其余用名字）。"""
    if (encoder or "").strip().lower() == "aac_mf":
        return str(AAC_MF_PROFILE_NUM.get((key or "").strip(), 1))
    return key


def aac_profile_args(encoder: str, key: str) -> List[str]:
    """"拼 -profile:a 参数；非 AAC 编码器返回空。"""
    if not is_aac_encoder(encoder):
        return []
    return ["-profile:a", aac_profile_value(encoder, key)]


def profile_really_matches(requested: str, actual: str) -> bool:
    """"编码产出的实际 profile 是否真的达到了要求。

    aac_mf 的坑：要 v2（profile 28）时，ffmpeg 输出行会写 "HE-AACv2"，
    但 ffprobe 读实际码流只有 "HE-AAC"（v1，缺 PS 层）。
    如果只看"编码成功 + 解码成功"，就会误判成支持 v2，
    于是软件瞄准 v2 去对齐，转完 161 个文件仍是对不齐 ——
    正是之前反复修过的老毛病。所以必须比对实际产出。
    """
    if not requested or not actual:
        return True   # 探测不到就不误杀
    return normalize_aac_profile(requested) == normalize_aac_profile(actual)


def aac_profile_label(key: str) -> str:
    """内部 key → 展示名。"""
    for k, label, _d in AAC_PROFILES:
        if k == key:
            return label
    return {"aac_main": "AAC-Main", "aac_ssr": "AAC-SSR",
            "aac_ltp": "AAC-LTP"}.get(key, key or "未知")

# profile 取值 → 展示名 + 说明
AAC_PROFILES: List[Tuple[str, str, str]] = [
    ("aac_low", "AAC-LC", "标准 AAC，兼容性最好（推荐）"),
    ("aac_he", "HE-AAC (AAC+SBR)", "低码率下音质更好，需 libfdk_aac 支持"),
    ("aac_he_v2", "HE-AAC v2 (AAC+SBR+PS)", "极低码率优化，需立体声 + libfdk_aac"),
]


@dataclass
class ProfileInfo:
    key: str = ""                   # aac_low / aac_he / aac_he_v2
    label: str = ""                 # AAC-LC ...
    desc: str = ""
    usable: bool = False
    tested: bool = False
    error: str = ""
    # 能否装进各中间容器（HE-AAC 在原生编码器下装不了 ADTS/TS）
    ts_ok: bool = True
    mkv_ok: bool = True
    mp4_ok: bool = True

    @property
    def display(self) -> str:
        if not self.tested:
            return f"{self.label}（{self.desc}）"
        mark = "✔" if self.usable else "✘"
        if self.usable:
            return f"{self.label}  ✔（{self.desc}）"
        return f"{self.label}  ✘ 不可用（{self.error[:40]}）"


class AudioProfileProbe:
    """实测各 AAC profile 在当前 ffmpeg 下能否产出可解码的流。"""

    _lock = threading.Lock()

    def __init__(self, ffmpeg: str = "", ffprobe: str = ""):
        self.ffmpeg = ffmpeg
        self.ffprobe = ffprobe
        self._cache: Dict[Tuple[str, str], ProfileInfo] = {}

    def set_ffmpeg(self, ffmpeg: str, ffprobe: str = "") -> None:
        if ffmpeg != self.ffmpeg or ffprobe != self.ffprobe:
            self.ffmpeg = ffmpeg
            self.ffprobe = ffprobe
            self._cache.clear()

    # ------------------------------------------------------------------
    @staticmethod
    def _probe_args(key: str, encoder: str = "aac") -> List[str]:
        """编码时用的 profile 参数（aac_mf 要用数字枚举）。"""
        if not key:
            return []
        return ["-profile:a", aac_profile_value(encoder, key)]

    def _encode_to(self, key: str, container: str,
                   encoder: str = "aac") -> Tuple[bool, str, Optional[str]]:
        """把一小段音频按指定 profile 编码到指定容器，返回 (成功, 报错, 文件路径)。

        encoder 必须真的用上传入的那个。
        之前这里**硬编码 -c:a aac**，导致 test_profile(key, "libfdk_aac")
        拿原生 aac 去测 —— 原生 aac 只能输出 LC，于是永远判
        "不支持 HE-AAC"，即使用户装的是带 libfdk_aac 的 nonfree 构建。
        """
        ext = {"ts": ".ts", "mkv": ".mkv", "mp4": ".mp4"}[container]
        container_args = {
            "ts": ["-f", "mpegts"],
            "mkv": ["-f", "matroska"],
            "mp4": ["-f", "mp4", "-movflags", "+frag_keyframe+empty_moov"],
        }[container]

        fd, path = tempfile.mkstemp(suffix=ext)
        os.close(fd)
        try:
            # 立体声（HE-AAC v2 要求 2 声道）
            cmd = [self.ffmpeg, "-hide_banner", "-v", "error", "-nostdin",
                   "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
                   "-ac", "2", "-c:a", (encoder or "aac"), "-b:a", "64k"]
            cmd += self._probe_args(key, encoder) + container_args + ["-y", path]
            r = run_hidden(cmd, capture_output=True, text=True, encoding=SUBPROC_ENCODING,
                               errors="replace", timeout=60,
                               startupinfo=_hidden_startupinfo())
            if r.returncode != 0 or not os.path.isfile(path) or os.path.getsize(path) == 0:
                err = (r.stderr or "").strip()
                first = next((ln for ln in err.splitlines() if ln.strip()), "编码失败")
                return False, first[:160], None
            return True, "", path
        except Exception as exc:  # noqa: BLE001
            return False, str(exc)[:160], None

    def _can_decode(self, path: str) -> Tuple[bool, str]:
        """尝试完整解码，确认这条流真的能用。

        这是关键：ffmpeg 原生 aac 的 HE-AAC 能写出文件，
        但解码时会报 Function not implemented —— 只有真解一遍才暴露。
        """
        try:
            r = run_hidden(
                [self.ffmpeg, "-hide_banner", "-v", "error", "-nostdin",
                 "-i", path, "-f", "null", "-"],
                capture_output=True, text=True, encoding=SUBPROC_ENCODING, errors="replace", timeout=60,
                startupinfo=_hidden_startupinfo())
        except Exception as exc:  # noqa: BLE001
            return False, str(exc)[:120]
        err = (r.stderr or "").strip()
        if r.returncode != 0 or err:
            first = next((ln for ln in err.splitlines() if ln.strip()), "解码失败")
            # "Function not implemented" 说明没有对应 AOT 的解码器
            return False, first[:160]
        return True, ""

    # ------------------------------------------------------------------
    def test_profile(self, key: str, encoder: str = "aac") -> ProfileInfo:
        """实测某个 profile。结果按 (profile, encoder) 缓存。"""
        label = next((l for k, l, _ in AAC_PROFILES if k == key), key)
        desc = next((d for k, _, d in AAC_PROFILES if k == key), "")

        with self._lock:
            cache_key = (key, encoder)
            if cache_key in self._cache:
                return self._cache[cache_key]

            info = ProfileInfo(key=key, label=label, desc=desc)
            if not self.ffmpeg:
                info.tested = True
                info.error = "没有可用的 ffmpeg"
                self._cache[cache_key] = info
                return info

            # 非 AAC 编码器（mp3/flac/ac3 等）没有 profile 概念，一律放行
            if not is_aac_encoder(encoder):
                info.tested = True
                info.usable = True
                self._cache[cache_key] = info
                return info

            # 先用 MKV 试（最宽容），判断"能不能编出可解码的流"
            ok, err, path = self._encode_to(key, "mkv", encoder)
            if not ok:
                info.tested = True
                info.usable = False
                info.error = err
                info.mkv_ok = False
                info.ts_ok = False
                info.mp4_ok = False
                self._cache[cache_key] = info
                return info

            dec_ok, dec_err = self._can_decode(path)
            if not dec_ok:
                try:
                    os.remove(path)
                except OSError:
                    pass
                info.tested = True
                info.usable = False
                info.error = dec_err or "产出的流无法解码"
                self._cache[cache_key] = info
                return info

            # 关键：确认"实际产出"真的是目标 profile。
            # aac_mf 要 v2 时 ffmpeg 的输出行会写 HE-AACv2，
            # 但 ffprobe 读实际码流只有 HE-AAC（v1，缺 PS 层）。
            # 只看"编码成功 + 解码成功"会误判成支持 v2，
            # 于是软件瞄准 v2 去对齐，161 个文件转完仍然对不齐。
            actual = self._actual_profile(path)
            try:
                os.remove(path)
            except OSError:
                pass
            if not profile_really_matches(key, actual):
                info.tested = True
                info.usable = False
                info.error = (
                    f"编码器实际产出 {aac_profile_label(actual) or actual or '未知格式'}，"
                    f"达不到 {info.label}")
                self._cache[cache_key] = info
                return info

            # 编解码都通过 → 再确认各中间容器能否容纳
            info.usable = True
            info.mkv_ok = True
            for cont, attr in (("ts", "ts_ok"), ("mp4", "mp4_ok")):
                c_ok, _c_err, c_path = self._encode_to(key, cont, encoder)
                ok_flag = c_ok
                if c_ok and c_path:
                    d_ok, _ = self._can_decode(c_path)
                    ok_flag = d_ok
                    try:
                        os.remove(c_path)
                    except OSError:
                        pass
                setattr(info, attr, ok_flag)

            info.tested = True
            self._cache[cache_key] = info
            return info

    # ------------------------------------------------------------------
    def _actual_profile(self, path: str) -> str:
        """用 ffprobe 读出编码产物的**实际** profile（归一化后的 key）。

        只看 ffmpeg 输出行是不行的 —— 那是"请求值"，不是"实测值"。
        """
        if not (self.ffprobe and os.path.isfile(path)):
            return ""
        try:
            r = run_hidden(
                [self.ffprobe, "-v", "error", "-select_streams", "a:0",
                 "-show_entries", "stream=profile", "-of",
                 "default=noprint_wrappers=1:nokey=1", path],
                capture_output=True, text=True, encoding=SUBPROC_ENCODING,
                errors="replace", timeout=30,
                startupinfo=_hidden_startupinfo())
        except Exception:  # noqa: BLE001
            return ""
        raw = (r.stdout or "").strip().splitlines()
        raw = raw[0].strip() if raw else ""
        return normalize_aac_profile(raw) if raw else ""

    # ------------------------------------------------------------------
    # ------------------------------------------------------------------
    def test_decode_capability(self) -> Tuple[bool, str]:
        """实测本机 ffmpeg 能否**解码** HE-AAC（不是编码）。

        这两件事完全不同，但很容易混淆：
          · 编码 HE-AAC → 需要 libfdk_aac（原生 aac 编码器做不了，会产出坏流）
          · 解码 HE-AAC → 原生 aac 解码器自带 SBR/PS 实现，不需要任何外部库

        实测方法：生成一个正常的 AAC-LC 片段，把它的 AudioSpecificConfig 里
        sbrPresentFlag 从 0 翻成 1（只改一个 bit，其余字节完全不动），
        让它成为"声明为 HE-AAC"的流，再尝试解码。
        能解成功，说明解码器接受了 HE-AAC 配置并具备 SBR 处理能力。

        :return: (能否解码, 说明文字)
        """
        if not self.ffmpeg:
            return False, "没有可用的 ffmpeg"

        import shutil
        import struct as _struct

        tmp_dir = tempfile.mkdtemp(prefix="mp4merger_aacdec_")
        base = os.path.join(tmp_dir, "base.mp4")
        he = os.path.join(tmp_dir, "he.mp4")
        try:
            r = run_hidden(
                [self.ffmpeg, "-hide_banner", "-v", "error", "-nostdin",
                 "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
                 "-ac", "2", "-c:a", "aac", "-b:a", "96k", "-y", base],
                capture_output=True, text=True, encoding=SUBPROC_ENCODING, errors="replace", timeout=60,
                startupinfo=_hidden_startupinfo())
            if r.returncode != 0 or not os.path.isfile(base):
                return False, "无法生成测试素材"

            data = bytearray(open(base, "rb").read())
            asc = self._find_asc(data)
            if asc is None:
                return False, "未能定位测试素材的 AudioSpecificConfig"

            off, ln = asc
            # 翻转最后一个字节的 sbrPresentFlag（bit 4，从 MSB 数）
            data[off + ln - 1] |= 0x08
            with open(he, "wb") as f:
                f.write(bytes(data))

            ok, err = self._can_decode(he)
            if ok:
                return True, ("可以解码 HE-AAC（原生 aac 解码器自带 SBR 支持）"
                              "—— 读取现成的 HE-AAC 素材并重编码为 AAC-LC 没有问题")
            # 区分两种失败：完全不支持 vs 素材本身缺 SBR 数据导致的告警
            if "not implemented" in err.lower() or "unknown" in err.lower():
                return False, f"解码器不支持该配置：{err}"
            return False, f"解码未通过：{err}"
        except Exception as exc:  # noqa: BLE001
            return False, str(exc)[:120]
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

    @staticmethod
    def _find_asc(data) -> Optional[Tuple[int, int]]:
        """在 MP4 里定位 esds → DecoderSpecificInfo(0x05) 的 ASC 起始偏移与长度。"""
        try:
            import struct as _st

            def boxes(s, e):
                i = s
                out = []
                while i + 8 <= e:
                    sz = int.from_bytes(data[i:i + 4], "big")
                    typ = data[i + 4:i + 8].decode("latin1", "replace")
                    if sz == 0:
                        sz = e - i
                    if sz < 8:
                        break
                    out.append((typ, i, i + sz))
                    i += sz
                return out

            def walk(s, e):
                for typ, bs, be in boxes(s, e):
                    if typ == "esds":
                        return bs
                    nxt = {"moov": bs + 8, "trak": bs + 8, "mdia": bs + 8,
                           "minf": bs + 8, "stbl": bs + 8, "stsd": bs + 16,
                           "mp4a": bs + 36}.get(typ)
                    if nxt is not None:
                        got = walk(nxt, be)
                        if got is not None:
                            return got
                return None

            es = walk(0, len(data))
            if es is None:
                return None
            i = es + 12
            while i < min(es + 200, len(data)):
                if data[i] == 0x05:
                    j = i + 1
                    ln = 0
                    while j < len(data):
                        b = data[j]
                        ln = (ln << 7) | (b & 0x7F)
                        j += 1
                        if not (b & 0x80):
                            break
                    return (i + 1 + (j - i - 1), ln)
                i += 1
            return None
        except Exception:  # noqa: BLE001
            return None

    def usable_profiles(self, encoder: str = "aac") -> List[str]:
        return [k for k, _l, _d in AAC_PROFILES
                if self.test_profile(k, encoder).usable]

    def container_for_profile(self, key: str, encoder: str = "aac",
                              preferred: str = "ts") -> str:
        """为指定 profile 挑一个能容纳它的中间容器。

        HE-AAC 在当前 ffmpeg 下若装不进 MPEG-TS（ADTS 限制），自动退回 MKV。
        """
        info = self.test_profile(key, encoder)
        if not info.usable:
            return preferred
        order = [preferred] + [c for c in ("ts", "mp4", "mkv") if c != preferred]
        for c in order:
            flag = {"ts": info.ts_ok, "mp4": info.mp4_ok, "mkv": info.mkv_ok}[c]
            if flag:
                return c
        return "mkv"

    def summary_lines(self, encoder: str = "aac") -> List[str]:
        lines: List[str] = []
        for key, label, _desc in AAC_PROFILES:
            info = self.test_profile(key, encoder)
            if info.usable:
                conts = [c for c, ok in (("MPEG-TS", info.ts_ok),
                                         ("分片MP4", info.mp4_ok),
                                         ("MKV", info.mkv_ok)) if ok]
                lines.append(f"✔ {label}：可用（中间容器支持：{'/'.join(conts)}）")
            else:
                lines.append(f"✘ {label}：不可用 —— {info.error}")
        return lines
