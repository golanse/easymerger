"""任务执行器：把一个 MergeTask 跑完（阻塞，供工作线程调用）。

阶段划分（对应进度条）：
    准备 / 检测 → 转码（逐文件，按时长加权） → 合并 → 提取音频 → 清理缓存
"""

from __future__ import annotations

import os
import subprocess

from .subproc import run_hidden
import time
import traceback
from typing import Callable, List, Optional, Sequence

from .commands import (
    build_concat_cached_cmd,
    build_extract_audio_cmd,
    build_lossless_cmd,
    build_transcode_cmd,
    cache_filename,
    resolve_container,
    resolve_intermediate,
    unique_path,
    write_concat_list,
)
from .compat import check_lossless
from .merge_plan import plan_merge
from .models import STATUS, MergeTask, VideoInfo
from .runner import CancelToken, run_ffmpeg

__all__ = ["MergeJob", "JobCallbacks"]

# 走 MPEG-TS 中间容器时需要 adtstoasc 过滤器的音频编码器
AAC_LIKE = ("aac", "libfdk_aac", "libfaac", "libaacplus", "aac_mf")

# 各阶段在总进度条中的占比
S_PREPARE = 0.02
S_TRANSCODE_END = 0.78
S_CONCAT_END = 0.93
S_AUDIO_END = 0.99


def is_hw_name(name: str) -> bool:
    """编码器名是否属于硬件编码（GPU）家族。"""
    n = (name or "").lower()
    return any(k in n for k in ("nvenc", "qsv", "amf", "videotoolbox", "vaapi"))


class JobCallbacks:
    """回调集合：由上层（Qt 信号 / 命令行）实现。"""

    def __init__(
        self,
        on_stage: Optional[Callable[[str, str], None]] = None,
        on_progress: Optional[Callable[[float, str], None]] = None,
        on_log: Optional[Callable[[str], None]] = None,
        on_status: Optional[Callable[[str], None]] = None,
    ):
        self.on_stage = on_stage or (lambda *_: None)
        self.on_progress = on_progress or (lambda *_: None)
        self.on_log = on_log or (lambda *_: None)
        self.on_status = on_status or (lambda *_: None)


class MergeJob:
    """执行一次合并任务。"""

    def __init__(
        self,
        task: MergeTask,
        ffmpeg: str,
        ffprobe: str,
        callbacks: Optional[JobCallbacks] = None,
        cancel: Optional[CancelToken] = None,
        should_pause: Optional[Callable[[], bool]] = None,
    ):
        self.task = task
        self.ffmpeg = ffmpeg
        self.ffprobe = ffprobe
        self.cb = callbacks or JobCallbacks()
        self.cancel = cancel or CancelToken()
        self.should_pause = should_pause or (lambda: False)
        self._cache_files: List[str] = []
        self._final_list: str = ""

    # ------------------------------------------------------------------
    def log(self, text: str) -> None:
        # 勾选"保留任务日志"时放宽上限，避免长任务把前面的记录挤掉
        keep = 20000 if getattr(self.task.output, "keep_log", False) else 2000
        self.task.add_log(text, keep=keep)
        self.cb.on_log(text)

    def stage(self, text: str) -> None:
        self.task.stage = text
        self.cb.on_stage(text, self.task.status)

    def progress(self, value: float, speed: str = "") -> None:
        self.task.progress = max(0.0, min(1.0, value))
        if speed:
            self.task.speed = speed
        self.cb.on_progress(self.task.progress, speed)

    # ------------------------------------------------------------------
    def run(self) -> MergeTask:
        t = self.task
        t.started_at = time.time()
        t.error = ""
        t.output_path = ""
        t.audio_path = ""
        t.status = STATUS.RUNNING
        t.progress = 0.0
        self.cb.on_status(STATUS.RUNNING)
        try:
            self._run_inner()
        except Exception as exc:  # noqa: BLE001
            t.status = STATUS.FAILED
            t.error = f"{exc}"
            self.log(f"任务异常：{exc}")
            self.log(traceback.format_exc(limit=3))
        finally:
            self.cleanup()
            if t.status == STATUS.RUNNING:
                t.status = STATUS.DONE if not t.error else STATUS.FAILED
            t.progress = 1.0 if t.status == STATUS.DONE else t.progress
            t.finished_at = time.time()
            self.cb.on_status(t.status)
            self.progress(t.progress)
        return t

    def _run_inner(self) -> None:
        t = self.task
        if not t.files:
            t.error = "任务没有文件"
            t.status = STATUS.FAILED
            self.log(t.error)
            return
        if not self.ffmpeg or not os.path.isfile(self.ffmpeg):
            t.error = "未找到 ffmpeg，无法执行合并"
            t.status = STATUS.FAILED
            self.log(t.error)
            return

        self.stage("准备")
        self.log(f"任务开始：{t.name}（{len(t.files)} 个文件，总时长 {t.total_duration_str}）")
        out_dir = t.output.out_dir or t.source_dir
        if not out_dir:
            out_dir = t.source_dir
        os.makedirs(out_dir, exist_ok=True)
        # AV1 装不进 MOV（ffmpeg 报 "av1 only supported in MP4"），自动改用 mp4
        container = resolve_container(t.params.vcodec, t.output.container)
        if container != (t.output.container or "mp4").lower().lstrip("."):
            self.log(f"提示：{t.params.vcodec} 不支持 {t.output.container} 容器，"
                     f"输出格式已自动改为 {container}")
        out_path = unique_path(os.path.join(out_dir, f"{t.output.out_name}.{container}"))
        t.output.out_dir = out_dir
        t.output.container = container
        if os.path.basename(out_path) != f"{t.output.out_name}.{container}":
            self.log(f"输出文件已存在，自动改名为：{os.path.basename(out_path)}")

        # ---- 兼容性检测（流级别）----
        #
        # 这里不再是"全一致才 copy、否则全部重编码"，而是按**流维度**
        # 分别决策：视频参数一致就视频 copy、只重编码真正不一致的那一流。
        # 现实中"不一致"往往只是某一项（比如只有几个文件的 AAC Profile
        # 不同），此时把整段视频重编码一遍是极大的浪费。
        self._audio_copy = False
        use_lossless = False
        video_copy = False
        audio_copy = False
        plan_intermediate = "ts"
        if t.params.check_lossless:
            self.stage("检测参数一致性")
            if not t.compat.checked:
                t.compat = check_lossless(t.files)
            self.log(f"无损检测：{t.compat.summary}")
            for m in t.compat.mismatches:
                self.log(f"  差异：{m}")

            plan = plan_merge(t.files, container)
            t.plan = plan          # 存起来供界面/任务详情展示
            self.log(f"合并策略：{plan.label}")
            self.log(f"  · 视频流：{'直接 copy（不重编码）' if plan.video_copy else '重编码'}")
            self.log(f"  · 音频流：{'直接 copy' if plan.audio_copy else '重编码'}")
            for m in plan.video_mismatches:
                self.log(f"  视频差异：{m}")
            for m in plan.audio_mismatches:
                self.log(f"  音频差异：{m}")

            if plan.mode == "lossless":
                use_lossless = True
            elif plan.mode == "smart":
                video_copy = True
                plan_intermediate = plan.intermediate
                self.log("视频参数完全一致，将直接复制视频流，"
                         "只统一处理音频 —— 比全转码快很多且视频零损失")
            elif plan.audio_copy:
                # 视频不一致、但音频一致：视频必须重编码，
                # 音频却没必要跟着遭殃（慢，且白白损失一次音质）。
                audio_copy = True
                self.log("音频参数完全一致，音频流将直接复制（不重编码）"
                         " —— 只重编码视频")
        else:
            self.log("未勾选无损检测，按设置的参数转码后合并")

        self.progress(S_PREPARE)

        # ---- 兜底校验：确认音频 profile 真能产出可解码的流 ----
        # 这一步必须在开始转码前做，否则用户会白等几十分钟拿到一个坏文件。
        if not use_lossless:
            bad = self._check_audio_profile(t)
            if bad:
                t.status = STATUS.FAILED
                t.error = bad
                self.log(bad)
                return

        if use_lossless:
            t.mode = "lossless"
            self._do_lossless(out_path)
        else:
            t.mode = "smart" if video_copy else "transcode"
            # 优先走单遍合并（快约 2 倍、无中间文件）。
            #
            # 唯一不能走单遍的情况：video_copy（只重编音频）——
            # 单遍是整条链路重编码，会破坏"视频零损失"的前提。
            #
            # 关于 audio_copy：技术上单遍确实不能音频直通
            # （filter_complex 输出的流不能 streamcopy，硬来会报
            #  "Filtering and streamcopy cannot be used together"）。
            # 但之前因此**整个放弃单遍**是错的权衡：
            #   单遍  = 11 次调用、不落缓存      → 快约 2 倍
            #   分段  = 132 次调用 + 写读缓存    → 慢一倍，只为保住音频
            # 用户的核心诉求是"快"，而 64~128kbps 的 AAC 重编码一次
            # 听感损失很小（且下面会自动提码率补偿）。所以改为**单遍优先**。
            if not video_copy:
                target = self._unify_target()
                self._audio_copy = audio_copy
                if audio_copy:
                    # 单遍架构下音频必然跟着重编码一次，这里提一档码率
                    # 补偿（64→96 / 128→160 …），把听感损失降到可忽略。
                    self._bump_audio_bitrate()
                    self.log("各文件音频参数一致，但单遍合并会让音频随视频"
                             "一起重编码一次；已自动提高音频码率以保持听感"
                             "（若你更看重音质原样保留，可改用「智能直通」）")
                if self._try_singlepass(out_path, target):
                    # 单遍已处理（成功或失败都已设好状态）
                    done = t.status in (STATUS.DONE, STATUS.CANCELED, STATUS.FAILED)
                    if done:
                        self._after_merge(out_path)
                        return
                    self.log("单遍合并未产出文件，改用分段转码合并（兼容模式）")
                    t.status = STATUS.RUNNING
            self._do_transcode(out_path, video_copy=video_copy,
                               intermediate=plan_intermediate,
                               audio_copy=audio_copy)

        self._after_merge(out_path)

    def _after_merge(self, out_path: str) -> None:
        """合并产出后的共同收尾：校验 → 记路径 → 提取音频 → 置完成。"""
        t = self.task
        if self.cancel.cancelled or t.status in (STATUS.CANCELED, STATUS.FAILED):
            # 失败/取消时也要存日志 —— 用户恰恰是这时候最需要看日志。
            # 注意：此时输出文件可能不存在，所以直接按预期路径推 .log 名字。
            if getattr(t.output, "keep_log", False):
                self._save_log(out_path)
            return
        if not os.path.isfile(out_path):
            t.error = "输出文件未生成，请查看日志"
            t.status = STATUS.FAILED
            return

        t.output_path = out_path
        self.log(f"合并完成：{out_path}")

        # 勾选了"保留任务日志"就落盘，放在输出文件旁边
        if getattr(t.output, "keep_log", False):
            self._save_log(out_path)

        # ---- 提取音频 ----
        if t.output.extract_audio:
            self._do_extract_audio(out_path)

        t.status = STATUS.DONE
        self.stage("完成")
        self.log("任务完成")

    # 常见的 AAC 码率档位，提一档用于补偿有损→有损的损失
    _ABR_LADDER = [48, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320]

    def _bump_audio_bitrate(self) -> None:
        """把音频码率提高一档（单遍合并时的听感补偿）。

        只提一档：提太多会明显增大体积，而 64→96 已经足够
        抵消一次 AAC 重编码的损失。用户显式设过的更大值保持不动
        （不盲目往上加，避免体积失控）。
        """
        p = self.task.params
        cur = getattr(p, "audio_bitrate_kbps", 0) or 0
        if cur <= 0:
            return
        for step in self._ABR_LADDER:
            if step > cur:
                # 不超过 192：再高对听感无益，只会白白增大体积
                p.audio_bitrate_kbps = min(step, 192)
                self.log(f"音频码率 {cur}k → {p.audio_bitrate_kbps}k"
                         f"（补偿一次重编码）")
                return

    def _save_log(self, out_path: str) -> None:
        """把完整运行日志写成 .log 文件，与输出文件放在一起。"""
        t = self.task
        try:
            base = os.path.splitext(out_path)[0]
            log_path = base + ".log"
            lines = [
                "=" * 60,
                "EasyMerger 任务日志",
                "=" * 60,
                f"任务名称：{t.name}",
                f"输出文件：{out_path}",
                f"文件数量：{len(t.files)}",
                f"合并方式：{t.mode}",
                f"结束状态：{t.status}",
            ]
            if t.error:
                lines.append(f"错误信息：{t.error}")
            lines.append("=" * 60)
            lines.append("")
            lines.extend(t.log_lines)
            with open(log_path, "w", encoding="utf-8") as f:
                f.write("\n".join(lines) + "\n")
            self.log(f"日志已保存：{log_path}")
        except OSError as e:
            self.log(f"日志保存失败：{e}")

    def _unify_target(self) -> dict:
        """算出把所有分片统一到什么参数。

        规则与「参数自洽校正」一致：取最大值（降质不可逆，
        体积变大是可接受的代价），用户显式指定的优先。
        """
        t = self.task
        p = t.params
        files = [f for f in t.files if f.width and f.height]
        if not files:
            return {"width": 0, "height": 0, "fps": 0, "pix_fmt": p.pix_fmt}

        # 分辨率：用户指定了就用用户的，否则取面积最大的
        if p.scale_mode == "value" and p.scale_w and p.scale_h:
            tw, th = p.scale_w, p.scale_h
        else:
            biggest = max(files, key=lambda f: f.width * f.height)
            tw, th = biggest.width, biggest.height

        # 帧率：用户指定了就用用户的，否则取最高的
        if p.fps_mode == "value" and p.fps_value:
            tfps = float(p.fps_value)
        else:
            tfps = max((f.fps or 0) for f in t.files) or 0.0

        return {"width": tw, "height": th, "fps": tfps, "pix_fmt": p.pix_fmt}

    # ------------------------------------------------------------------
    def _check_audio_profile(self, t: "MergeTask") -> str:
        """动工前确认所选 AAC Profile 可用，返回错误描述（空串 = 通过）。

        ffmpeg 原生 aac 编码器的 HE-AAC 会产出无法解码的坏流，
        而且它**不会报错**——文件照样生成、时长也写得进去，
        只有真去解码时才暴露。所以必须在动手前拦下来。
        """
        ac = (t.params.acodec or "").lower()
        if ac not in AAC_LIKE:
            return ""
        prof = getattr(t.params, "aac_profile", "aac_low") or "aac_low"
        if prof == "aac_low":
            return ""
        try:
            from .audio_profile import AudioProfileProbe
            probe = AudioProfileProbe(self.ffmpeg, self.ffprobe)
            info = probe.test_profile(prof, ac)
        except Exception:  # noqa: BLE001
            return ""   # 探测本身出问题就不拦，交由 ffmpeg 自己报错
        if info.tested and not info.usable:
            # 顺带确认解码能力，避免用户误以为"HE-AAC 素材我也读不了"
            dec_note = ""
            try:
                from .audio_profile import AudioProfileProbe
                ok, msg = AudioProfileProbe(self.ffmpeg, self.ffprobe).test_decode_capability()
                dec_note = ("\n\n另外说明：这只是**编码**用不了。"
                            "读取现成的 HE-AAC 素材并转成 AAC-LC 不受影响\n"
                            f"（实测：{msg}）" if ok
                            else f"\n\n注意：本机解码 HE-AAC 也未通过：{msg}")
            except Exception:  # noqa: BLE001
                dec_note = ""
            return (
                f"所选音频 Profile「{info.label}」在当前 ffmpeg 下无法用于输出。\n"
                f"ffmpeg 报错：{info.error}\n\n"
                "原因：ffmpeg 自带的 aac 编码器对 HE-AAC 的支持有缺陷，"
                "会输出标准不支持的码流（文件能生成，但没有声音）。\n"
                "解决办法（任选其一）：\n"
                "  1）把 AAC Profile 改回 AAC-LC（推荐，兼容性最好）；\n"
                "  2）换用带 libfdk_aac 的 ffmpeg 构建。" + dec_note)
        return ""

    def _do_lossless(self, out_path: str) -> None:
        t = self.task
        self.stage("无损合并（-c copy）")
        list_path = os.path.join(t.source_dir, f".mp4merger_{t.id}_list.txt")
        write_concat_list([f.path for f in t.files], list_path)
        self._final_list = list_path
        cmd = build_lossless_cmd(list_path, out_path, t.files[0],
                                 faststart=getattr(t.output, "faststart", True))

        def on_prog(v, p):
            self.progress(v, p.speed)
            self.task.eta = p.eta_text(t.total_duration)

        do_faststart = getattr(t.output, "faststart", True)

        def on_finishing():
            if not do_faststart:
                return
            self.stage("收尾：整理文件索引")
            self.log("视频数据已写完，正在整理文件索引（faststart）…")
            try:
                mb = os.path.getsize(out_path) / (1024 * 1024)
                self.log(f"输出文件约 {mb:,.0f} MB，这一步需要重写一遍文件，"
                         f"文件越大越久（通常几十秒以内），期间没有进度可显示。")
            except OSError:
                pass

        rc = run_ffmpeg(self.ffmpeg, cmd, t.total_duration, on_progress=on_prog,
                        on_log=self.log, cancel=self.cancel,
                        span_start=S_PREPARE, span_end=S_AUDIO_END if not t.output.extract_audio else S_CONCAT_END,
                        on_finishing=on_finishing)
        if rc == -1:
            t.status = STATUS.CANCELED
            self.log("任务已取消")
        elif rc != 0:
            t.status = STATUS.FAILED
            t.error = self._tail_error("无损合并失败")
            self.log(t.error)

    def _diagnose_source(self, path: str, name: str) -> None:
        """判断源文件码流到底干不干净 —— 用解码校验，不靠猜。

        ffmpeg 播放器（以及 WMP / 系统自带播放器）都有错误掩盖：
        碰到坏包会跳帧或插值，画面照样能看、声音照样能听。
        但 -c:v copy 走的是 bitstream filter，对坏包零容忍。

        所以"播放器能播"**不能证明**码流没问题。
        这条命令才是判断依据：
            ffmpeg -v error -i 文件 -f null -
        有输出 = 码流确实有错；没输出 = 码流干净，问题在别处。
        """
        self.log(f"    正在校验「{name}」的码流是否完好（解码全片）…")
        try:
            r = run_hidden([self.ffmpeg, "-v", "error", "-xerror", "-i", path,
                            "-f", "null", "-"],
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                           text=True, encoding="utf-8", errors="replace",
                           timeout=600)
        except Exception as exc:  # noqa: BLE001
            self.log(f"    校验未能完成：{exc}")
            return
        err = (r.stderr or "").strip()
        if not err:
            self.log("    ✅ 码流校验通过：全片解码无任何错误。")
            self.log("       → 说明文件本身是好的，问题出在 TS 封装环节"
                     "（换 MKV 通常可解）。")
            return
        first = [ln for ln in err.splitlines() if ln.strip()][:3]
        self.log("    ⚠ 码流校验发现问题（播放器靠容错掩盖了）：")
        for ln in first:
            self.log("       " + ln.strip()[:160])
        self.log("       → 源文件确实存在异常数据，重编码这一集即可救回。")

    # ------------------------------------------------------------------
    def _try_singlepass(self, out_path: str, target: dict) -> bool:
        """尝试单遍合并：一次解码→拼接→编码→输出，不落中间文件。

        返回 True 表示已完成（无论成败都已设置好任务状态）。

        为什么优先走这条路：常规做法是「每段转码成缓存 → 再拼接」，
        每个字节写了两遍、读了两遍。实测 8 段共 120 秒素材：
            两遍（TS 缓存）  25.8 秒（转码 23.3 + 拼接 2.4）
            单遍（concat）   11.9 秒
        → 快 2.17 倍，输出体积一致（107.2 vs 107.0 MB）。
        而且不产生缓存文件，省 I/O 也省清理，画质只损失一次。
        """
        from .commands import (build_singlepass_concat_cmd,
                               plan_singlepass_batches, SINGLEPASS_MAX_INPUTS)

        t = self.task
        n = len(t.files)
        batches = plan_singlepass_batches(n)

        # 单遍走 concat 滤镜，音频会经过 filtergraph ——
        # 此时 ffmpeg **禁止**音频 streamcopy（会直接报 Invalid argument）。
        # 所以这里即使音频参数本就一致，也只能重编码。
        # 必须提前说清楚，否则日志上一行写"音频直接 copy"、
        # 实际命令却是 -c:a aac，用户完全对不上。
        if getattr(self, "_audio_copy", False):
            self.log("注意：单遍合并用 concat 滤镜拼接，音频会经过滤镜链，"
                     "ffmpeg 不允许此时再 streamcopy。")
            self.log("      因此音频将按你的设置重编码（不是 copy）——"
                     "这与是否设为 HE-AACv2 无关，是 concat 滤镜的硬性限制。")

        def finish(rc: int) -> None:
            if rc == -1:
                t.status = STATUS.CANCELED
                self.log("任务已取消")
            elif rc != 0:
                t.status = STATUS.FAILED
                t.error = self._tail_error("合并失败")
                self.log(t.error)
            else:
                # 关键：成功时**必须**设状态。
                #
                # 之前这里漏了 rc == 0 的分支，导致状态还是 RUNNING，
                # 外层误判为"单遍没产出文件"，随即又跑了一遍老的分段
                # 转码流程 —— 132 个文件等于编码了两次。
                # 失败和取消都设了状态，唯独最常用的成功路径没设。
                if os.path.isfile(out_path):
                    t.status = STATUS.DONE
                else:
                    t.status = STATUS.FAILED
                    t.error = "合并返回成功但输出文件不存在，请查看日志"
                    self.log(t.error)

        def on_prog(v, p):
            self.progress(v, p.speed)
            self.task.eta = p.eta_text(t.total_duration)

        if len(batches) == 1:
            cmd = build_singlepass_concat_cmd(
                [f.path for f in t.files], t.files, t.params, out_path, target,
                audio_copy=getattr(self, "_audio_copy", False))
            self.stage("转码合并（单遍，无中间文件）")
            self.log(f"单遍合并 {n} 个文件：解码→拼接→编码一次完成，不产生缓存文件")
            rc = run_ffmpeg(self.ffmpeg, cmd, t.total_duration,
                            on_progress=on_prog, on_log=self.log,
                            cancel=self.cancel,
                            span_start=S_PREPARE,
                            span_end=S_AUDIO_END if not t.output.extract_audio
                            else S_CONCAT_END)
            finish(rc)
            return True

        # 文件太多（> %d 个）：分批 concat，中间结果走临时文件级联。
        # 一次性打开上百个输入会让命令行极长、句柄吃紧，稳定性差。
        # 分批后仍然比"全部转码成缓存"快，因为少了一整轮编码。
        self.stage(f"转码合并（分 {len(batches)} 批）")
        self.log(f"共 {n} 个文件，分 {len(batches)} 批处理（单批上限 "
                 f"{SINGLEPASS_MAX_INPUTS} 个，避免一次性打开过多文件）")
        import tempfile
        tmpdir = os.path.join(t.source_dir, f".mp4merger_{t.id}_parts")
        os.makedirs(tmpdir, exist_ok=True)
        parts = []
        cursor = 0
        span = (S_CONCAT_END - S_PREPARE) / len(batches)
        try:
            for bi, bsz in enumerate(batches):
                sub_files = t.files[cursor:cursor + bsz]
                part_path = os.path.join(tmpdir, f"part{bi}.mkv")
                parts.append(part_path)
                self._cache_files.append(part_path)
                cmd = build_singlepass_concat_cmd(
                    [f.path for f in sub_files], sub_files, t.params,
                    part_path, target,
                    audio_copy=getattr(self, "_audio_copy", False))
                self.log(f"第 {bi + 1}/{len(batches)} 批（{bsz} 个文件）")

                def on_prog_b(v, p, _b=bi, _c=cursor):
                    self.progress(S_PREPARE + (_b + v) * span, p.speed)

                rc = run_ffmpeg(self.ffmpeg, cmd,
                                sum((f.duration or 0) for f in sub_files),
                                on_progress=on_prog_b, on_log=self.log,
                                cancel=self.cancel)
                if rc != 0:
                    self.task.status = STATUS.FAILED
                    self.task.error = self._tail_error(
                        f"第 {bi + 1} 批合并失败")
                    self.log(self.task.error)
                    return True
                cursor += bsz

            # 级联：把各批结果拼起来（参数已一致，直接 copy）
            self.stage("合并各批次")
            list_path = os.path.join(tmpdir, "parts.txt")
            write_concat_list(parts, list_path)
            cmd = build_concat_cached_cmd(
                parts, out_path, "mkv",
                faststart=getattr(t.output, "faststart", True),
                cache_id=t.id)
            rc = run_ffmpeg(self.ffmpeg, cmd, t.total_duration,
                            on_progress=on_prog, on_log=self.log,
                            cancel=self.cancel,
                            span_start=S_CONCAT_END - 0.02,
                            span_end=S_AUDIO_END if not t.output.extract_audio
                            else S_CONCAT_END)
            finish(rc)
            return True
        finally:
            # 分批的临时文件必须清掉 —— 它们落在**用户的源文件目录**里。
            #
            # 之前这里全是 `except OSError: pass`，删不掉就静默放过。
            # 结果就是用户源目录里留下一个 .mp4merger_xxx_parts 文件夹，
            # 里面是几十 GB 的中间文件，而软件一声不吭。
            #
            # Windows 上删不掉通常是文件还被 ffmpeg 句柄占着（刚取消、
            # 进程还没完全退出），稍等一下重试往往就成了。
            self._cleanup_tmpdir(tmpdir)

    def _cleanup_tmpdir(self, tmpdir: str, retries: int = 5) -> None:
        """删除分批产生的临时目录，带重试与失败告警。"""
        if not tmpdir or not os.path.isdir(tmpdir):
            return

        def _try_remove(path: str) -> bool:
            for attempt in range(retries):
                try:
                    if os.path.isfile(path):
                        os.remove(path)
                        return True
                except OSError:
                    if attempt + 1 < retries:
                        time.sleep(0.4 * (attempt + 1))
            return not os.path.exists(path)

        leftover: List[str] = []
        try:
            for name in os.listdir(tmpdir):
                _p = os.path.join(tmpdir, name)
                if not _try_remove(_p):
                    leftover.append(name)
        except OSError as e:
            self.log(f"临时目录无法列举：{tmpdir}（{e}）")
            return

        if leftover:
            # 删不掉必须明说，不能让用户自己发现源目录多了几十 GB
            self.log(f"⚠ 有 {len(leftover)} 个临时文件未能删除：{tmpdir}")
            self.log(f"   残留：{', '.join(leftover[:5])}"
                     f"{' …' if len(leftover) > 5 else ''}")
            self.log("   通常是文件仍被占用（任务刚取消时常见），"
                     "可稍后手动删除该文件夹。")
            return

        try:
            os.rmdir(tmpdir)
        except OSError as e:
            self.log(f"⚠ 临时目录未能删除：{tmpdir}（{e}）")

    # ------------------------------------------------------------------
    def _do_transcode(self, out_path: str, video_copy: bool = False,
                      intermediate: str = "ts", audio_copy: bool = False) -> None:
        """转码每个分片为统一格式。

        video_copy=True 时视频流直接复制，只重编码音频（"智能直通"）。
        各分片视频参数一致、只有音频不统一时用它，比全转码快一个数量级，
        而且视频部分零损失。
        """
        t = self.task

        # 视频直通与"需要改像素"互斥 —— 必须先确认。
        # 不查的话，ffmpeg 会直接报
        #   "Filtergraph ... was specified, but codec copy was selected"
        # 而且这种失败发生在**第一个文件**，250 集的任务一上来就挂。
        #
        # 典型触发：参数自动统一把 scale_mode/fps_mode 设成 value
        # （值取自首个文件，其实与源完全相同）→ 滤镜链非空 → 冲突。
        if video_copy and t.files:
            try:
                from core.commands import video_copy_conflict
                why = video_copy_conflict(t.params, t.files[0])
            except Exception:  # noqa: BLE001
                why = ""
            if why:
                self.log(f"⚠ 无法视频直通：{why}")
                self.log("  已改为视频重编码（否则 ffmpeg 会拒绝执行："
                         "滤镜与 streamcopy 不能并用）")
                video_copy = False

        total = t.total_duration or float(len(t.files))
        weights = [((f.duration or 0) / total) if t.total_duration else (1.0 / len(t.files))
                   for f in t.files]
        cache_paths: List[str] = []

        # 直通失败时的容器回退状态。
        # MPEG-TS 对视频码流要做 Annex-B 转换（bitstream filter），
        # 遇到个别文件的异常包会直接拒绝；而 MKV 原样承载，
        # 实测同一个文件 copy 到 TS 失败、copy 到 MKV 却成功。
        # 所以优先换容器（零损失），实在不行才重编码。
        self._force_mkv = False

        cursor = S_PREPARE
        _restart = True
        while _restart:
          _restart = False
          cache_paths = []
          cursor = S_PREPARE
          for idx, (info, w) in enumerate(zip(t.files, weights)):
              if self.cancel.cancelled:
                  t.status = STATUS.CANCELED
                  return
              self._wait_if_paused()
              # AV1 强制走 MKV 中间容器（TS 会让 AV1 退化成 bin_data 进而丢流）；
              # 视频直通时用它自己挑的容器（源编码能否装进 TS 决定）。
              inter = ("mkv" if getattr(self, "_force_mkv", False) else intermediate) \
                  if video_copy else \
                  resolve_intermediate(t.params.vcodec, t.params.intermediate)
              cache_name = cache_filename(t.id, idx, inter)
              cache_path = os.path.join(os.path.dirname(os.path.abspath(info.path)), cache_name)
              stage_name = "统一音频" if video_copy else "转码"
              self.stage(f"{stage_name} {idx + 1}/{len(t.files)}：{info.name}")
              self.log(f"[{idx + 1}/{len(t.files)}] {stage_name} {info.name} → 缓存 {cache_name}")
              cmd = build_transcode_cmd(info.path, t.params, cache_path, info,
                                        duration=info.duration, ffmpeg=self.ffmpeg,
                                        video_copy=video_copy, audio_copy=audio_copy,
                                        container=inter)
              # 把完整命令行写进日志。
              # 用户需要能自己确认"到底有没有真的用上硬件编码" ——
              # 看日志里的 -c:v h264_amf 比看界面提示更直接、更可信。
              vco = ""
              for _a in cmd:
                  if _a == "-c:v":
                      vco = cmd[cmd.index("-c:v") + 1] if cmd.index("-c:v") + 1 < len(cmd) else ""
                      break
              self.log("  命令：ffmpeg " + " ".join(cmd[:2]))
              if video_copy and (vco or "").lower() == "copy":
                  self.log("  视频编码器：-c:v copy　← 直接复制视频流（不重编码，零损失）")
              else:
                  self.log(f"  视频编码器：-c:v {vco or '（未找到）'}"
                           + ("　← 硬件编码（GPU）" if is_hw_name(vco) else "　← CPU 软编"))
              self.log("  完整参数：" + " ".join(cmd))
              # 先登记再转码：这样无论成功、失败还是用户中途点「停止」，
              # 最后都能被 finally 清理掉。ffmpeg 一旦启动就可能已经建了输出文件，
              # 若等转码成功后才登记，失败/取消时就会在用户源目录留下垃圾。
              self._cache_files.append(cache_path)
              end = cursor + (S_TRANSCODE_END - S_PREPARE) * w

              def on_prog(v, p, _end=end):
                  self.progress(v, p.speed)
                  self.task.eta = p.eta_text(t.total_duration)

              rc = run_ffmpeg(self.ffmpeg, cmd, info.duration, on_progress=on_prog,
                              on_log=self.log, cancel=self.cancel,
                              span_start=cursor, span_end=end)
              if rc == -1:
                  t.status = STATUS.CANCELED
                  self.log("任务已取消")
                  return
              if rc != 0 or not os.path.isfile(cache_path):
                  # ----------------------------------------------------------
                  # 单个文件失败 → **不能让整个任务陪葬**。
                  #
                  # 典型场景（用户实测）：161 集里第 11 集报
                  #   "Error applying bitstream filters to an output packet"
                  #   "Invalid data found when processing input"
                  # 视频流 copy 时，源文件的码流若有损坏（或读取时出了岔子），
                  # 封装器就会直接拒绝 —— 前面 10 集白白跑完，整个任务却挂了。
                  #
                  # 正确做法：把这一个文件改成**重编码**再试一次。
                  # 解码器对损坏数据的容忍度远高于 bitstream filter，
                  # 重编码通常能跳过坏包把内容救回来。
                  # 代价只是这一集的画质，换来的是整个任务能跑完。
                  # ----------------------------------------------------------
                  if video_copy and not getattr(self, "_force_mkv", False):
                      # 先诊断，把真相写进日志（别再靠猜）
                      self.log(f"  ⚠ {info.name} 视频直通（TS）失败")
                      self._diagnose_source(info.path, info.name)
                      self.log("    改用 **MKV 中间容器** 重跑 —— "
                               "视频仍然是原样复制，零损失，"
                               "只是绕开 TS 的码流转换。")
                      self._force_mkv = True
                      _restart = True
                      break   # 整批改用 MKV 重来（直通很快，代价极小）
                  if video_copy:
                      self.log(f"  ⚠ {info.name} 视频直通失败（换 MKV 也不行）")
                      self.log("    正在改用**重编码**重试这一个文件 —— "
                               "只为救回这一集，其余文件仍保持直通。")
                      try:
                          if os.path.isfile(cache_path):
                              os.remove(cache_path)   # 清掉半截产物
                      except OSError:
                          pass
                      inter2 = "mkv" if getattr(self, "_force_mkv", False) \
                          else resolve_intermediate(
                              t.params.vcodec, t.params.intermediate)
                      cache_path2 = os.path.join(
                          os.path.dirname(os.path.abspath(info.path)),
                          cache_filename(t.id, idx, inter2))
                      self._cache_files.append(cache_path2)
                      cmd2 = build_transcode_cmd(
                          info.path, t.params, cache_path2, info,
                          duration=info.duration, ffmpeg=self.ffmpeg,
                          video_copy=False, audio_copy=audio_copy,
                          container=inter2)
                      self.log("  重试命令：" + " ".join(cmd2))
                      rc2 = run_ffmpeg(self.ffmpeg, cmd2, info.duration,
                                       on_progress=on_prog, on_log=self.log,
                                       cancel=self.cancel,
                                       span_start=cursor, span_end=end)
                      if rc2 == -1:
                          t.status = STATUS.CANCELED
                          self.log("任务已取消")
                          return
                      if rc2 == 0 and os.path.isfile(cache_path2):
                          self.log(f"  ✅ {info.name} 已通过重编码救回，任务继续")
                          cache_paths.append(cache_path2)
                          cursor = end
                          self.progress(end)
                          continue
                  t.status = STATUS.FAILED
                  t.error = self._tail_error(f"第 {idx + 1} 个文件转码失败：{info.name}")
                  self.log(t.error)
                  return
              cache_paths.append(cache_path)
              cursor = end
              self.progress(end)

        if len(cache_paths) < 1:
            return

        self.stage("拼接（copy）")
        self.log(f"开始拼接 {len(cache_paths)} 个缓存文件")
        list_path = os.path.join(os.path.dirname(os.path.abspath(cache_paths[0])),
                                 f".mp4merger_{t.id}_cachelist.txt")
        write_concat_list(cache_paths, list_path)
        self._final_list = list_path
        cmd = ["-f", "concat", "-safe", "0", "-protocol_whitelist", "file,crypto,data",
               "-i", list_path, "-map", "0:v:0?", "-map", "0:a:0?", "-c", "copy",
               "-fflags", "+genpts"]
        # +faststart 会把 moov 索引挪到文件头（利于边下边播），
        # 但代价是写完后再把整个文件重写一遍 —— 大文件这一步要好几十秒，
        # 且期间没有任何进度输出（用户看到的就是"卡在 99%"）。
        # 本地播放根本不需要它，所以做成可关的。
        if getattr(t.output, "faststart", True):
            cmd += ["-movflags", "+faststart"]
        # MPEG-TS 里的音频是 ADTS 封装的 AAC，转成 MP4 必须过 adtstoasc。
        # 但如果音频不是 AAC（比如选了 MP3 编码器），加这个过滤器会直接失败：
        #   Error initializing bitstream filter: aac_adtstoasc
        # 所以要按实际音频编码器决定，不能无脑加。
        inter = resolve_intermediate(t.params.vcodec, t.params.intermediate)
        # 实际用了 MKV 中间容器时**不能**再加 adtstoasc：
        # MKV 里的 AAC 是 ASC（裸帧）格式，不是 ADTS，
        # 硬加这个 filter 会失败（实测已确认）。
        # 以真实产出的缓存文件扩展名为准，别只看参数。
        _real = None
        if cache_paths:
            _ext = os.path.splitext(cache_paths[0])[1].lower()
            _real = "ts" if _ext == ".ts" else ("mkv" if _ext == ".mkv" else None)
        if _real is not None:
            inter = _real
        if inter == "ts" and (t.params.acodec or "").lower() in AAC_LIKE:
            cmd += ["-bsf:a", "aac_adtstoasc"]
        cmd += [out_path]

        def on_prog2(v, p):
            self.progress(v, p.speed)
            self.task.eta = p.eta_text(t.total_duration)

        # 拼接的数据流写完之后，ffmpeg 还要把 moov 索引从文件尾挪到文件头
        #（+faststart），这一步等于把整个文件重写一遍，而且**不输出任何进度**。
        # 用户看到的正是"进度停在 99% 不动" —— 其实软件一直在干活。
        # 这里切到一个明确的收尾状态，并说明还要多久量级，别让人以为卡死。
        def on_finishing2():
            self.stage("收尾：整理文件索引")
            self.log("视频数据已写完，正在整理文件索引（faststart）…")
            try:
                mb = os.path.getsize(out_path) / (1024 * 1024)
                self.log(f"输出文件约 {mb:,.0f} MB，整理索引需要重写一遍文件，"
                         f"文件越大越久（通常几十秒以内），这一步没有进度可显示。")
            except OSError:
                pass

        rc = run_ffmpeg(self.ffmpeg, cmd, t.total_duration, on_progress=on_prog2,
                        on_log=self.log, cancel=self.cancel,
                        span_start=S_TRANSCODE_END,
                        span_end=S_CONCAT_END if t.output.extract_audio else S_AUDIO_END,
                        on_finishing=on_finishing2)
        if rc == -1:
            t.status = STATUS.CANCELED
            self.log("任务已取消")
        elif rc != 0:
            t.status = STATUS.FAILED
            t.error = self._tail_error("拼接失败")
            self.log(t.error)

    # ------------------------------------------------------------------
    def _do_extract_audio(self, video_path: str) -> None:
        t = self.task
        has_audio = any(f.has_audio for f in t.files)
        if not has_audio:
            self.log("源视频没有音轨，跳过提取音频")
            return
        self.stage("提取音频")
        a_dir = t.output.audio_dir or t.output.out_dir or t.source_dir
        os.makedirs(a_dir, exist_ok=True)
        name = t.output.audio_name or t.output.out_name
        a_path = unique_path(os.path.join(a_dir, f"{name}.{t.output.audio_format}"))
        src_acodec = t.files[0].a_codec if t.mode == "lossless" else t.params.acodec
        self.log(f"提取音频 → {a_path}")
        cmd = build_extract_audio_cmd(video_path, a_path, t.output.audio_codec,
                                      t.output.audio_bitrate_kbps, src_acodec, t.output.audio_format,
                                      getattr(t.params, "aac_profile", "aac_low"),
                                      faststart=getattr(t.output, "faststart", True))

        def on_prog(v, p):
            self.progress(v, p.speed)

        rc = run_ffmpeg(self.ffmpeg, cmd, t.total_duration, on_progress=on_prog,
                        on_log=self.log, cancel=self.cancel,
                        span_start=S_CONCAT_END, span_end=S_AUDIO_END)
        if rc == -1:
            t.status = STATUS.CANCELED
            self.log("任务已取消（提取音频阶段）")
        elif rc != 0:
            t.error = self._tail_error("提取音频失败")
            t.status = STATUS.FAILED
            self.log(t.error)
        else:
            t.audio_path = a_path
            self.log(f"音频已导出：{a_path}")

    # ------------------------------------------------------------------
    def _wait_if_paused(self) -> None:
        while self.should_pause() and not self.cancel.cancelled:
            self.task.status = STATUS.PAUSED
            self.cb.on_status(STATUS.PAUSED)
            self.stage("已暂停（等待继续）")
            time.sleep(0.3)
        if self.task.status == STATUS.PAUSED:
            self.task.status = STATUS.RUNNING
            self.cb.on_status(STATUS.RUNNING)

    # ------------------------------------------------------------------
    def cleanup(self) -> None:
        """删除缓存文件与列表文件（无论成功失败）。"""
        removed = 0
        for p in list(self._cache_files):
            try:
                if os.path.isfile(p):
                    os.remove(p)
                    removed += 1
            except OSError as exc:
                self.log(f"缓存文件删除失败：{p}（{exc}）")
        self._cache_files.clear()
        for p in (self._final_list,):
            if p and os.path.isfile(p):
                try:
                    os.remove(p)
                except OSError:
                    pass
        self._final_list = ""
        if removed:
            self.log(f"已清理 {removed} 个缓存文件")

    def _tail_error(self, prefix: str) -> str:
        """从日志尾部抽取 ffmpeg 的报错行，拼一句人话错误。"""
        keys = ("error", "invalid", "failed", "unable", "no such", "not supported",
                "could not", "cannot", "incorrect", "denied")
        lines = [ln for ln in self.task.log_lines[-40:] if any(k in ln.lower() for k in keys)]
        detail = "；".join(lines[-2:]) if lines else "请查看任务日志"
        return f"{prefix}：{detail}"
