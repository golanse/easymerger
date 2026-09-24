"""用 ffprobe 探测视频参数。"""

from __future__ import annotations

import json
import os
import subprocess

from .subproc import popen_hidden, run_hidden
from typing import Dict, List, Optional, Sequence

from .ffmpeg_env import _hidden_startupinfo
from .models import VideoInfo
from .natsort import natural_key
from .textio import SUBPROC_ENCODING

__all__ = ["probe", "probe_many", "scan_folder", "MP4_EXTS", "is_video_file"]

MP4_EXTS = {".mp4", ".m4v", ".mov", ".mkv", ".ts", ".m4a"}  # 允许导入的常见视频封装


def is_video_file(path: str, exts: Optional[set] = None) -> bool:
    exts = exts or {".mp4", ".m4v", ".mov", ".mkv"}
    return os.path.splitext(path)[1].lower() in exts


def _to_int(value, default=0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _to_float(value, default=0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _parse_fps(rate: str) -> float:
    """把 '30000/1001' 转成 29.97。"""
    if not rate:
        return 0.0
    if "/" in rate:
        num, _, den = rate.partition("/")
        try:
            num_f, den_f = float(num), float(den)
            return num_f / den_f if den_f else 0.0
        except ValueError:
            return 0.0
    return _to_float(rate)


def _rotation_of(stream: Dict) -> int:
    for sd in stream.get("side_data_list", []) or []:
        if "rotation" in sd:
            return _to_int(sd.get("rotation"))
    tags = stream.get("tags", {}) or {}
    for key in ("rotate", "rotation"):
        if key in tags:
            return _to_int(tags[key])
    return 0


def probe(path: str, ffprobe: str) -> VideoInfo:
    """探测单个文件；失败时返回带 error 字段的 VideoInfo。"""
    info = VideoInfo(path=path, name=os.path.basename(path))
    try:
        info.size = os.path.getsize(path)
    except OSError:
        info.size = 0

    cmd = [
        ffprobe, "-v", "error", "-print_format", "json",
        "-show_format", "-show_streams", "-show_entries",
        "stream=index,codec_type,codec_name,profile,level,width,height,coded_width,coded_height,"
        "pix_fmt,sample_aspect_ratio,display_aspect_ratio,r_frame_rate,avg_frame_rate,bit_rate,"
        "sample_rate,channels,channel_layout,duration:format=duration,size,bit_rate,format_name",
        path,
    ]
    try:
        r = run_hidden(cmd, capture_output=True, text=True, encoding=SUBPROC_ENCODING, errors="replace",
                           timeout=60, startupinfo=_hidden_startupinfo())
    except Exception as exc:  # noqa: BLE001
        info.error = f"ffprobe 执行失败：{exc}"
        return info

    if r.returncode != 0 or not r.stdout.strip():
        info.error = (r.stderr or "无法解析文件（可能不是有效视频或已损坏）").strip()[:300]
        return info

    try:
        data = json.loads(r.stdout)
    except json.JSONDecodeError as exc:
        info.error = f"ffprobe 输出解析失败：{exc}"
        return info

    fmt = data.get("format", {}) or {}
    info.container = fmt.get("format_name", "") or ""
    info.overall_bitrate = _to_int(fmt.get("bit_rate"))
    info.duration = _to_float(fmt.get("duration"))

    for st in data.get("streams", []) or []:
        ctype = st.get("codec_type")
        if ctype == "video" and not info.has_video:
            info.has_video = True
            info.v_codec = st.get("codec_name", "") or ""
            info.v_profile = st.get("profile", "") or ""
            info.v_level = str(st.get("level", "") or "")
            info.v_pix_fmt = st.get("pix_fmt", "") or ""
            info.width = _to_int(st.get("width"))
            info.height = _to_int(st.get("height"))
            info.sar = st.get("sample_aspect_ratio", "") or "1:1"
            info.dar = st.get("display_aspect_ratio", "") or ""
            info.fps_raw = st.get("r_frame_rate", "") or ""
            info.fps = _parse_fps(st.get("r_frame_rate", "")) or _parse_fps(st.get("avg_frame_rate", ""))
            info.v_bitrate = _to_int(st.get("bit_rate"))
            info.rotation = _rotation_of(st)
            if not info.duration:
                info.duration = _to_float(st.get("duration"))
        elif ctype == "audio" and not info.has_audio:
            info.has_audio = True
            info.a_codec = st.get("codec_name", "") or ""
            info.a_profile = st.get("profile", "") or ""
            info.a_sample_rate = _to_int(st.get("sample_rate"))
            info.a_channels = _to_int(st.get("channels"))
            info.a_layout = st.get("channel_layout", "") or ""
            info.a_bitrate = _to_int(st.get("bit_rate"))
            if not info.duration:
                info.duration = _to_float(st.get("duration"))

    if not info.has_video and not info.has_audio:
        info.error = "文件中没有找到视频或音频流"
    return info


def probe_many(paths: Sequence[str], ffprobe: str, on_progress=None) -> List[VideoInfo]:
    infos: List[VideoInfo] = []
    total = len(paths)
    for i, p in enumerate(paths):
        infos.append(probe(p, ffprobe))
        if on_progress:
            on_progress(i + 1, total, p)
    return infos


def scan_folder(folder: str, recursive: bool = False, exts: Optional[set] = None) -> List[str]:
    """扫描文件夹内的视频文件，返回按自然排序后的路径列表。"""
    exts = exts or {".mp4", ".m4v", ".mov", ".mkv"}
    found: List[str] = []
    if recursive:
        for dirpath, dirnames, filenames in os.walk(folder):
            dirnames.sort()
            for fn in filenames:
                if os.path.splitext(fn)[1].lower() in exts:
                    found.append(os.path.join(dirpath, fn))
    else:
        try:
            entries = os.listdir(folder)
        except OSError:
            return []
        for fn in entries:
            fp = os.path.join(folder, fn)
            if os.path.isfile(fp) and os.path.splitext(fn)[1].lower() in exts:
                found.append(fp)
    found.sort(key=lambda p: natural_key(os.path.basename(p)))
    return found


if __name__ == "__main__":
    import sys
    from .ffmpeg_env import detect_env
    env = detect_env()
    for p in sys.argv[1:]:
        print(probe(p, env.status.ffprobe))
