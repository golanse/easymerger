"""国际化（i18n）—— 中 / 英双语。

设计思路
--------
传统的 Qt 翻译要先把每个字符串包一层 `self.tr("...")`，
本项目 5000+ 行界面代码、几百处文案，逐行改容易漏、也容易改错。

这里改用**运行时扫描替换**：程序启动 / 切换语言时，递归遍历所有控件，
把 text / title / toolTip / placeholderText 等属性拿去查字典，命中就替换。
好处是**不用改任何一行业务代码**，加新界面也自动生效。

字典里没有的词会保持中文原样 —— 属于渐进式翻译，
后续只要往 STRINGS 里加条目即可，不需要动界面代码。
"""

from __future__ import annotations

import re

__all__ = ["LANGUAGES", "current_lang", "set_lang", "tr",
           "apply_translation", "load_lang_from_config", "save_lang_to_config"]

LANGUAGES = {"zh": "简体中文", "en": "English"}

_lang = "zh"

# ==================================================================
# 翻译字典：key = 中文原文，value = 英文
# ==================================================================
STRINGS: dict[str, str] = {

    # ---------- 主窗口：顶部工具条 ----------
    "导入文件夹": "Import Folder",
    "导入文件": "Import Files",
    "批量导入（每文件夹一个任务）": "Batch Import (one task per folder)",
    "含子文件夹": "Include Subfolders",
    "移除选中": "Remove Selected",
    "↑ 上移": "↑ Move Up",
    "↓ 下移": "↓ Move Down",
    "恢复自然排序": "Restore Natural Sort",
    "⚡ 一键对齐异类": "⚡ Align Mismatched",
    "清空列表": "Clear List",
    "ffmpeg 状态": "FFmpeg Status",
    "参数": "Parameters",
    "用此文件参数填充右侧编码参数": "Fill Encoding Params from This File",
    "编码参数": "Encoding",
    "输出设置": "Output",
    "+ 加入队列": "+ Add to Queue",
    "▶ 开始队列": "▶ Start Queue",
    "已清理": "Cleaned up ",
    "个已结束的任务": "finished task(s)",
    "⏸ 暂停": "⏸ Pause",
    "⏹ 停止": "⏹ Stop",
    "任务详情": "Task Details",
    "移除任务": "Remove Task",
    "清空已完成": "Clear Finished",
    "任务开始时自动打开详情": "Open details automatically when task starts",
    "就绪": "Ready",
    "删除": "Delete",
    "选择包含视频的文件夹": "Select a Folder Containing Videos",
    "没有找到视频": "No Videos Found",
    "选择视频文件": "Select Video Files",
    "导入失败": "Import Failed",
    "导入成功": "Import Succeeded",
    "缺少 ffprobe": "ffprobe Missing",
    "读取失败": "Read Failed",
    "列表为空": "List Is Empty",
    "请先导入视频文件。": "Please import video files first.",
    "队列为空": "Queue Is Empty",
    "队列开始处理…": "Queue started…",
    "继续处理": "Resume",
    "停止队列": "Stop Queue",
    "正在停止…": "Stopping…",
    "移除选中的任务（支持多选）。": "Remove selected tasks (multi-select supported).",
    "任务正在运行": "Task Is Running",
    "确定移除吗？": "Remove it?",
    "请选择任务": "Select a Task",
    "先在队列里选中一个任务。": "Select a task in the queue first.",
    "双击某一行可直接打开该任务。": "Double-click a row to open that task.",
    "队列完成": "Queue Finished",
    "导出": "Export",
    "打开输出文件夹": "Open Output Folder",

    # ---------- 文件列表表头 ----------
    "文件名": "File Name",
    "时长": "Duration",
    "分辨率": "Resolution",
    "视频编码": "Video Codec",
    "像素格式": "Pixel Format",
    "帧率": "Frame Rate",
    "视频码率": "Video Bitrate",
    "旋转": "Rotation",
    "音频编码": "Audio Codec",
    "音频 Profile": "Audio Profile",
    "采样率": "Sample Rate",
    "声道": "Channels",
    "音频码率": "Audio Bitrate",
    "整体码率": "Overall Bitrate",
    "大小": "Size",
    "看着不一样": "Looks Different",

    # ---------- 任务队列表头 ----------
    "任务名": "Task",
    "文件数": "Files",
    "总时长": "Total Duration",
    "合并方式": "Merge Mode",
    "进度": "Progress",
    "输出文件": "Output File",
    "任务状态": "Task Status",

    # ---------- 编码参数面板 ----------
    "视频编码参数（转码时使用）": "Video Encoding (used when transcoding)",
    "视频编码器：": "Video Encoder:",
    "优先使用硬件编码（GPU）": "Prefer Hardware Encoding (GPU)",
    "优先使用的 GPU：": "Preferred GPU:",
    "一键选最优": "Pick Best",
    "硬件编码：未检测": "HW Encoding: Not Detected",
    "选最快可用": "Pick Fastest",
    "诊断": "Diagnose",
    "优先硬件": "Prefer HW",
    "编码速度 Preset：": "Encoding Preset:",
    "CRF 恒定质量": "CRF (Constant Quality)",
    "固定码率": "Constant Bitrate",
    "码率控制：": "Rate Control:",
    "分辨率：": "Resolution:",
    "保持原始分辨率": "Keep Original Resolution",
    "自定义分辨率": "Custom Resolution",
    "黑边填充": "Pad with Black Bars",
    "宽高联动": "Link Width & Height",
    "帧率：": "Frame Rate:",
    "保持原始帧率": "Keep Original Frame Rate",
    "统一为": "Unify to",
    "保留旋转标记（推荐，不重新编码画面）": "Keep rotation flag (recommended, no re-encode)",
    "烧录旋转（画面转正，需重编码）": "Burn in rotation (upright picture, requires re-encode)",
    "旋转处理：": "Rotation:",
    "视频附加参数：": "Extra Video Options:",
    "音频编码参数（转码时使用）": "Audio Encoding (used when transcoding)",
    "音频编码器：": "Audio Encoder:",
    "优先使用 AAC-LC": "Prefer AAC-LC",
    "音频码率：": "Audio Bitrate:",
    "保持原始采样率": "Keep Original Sample Rate",
    "保持原始声道": "Keep Original Channels",
    "1（单声道）": "1 (Mono)",
    "2（立体声）": "2 (Stereo)",
    "6（5.1）": "6 (5.1)",
    "像素格式：": "Pixel Format:",
    "采样率：": "Sample Rate:",
    "声道：": "Channels:",
    # 注意：界面上这个标签原文就写作 "AAC Profile："（不是「音频 Profile」），
    # 之前漏收，导致英文模式下它保持原样不翻。
    "AAC Profile：": "AAC Profile:",
    "智能推荐参数": "Smart Recommended Params",
    "检查是否支持无损合并（先比对参数，一致则直接 -c copy 无损合并，不一致才转码）":
        "Check lossless merge (compare params first; identical → -c copy, else transcode)",
    "音频附加参数：": "Extra Audio Options:",
    "高级": "Advanced",
    "中间缓存容器：": "Intermediate Container:",
    "恢复默认参数": "Restore Defaults",
    "恢复默认": "Restore Defaults",
    "检测": "Detect",
    "检测中…": "Detecting…",
    "正在检测": "Detecting",
    "无法检测": "Cannot Detect",
    "检测硬件编码": "Detect HW Encoding",
    "可用": "Available",
    "没有可用的": "None Available",
    "不支持": "Not Supported",
    "已改用 AAC-LC": "Fell back to AAC-LC",
    "建议改回 AAC-LC": "AAC-LC is recommended",
    "如果你使用的nonfree ffmpeg请在音频编码器选择libfdk_aac即可，如果你使用的是官方ffmpeg，建议改回AAC-LC": "If you use a nonfree FFmpeg build, just select libfdk_aac as the audio encoder; if you use the official FFmpeg build, AAC-LC is recommended.",
    "需要转换的文件（{n} 个）": "Files to convert ({n})",
    "自定义": "Custom",
    "自动（默认优先级）": "Auto (default priority)",

    # ---------- Preset ----------
    "最快，体积大": "Fastest, larger file",
    "很快": "Very fast",
    "快": "Fast",
    "较快": "Faster",
    "均衡（默认）": "Balanced (default)",
    "较慢，压缩更好": "Slower, better compression",
    "慢": "Slow",
    "最慢，体积最小": "Slowest, smallest file",

    # ---------- 输出面板 ----------
    "输出文件夹：": "Output Folder:",
    "浏览…": "Browse…",
    "选择…": "Choose…",
    "输出文件名：": "Output File Name:",
    "输出视频": "Output Video",
    "合并输出": "Merge Output",
    "优化在线播放": "Optimize for Streaming",
    "是（收尾需重写文件）": "Yes (rewrites file at the end)",
    "否（更快完成）": "No (finishes faster)",
    "保留任务日志": "Keep Task Log",
    "是（输出目录 .log）": "Yes (.log in output folder)",
    "否": "No",
    "合并后提取音频（可选）": "Extract Audio After Merge (optional)",
    "音频输出文件夹：": "Audio Output Folder:",
    "音频文件名：": "Audio File Name:",
    "与视频同名": "Same as Video",
    "格式": "Format",
    "编码": "Codec",
    "重编码码率": "Re-encode Bitrate",
    "AAC 重新编码": "Re-encode to AAC",
    "MP3 重新编码": "Re-encode to MP3",
    "直接复制原音轨（copy）": "Copy original audio track",
    "选择输出文件夹": "Select Output Folder",
    "请选择输出文件夹": "Please select an output folder",

    # ---------- 任务状态 ----------
    "等待中": "Pending",
    "进行中": "Running",
    "已暂停": "Paused",
    "已完成": "Finished",
    "准备": "Preparing",
    "收尾": "Finalizing",
    "收尾中": "Finalizing",
    "完成": "Done",
    "成功": "Success",
    "失败": "Failed",
    "错误": "Error",
    "已取消": "Cancelled",
    "任务完成": "Task Finished",
    "任务已取消": "Task Cancelled",
    "合并失败": "Merge Failed",
    "取消此任务": "Cancel This Task",
    "⏵ 继续": "⏵ Resume",
    "请查看任务日志": "See the task log for details",

    # ---------- 合并方式 ----------
    "待检测": "Not Detected",
    "无损合并（-c copy）": "Lossless Merge (-c copy)",
    "无损合并": "Lossless Merge",
    "转码合并": "Transcode Merge",
    "转码合并（单遍，无中间文件）": "Transcode Merge (single pass)",
    "智能直通": "Smart Passthrough",
    "需转码合并": "Transcode Merge Required",
    "参数完全一致": "All Parameters Identical",
    "参数不一致": "Parameters Differ",
    "视频零损失": "Zero Video Quality Loss",

    # ---------- 任务详情 ----------
    # 带序号/图标前缀的标题（"① 源视频参数"、"⚙ 软件自动调整"）在下面
    # 由 _strip_prefix 剥离前缀后查这几条，字典里不必为每种前缀各存一份。
    "源视频参数": "Source Video Parameters",
    "编码设置参数": "Encoding Parameters",
    "运行日志": "Run Log",
    "软件自动调整": "Automatic Adjustments",
    "编码设置参数（本次任务快照）": "Encoding Parameters (snapshot of this task)",
    "任务名称": "Task Name",
    "当前阶段": "Current Stage",
    "总进度": "Overall Progress",
    "所有文件参数一致（时长/码率/大小不同属正常）。": (
        "All files share identical parameters "
        "(differences in duration / bitrate / size are normal)."),
    "个文件，总时长": " files, total duration ",
    "，总大小": ", total size ",
    "   剩余 ": "   remaining ",
    # 差异计数里的量词："分辨率（5 处）"。整串数字是运行时拼的，
    # 只能把"处"单独翻。单字只在整串完全等于"处"时才命中，不会误伤"处理"等词。
    "处": "occurrences",

    # ---------- 任务详情 ----------
    "任务名称：": "Task Name:",
    "状态：": "Status:",
    "当前阶段：": "Current Stage:",
    "文件：": "Files:",
    "总进度：": "Overall Progress:",
    "速度 / 剩余：": "Speed / Remaining:",
    "① 源视频参数": "① Source Parameters",
    "② 编码设置参数": "② Encoding Parameters",
    "③ 运行日志": "③ Run Log",
    "兼容性检测": "Compatibility Check",
    "结果": "Result",
    "结论": "Conclusion",
    "参数是否一致": "Parameters Consistent?",
    "涉及文件数": "Affected Files",
    "检测基准文件": "Baseline File",
    "基准文件": "Baseline File",
    "基准行": "Baseline Row",
    "当前行": "Current Row",
    "只看不一致的文件": "Show Mismatched Only",
    "只看不一致的": "Mismatched Only",
    "差异在：": "Difference in: ",
    "编码设置参数（本次任务快照）": "Encoding Parameters (snapshot of this task)",
    "视频流": "Video Stream",
    "音频流": "Audio Stream",
    "直接 copy（不重编码）": "Direct copy (no re-encode)",
    "直接 copy": "Direct copy",
    "重编码": "Re-encode",
    "合并策略": "Merge Strategy",
    "必须转码，无法无损合并。": "Transcoding required; lossless merge impossible.",
    "未执行（未勾选无损检测）": "Not run (lossless check disabled)",

    # ---------- 对齐对话框 ----------
    "对齐异类文件": "Align Mismatched Files",
    "一键对齐异类文件。": "Align mismatched files in one click.",
    "哪里不一样": "What Differs",
    "状态": "Status",
    "待转换": "Pending",
    "转换中…": "Converting…",
    "✅ 完成": "✅ Done",
    "❌ 失败": "❌ Failed",
    "转换失败": "Conversion Failed",
    "转换完成": "Conversion Finished",
    "转换完成并已校验": "Converted and Verified",
    "转换完成，但没能对齐": "Converted, but not aligned",
    "开始转换": "Start Conversion",
    "取消": "Cancel",
    "关闭": "Close",
    "🔄 刷新参数": "🔄 Refresh",
    "转换后替换源文件": "Replace Source Files After Conversion",
    "转换后的文件放哪里": "Where to Save Converted Files",
    "已对齐的文件": "aligned_files",
    "确认替换原文件": "Confirm Replace Original",
    "我已备份，继续": "I have a backup, continue",
    "仍然转换": "Convert Anyway",
    "提示": "Tip",
    "转完之后整批能无损合并": "After conversion the whole batch can be merged losslessly",
    "原文件没有被改动。": "Original files were not modified.",
    "正在取消…": "Cancelling…",
    "无法刷新": "Cannot Refresh",
    "对齐": "Align",
    "对齐异类": "Align Mismatched",
    "只转换": "Convert only",

    # ---------- ffmpeg 对话框 ----------
    "手动选择 ffmpeg": "Manually Select ffmpeg",
    "手动选择 ffprobe": "Manually Select ffprobe",
    "从其他软件借用 ffmpeg": "Borrow ffmpeg from Another App",
    "硬件编码深度诊断…": "Deep HW Encoding Diagnostics…",
    "🔍 一键自检…": "🔍 Self-Check…",
    "用这个文件替换组件…": "Replace Component With This File…",
    "选择用哪个 ffmpeg…": "Choose Which ffmpeg to Use…",
    "磁盘占用 / 清理…": "Disk Usage / Cleanup…",
    "打开下载临时目录": "Open Download Temp Folder",
    "打开组件目录": "Open Component Folder",
    "获取 / 切换": "Get / Switch",
    "诊断 / 排查": "Diagnose / Troubleshoot",
    "版本": "Version",
    "（未找到）": "(not found)",
    "软件目录": "App directory",
    "组件目录": "Component directory",
    "自动下载会安装到": "Auto-download installs to",
    "所在盘可用空间": "Free space on drive",
    "下载临时目录": "Download temp directory",
    "可用下载源": "Available download sources",
    # ---------- 提示卡片（HintCard）：按句子存，不再存整段 HTML ----------
    # 以前把整段富文本（含颜色）当 key，切主题后颜色变了就再也匹配不上，
    # 于是英文界面里提示卡片仍是中文。改成按句子存即可稳定命中。
    "未勾选时，所有任务都会按下面的编码参数转码后再合并。":
        "When unchecked, every task is transcoded with the encoding parameters below before merging.",
    "转码合并时会在【源文件所在文件夹】生成临时缓存文件，合并完成后自动删除":
        "When transcoding, temporary cache files are created in the [source folder] and deleted automatically after merging",
    "输出与缓存都在本地磁盘，不占用系统临时目录":
        "Both output and cache stay on your local disk; the system temp folder is not used",
    "注意：AV1 码流装进 MPEG-TS 会退化成 bin_data 导致丢流，选择 AV1 编码时会自动改用 MKV 中间容器。":
        "Note: an AV1 stream inside MPEG-TS degrades to bin_data and gets dropped; choosing AV1 automatically switches the intermediate container to MKV.",
    "速度快、CPU 占用低；同码率下画质略逊于 libx264":
        "Fast with low CPU usage; slightly lower quality than libx264 at the same bitrate",
    "软件刚启动，还没拿到 ffmpeg 路径":
        "Just started; the ffmpeg path is not available yet",
    "就绪后会自动实测，无需手动操作":
        "It will be tested automatically once ready - no manual action needed",
    "编码与解码是两回事：":
        "Encoding and decoding are two different things:",
    "· 编码 HE-AAC 需要 libfdk_aac，ffmpeg 自带的 aac 编码器做不了，会产出标准不支持的坏码流 —— 文件能生成，但没声音；":
        "· Encoding HE-AAC requires libfdk_aac; ffmpeg's built-in aac encoder cannot do it and produces a non-standard broken stream - the file is created but has no sound;",
    "· 解码 HE-AAC 不需要任何额外组件，原生解码器自带 SBR 支持，所以「把现成的 HE-AAC 素材转成 AAC-LC」完全没问题。":
        "· Decoding HE-AAC needs no extra component; the native decoder has built-in SBR support, so converting existing HE-AAC material to AAC-LC is perfectly fine.",
    "要输出 HE-AAC：换用带 libfdk_aac 的 ffmpeg 构建（BtbN nonfree）。":
        "To output HE-AAC: switch to an ffmpeg build with libfdk_aac (BtbN nonfree).",
    "AV1 硬件编码": "AV1 hardware encoding",
    "速度快，但需要较新的显卡（RTX 40 系 / Intel 锐炫 / RX 6000 系以上）":
        "Fast, but requires a fairly new GPU (RTX 40 series / Intel Arc / RX 6000 series or newer)",
    "不支持的显卡会直接失败，建议先点「检测硬件编码」确认":
        "Unsupported GPUs fail outright; click \u201cDetect Hardware Encoding\u201d first to confirm",
    "中间容器已自动切换为 MKV（AV1 装进 MPEG-TS 会导致视频流丢失）":
        "The intermediate container has been switched to MKV automatically (AV1 in MPEG-TS causes video stream loss)",
    "AV1 软件编码：压缩率最高，但速度慢很多":
        "AV1 software encoding: highest compression, but much slower",
    "比 H.264 省约 30~50% 体积":
        "About 30-50% smaller than H.264",
    "长视频建议先用「较快的 Preset」试一小段，确认能接受再全量转":
        "For long videos, try a short clip with a faster preset first, then encode everything if acceptable",
    "点右侧「检测硬件编码」实测一下，就能确定能不能用。":
        "Click \u201cDetect Hardware Encoding\u201d on the right to test it and find out whether it works.",
    "展开排查步骤": "Show troubleshooting steps",
    "路径": "Path",
    "来源": "Source",
    "硬件编码器": "HW Encoders",
    "● 在用": "● In Use",
    "用选中的这个": "Use Selected One",
    "已切换": "Switched",
    "已生效": "Applied",
    "当前占用：": "Current Usage:",
    "磁盘占用": "Disk Usage",
    "清理残留临时文件": "Clean Leftover Temp Files",
    "清理完成": "Cleanup Finished",
    "确认删除": "Confirm Delete",
    "已删除": "Deleted",
    "无法诊断": "Cannot Diagnose",
    "诊断中…": "Diagnosing…",
    "测试中…": "Testing…",
    "测试结果": "Test Result",
    "扫描中…": "Scanning…",
    "扫描失败": "Scan Failed",
    "没找到": "Not Found",
    "没有找到": "Not Found",
    "ffmpeg 下载源": "ffmpeg Download Sources",
    "安装完成 ✔": "Installed ✔",
    "安装完成": "Installed",
    "下载失败": "Download Failed",
    "复制": "Copy",
    "复制全部": "Copy All",
    "已复制 ✔": "Copied ✔",
    "已复制 ✓": "Copied ✓",
    "正在解压…": "Extracting…",
    "没有可用的 ffmpeg": "No ffmpeg Available",
    "还没有可用的 ffmpeg。": "No ffmpeg available yet.",
    "还没有可用的 ffmpeg，请先安装 ffmpeg 组件。": "No ffmpeg available. Please install the ffmpeg component first.",
    "请选择要使用的 ffmpeg": "Select the ffmpeg to Use",
    "请手动指定 ffmpeg": "Please specify ffmpeg manually",
    "ffmpeg 路径无效": "Invalid ffmpeg Path",
    "ffmpeg 未就绪": "ffmpeg Not Ready",
    "等待 ffmpeg 就绪": "Waiting for ffmpeg",
    "未找到 ffmpeg": "ffmpeg Not Found",
    "未找到 ffprobe": "ffprobe Not Found",
    "正在检查…": "Checking…",
    "一键自检报告": "Self-Check Report",
    "编码器检测诊断": "Encoder Detection Diagnostics",
    "硬件编码检测结果": "HW Encoding Detection Result",
    "AMF 深度诊断": "AMF Deep Diagnostics",
    "当前使用": "In Use",
    "软件自带": "Bundled",
    "系统 PATH": "System PATH",
    "常见位置": "Common Locations",
    "手动指定": "Manual",
    "环境变量": "Environment Variable",
    "已保存配置": "Configuration Saved",
    "没有复制到任何文件": "No Files Copied",
    "磁盘空间可能不足": "Disk Space May Be Insufficient",
    "无法创建目录": "Cannot Create Directory",
    "所有文件 (*)": "All Files (*)",

    # ---------- 通用 ----------
    "确定": "OK",
    "是": "Yes",
    "项目": "Item",
    "值": "Value",
    "硬件（GPU）": "Hardware (GPU)",
    "软件（CPU）": "Software (CPU)",
    "高级选项（一般不用改）": "Advanced Options (rarely needed)",
    "打开文件夹": "Open Folder",
    "返回": "Back",
    "无": "None",
    "未知": "Unknown",
    "未知错误": "Unknown Error",
    "导入中…": "Importing…",
    "读取中…": "Reading…",
    "保持原始": "Keep Original",
    "最大/最高": "Max / Highest",
    "不可用": "Unavailable",
    "能用": "Usable",
    "存在": "Present",
    "未编译": "Not Compiled",
    "未测": "Not Tested",
    "✔ 可用": "✔ Available",
    "✘ 不可用": "✘ Unavailable",
    "? 未检测": "? Not Detected",
    "AAC Profile 检测": "AAC Profile Detection",
    "能不能编码": "Can Encode?",
    "能不能解码": "Can Decode?",
    "编码失败": "Encoding Failed",
    "解码失败": "Decoding Failed",
    "不支持 HE-AAC": "HE-AAC Not Supported",
    "最强的 AAC 编码器": "Best AAC Encoder",
    "CPU 软编": "CPU (software)",
    "CPU 软编可用：": "CPU software encoding available:",

    # ---------- 菜单（新增） ----------
    "文件": "File",
    "视图": "View",
    "帮助": "Help",
    "语言": "Language",
    "主题": "Theme",
    "浅色": "Light",
    "深色": "Dark",
    "跟随系统": "Follow System",
    "关于": "About",
    "关于 EasyMerger": "About EasyMerger",
    "检查更新": "Check for Updates",
    "打开项目主页": "Open Project Homepage",
    "退出": "Exit",

    # ---------- 语言标签页 ----------
    "界面语言": "Interface Language",
    "选择界面显示语言，切换后立即生效。\n首次启动会自动跟随系统语言，之后以你的选择为准。": "Choose the interface language; the change takes effect immediately.\nOn first launch the app follows your system language; afterwards your own choice is remembered.",
    "当前系统语言：": "Detected system language: ",

    # ---------- 第三批：动态句的静态片段 ----------
    "  （0 无损 / 23 默认 / 51 最差）": "  (0 lossless / 23 default / 51 worst)",
    "当前列表：": "Current list: ",
    " 个文件，总时长 ": " files, total duration ",
    "，总大小 ": ", total size ",
    "   剩余 ": "   remaining ",
    "（来源：": " (source: ",
    "✔ ffmpeg 就绪": "✔ ffmpeg ready",
    "ffmpeg 无法执行，请检查文件是否完整。": "ffmpeg cannot be executed; please check whether the file is intact.",
    "找到 ffmpeg 但缺少 ffprobe：": "Found ffmpeg, but ffprobe is missing: ",
    "未找到 ffmpeg。请点击「安装/定位 ffmpeg」下载静态构建，或手动选择 ffmpeg.exe。": "ffmpeg not found. Click「Install / Locate ffmpeg」to download a static build, or choose ffmpeg.exe manually.",
    "软件实际使用的 ffmpeg：": "ffmpeg actually in use:",
    "如果你想换成另一个 ffmpeg，点这里 →「手动选择 ffmpeg」或「从其他软件借用 ffmpeg」。": "To switch to another ffmpeg, click here →「Choose ffmpeg manually」or 「Borrow ffmpeg from another app」.",
    "下载官方静态构建并安装到本软件的组件目录：": "Download the official static build and install it into this app's component folder:",
    "约 100 MB（压缩包），解压后约 250 MB。\n如果组件目录里已经有 ffmpeg.exe，会被覆盖。": "About 100 MB (archive), roughly 250 MB once extracted.\nIf ffmpeg.exe already exists in the component folder, it will be overwritten.",
    "下载 ffmpeg 时，压缩包先落到这里再解压。": "When downloading ffmpeg, the archive lands here first and is extracted afterwards.",
    "下载成功会自动清空；失败则保留，可在这里手动删除。": "Cleared automatically on success; kept on failure so you can delete it here manually.",
    "对齐异类文件": "Align Mismatched Files",

    # ---------- 第二批：全量扫描补齐（长提示 / 下拉项 / 占位符） ----------
    "简体中文": "Simplified Chinese",
    "默认输出文件夹 = 源文件所在文件夹；默认文件名 = 该文件夹名。两者都可自定义。\n若输出文件已存在，会自动在末尾追加 _1、_2 … 不会覆盖原文件。": "Default output folder = the source folder; default filename = that folder's name. Both can be customized.\nIf the output file already exists, _1, _2 … will be appended — existing files are never overwritten.",
    "优化在线播放（faststart）": "Optimize for online playback (faststart)",
    "<span style='color:#1a66d8;font-weight:600;'>ℹ</span>&nbsp; <b style='color:#202124;'>已开启：收尾时会重写整个文件</b>": "<span style='color:#1a66d8;font-weight:600;'>ℹ</span>&nbsp; <b style='color:#202124;'>Enabled: the whole file is rewritten at the end</b>",
    "<p style='margin:0 0 2px 0; padding-left:15px; text-indent:-15px;'><span style='color:#1a66d8;'>•</span>&nbsp; 好处：网页播放 / 边下边播可以立刻开始</p><p style='margin:0 0 2px 0; padding-left:15px; text-indent:-15px;'><span style='color:#1a66d8;'>•</span>&nbsp; 代价：写完后再重写一遍，大文件会停在最后一步（日志里会显示「正在整理文件索引」）</p>": "<p style='margin:0 0 2px 0; padding-left:15px; text-indent:-15px;'><span style='color:#1a66d8;'>•</span>&nbsp; Benefit: web playback / progressive download can start immediately</p><p style='margin:0 0 2px 0; padding-left:15px; text-indent:-15px;'><span style='color:#1a66d8;'>•</span>&nbsp; Cost: the file is written once more afterwards, so large files appear to stall on the last step (the log shows 「Building file index」)</p>",
    "默认在视频输出文件夹生成「输出文件名.m4a」。可自行改位置和文件名。": "By default, \"output filename.m4a\" is created in the video output folder. You can change both the location and the filename.",
    "<span style='color:#1a66d8;font-weight:600;'>ℹ</span>&nbsp; <b style='color:#202124;'>未勾选时，所有任务都会按下面的编码参数转码后再合并。</b>": "<span style='color:#1a66d8;font-weight:600;'>ℹ</span>&nbsp; <b style='color:#202124;'>When unchecked, every task is transcoded with the parameters below before merging.</b>",
    "<span style='color:#1a66d8;font-weight:600;'>ℹ</span>&nbsp; <b style='color:#202124;'>关于临时缓存文件</b>": "<span style='color:#1a66d8;font-weight:600;'>ℹ</span>&nbsp; <b style='color:#202124;'>About temporary cache files</b>",
    "<p style='margin:0 0 2px 0; padding-left:15px; text-indent:-15px;'><span style='color:#1a66d8;'>•</span>&nbsp; 转码合并时会在【源文件所在文件夹】生成临时缓存文件，合并完成后自动删除</p><p style='margin:0 0 2px 0; padding-left:15px; text-indent:-15px;'><span style='color:#1a66d8;'>•</span>&nbsp; 输出与缓存都在本地磁盘，不占用系统临时目录</p>": "<p style='margin:0 0 2px 0; padding-left:15px; text-indent:-15px;'><span style='color:#1a66d8;'>•</span>&nbsp; When transcoding, temporary cache files are created in the 【source folder】 and deleted automatically after merging</p><p style='margin:0 0 2px 0; padding-left:15px; text-indent:-15px;'><span style='color:#1a66d8;'>•</span>&nbsp; Both the output and the cache stay on your local disk; the system temp folder is not used</p>",
    " AV1 的特别处理": " Special handling for AV1",
    "删除": "Delete",
    "无损拼接 / 转码拼接 / 批量排队": "Lossless / transcode / batch queue",
    "至少需要 2 个文件才能判断哪个是异类": "At least 2 files are needed to tell which one is the odd one out",
    "<b style=\"color:#888\">等待中</b>": "<b style=\"color:#888\">Pending</b>",
    "合并方式：": "Merge mode:",
    "所有文件参数一致（时长/码率/大小不同属正常）。": "All files share identical parameters (differences in duration / bitrate / size are normal).",
    "标<span style='background:#fff4c2;'>&nbsp;黄&nbsp;</span>的单元格＝与第一个文件不同且<b>影响合并</b>；标<span style='background:#e8f0fe;'>&nbsp;蓝&nbsp;</span>的＝不同但<b>不影响</b>（编码器自动算出，无法手动改）。\n时长 / 码率 / 文件大小本来就各不相同，未标色，也<b>不影响</b>合并。": "Cells marked <span style=\"background:#fff4c2;\">&nbsp;yellow&nbsp;</span> differ from the first file <b>and affect merging</b>; those marked <span style=\"background:#e8f0fe;\">&nbsp;blue&nbsp;</span> differ but <b>do not affect it</b> (computed by the encoder, cannot be set manually).\nDuration / bitrate / file size naturally differ per file, so they are not highlighted and <b>do not affect</b> merging.",
    "自动下载 ffmpeg（推荐）": "Download ffmpeg automatically (recommended)",
    "导入本地 ffmpeg（复制到软件）": "Import a local ffmpeg (copy into the app)",
    "测试某个 ffmpeg.exe…": "Test a specific ffmpeg.exe…",
    "官方下载链接（含硬件编码）": "Official download links (with hardware encoding)",
    "软件会按以下顺序查找 ffmpeg：手动指定 → 环境变量 MP4MERGER_FFMPEG → 保存的配置 → 软件自带目录 vendor/ffmpeg/bin → 系统 PATH。\n建议点「自动下载」，组件会装到软件目录里，重装系统/换电脑都不用到处找。": "The app looks for ffmpeg in this order: manually specified → environment variable MP4MERGER_FFMPEG → saved configuration → the bundled vendor/ffmpeg/bin folder → system PATH.\n「Download automatically」is recommended: the component is installed into the app folder, so you never have to hunt for it after reinstalling Windows or switching PCs.",
    "任务队列（按加入顺序依次处理）": "Task Queue (processed in the order added)",
    "选中文件的参数（用于参考设置转码参数）": "Selected File's Parameters (reference for encoding settings)",
    "⚙ 软件自动调整": "⚙ Automatic Adjustments",
    "运行环境": "Environment",
    "许可证": "License",
    "致谢": "Credits",
    "简体中文": "Simplified Chinese",
    "直接复制原音轨（copy，最快、无损失）": "Copy original audio track (fastest, lossless)",
    "ultrafast（最快，体积大）": "ultrafast (fastest, larger file)",
    "veryfast（很快）": "veryfast (very fast)",
    "faster（快）": "faster (fast)",
    "fast（较快）": "fast (fairly fast)",
    "medium（均衡（默认））": "medium (balanced, default)",
    "slow（较慢，压缩更好）": "slow (slower, better compression)",
    "slower（慢）": "slower (slow)",
    "veryslow（最慢，体积最小）": "veryslow (slowest, smallest file)",
    "MPEG-TS（推荐，拼接最稳）": "MPEG-TS (recommended, most robust for concatenation)",
    "分片 MP4（fragmented，编码器兼容性更好）": "Fragmented MP4 (better encoder compatibility)",
    "MKV（AV1 会自动切换到此项）": "MKV (AV1 switches to this automatically)",
    "默认：源文件所在文件夹": "Default: source folder",
    "默认：当前文件夹名": "Default: current folder name",
    "默认：与视频输出文件夹相同": "Default: same as video output folder",
    "默认：与视频输出文件名相同": "Default: same as video output filename",
    "可选，例如：-x264-params keyint=60:min-keyint=60": "Optional, e.g.: -x264-params keyint=60:min-keyint=60",
    "可选，例如：-af loudnorm": "Optional, e.g.: -af loudnorm",
    "简体中文": "Simplified Chinese",
    "选择一个文件夹，自动按文件名自然排序导入其中的 MP4 视频": "Pick a folder; the MP4 files in it are imported in natural filename order",
    "选择父文件夹，其下每个含视频的子文件夹会自动生成一个排队任务": "Pick a parent folder; each subfolder containing videos becomes a queued task",
    "有少数几个文件参数和大家不一样时，只转换这几个。\n转换后整批就能无损合并（几分钟、画质零损失），\n而不是把全部文件重新编码一遍。": "When only a few files differ from the rest, convert just those few.\nAfter that the whole batch can be merged losslessly (minutes, zero quality loss)\ninstead of re-encoding every single file.",
    "移除选中的任务。可以按住 Ctrl 点选多个、按住 Shift 选连续一段。": "Remove the selected tasks. Ctrl+click to select several, Shift+click for a range.",
    "任务结束后把完整运行日志存成 .log 文件，放在输出文件旁边。\n排查问题（为什么这么慢 / 为什么走了转码 / 哪一步失败）时很有用。\n默认关闭：多数情况用不到，而且会多出一个文件。": "Save the full run log to a .log file next to the output when the task finishes.\nUseful for troubleshooting (why it is slow / why it transcoded / which step failed).\nOff by default: rarely needed, and it creates an extra file.",
    "把播放索引挪到文件头，网页 / 边下边播能立刻开始。代价：写完文件后要把整个文件再重写一遍。": "Move the playback index to the front of the file so web playback / progressive download can start immediately. Cost: the whole file is rewritten after writing.",
    "勾选后，音频文件名自动跟随上面的输出文件名": "When checked, the audio filename automatically follows the output filename above",
    "勾选：自动比对所选视频的编码/分辨率/帧率/像素格式/音轨参数，全部一致时用 concat 解复用器无损合并（秒级完成、零画质损失）；\n不一致时自动按下方参数逐个转码为统一格式再合并。\n不勾选：无论参数是否一致，都按下方参数转码后合并。": "Checked: the app compares codec / resolution / frame rate / pixel format / audio parameters of the selected videos. If all match, files are merged losslessly with the concat demuxer (seconds, zero quality loss);\nif they differ, they are transcoded one by one to the parameters below and then merged.\nUnchecked: files are always transcoded with the parameters below, regardless of whether they match.",
    "勾选后，下面两处会自动挑选本机可用的硬件编码器：\n  ·「用此文件参数填充编码设置」\n  ·「⚡ 一键对齐异类」\n会按目标格式（H.264 / HEVC / AV1）依次尝试\nAMD AMF → NVIDIA NVENC → Intel QSV → Apple VideoToolbox → VAAPI。\n想指定某张卡，用下面「优先使用的 GPU」。\n\n取消勾选则一律用软件编码器（libx264 / libx265 / libsvtav1）：\n速度慢，但画质通常更好，也不占用显卡。": "When checked, the two places below pick a hardware encoder available on this machine:\n  ·「Fill Encoding Params from This File」\n  ·「⚡ Align Mismatched」\nThey try AMD AMF → NVIDIA NVENC → Intel QSV → Apple VideoToolbox → VAAPI based on the target format (H.264 / HEVC / AV1).\nTo force a specific card, use「Preferred GPU」below.\n\nUnchecked: always use software encoders (libx264 / libx265 / libsvtav1):\nslower, but usually better quality and no GPU load.",
    "电脑有多张显卡时才需要改（如 Intel 核显 + NVIDIA 独显）。\n  · 自动：按 AMD → NVIDIA → Intel → Apple 的顺序挑\n  · 指定厂商：优先用它，它不可用时自动退回下一家\n\nApple = VideoToolbox，macOS 上 Apple Silicon（M 系列）\n和 Intel Mac 的硬编都走它，是 Mac 上唯一通用的选项。\n\n实测编码一帧才知道某张卡是否真的可用（ffmpeg 列出来不代表能用），\n选错也不会失败 —— 只是退回到其他可用的编码器。": "Only needed if the PC has more than one GPU (e.g. Intel iGPU + NVIDIA discrete GPU).\n  · Auto: try AMD → NVIDIA → Intel → Apple in order\n  · Pick a vendor: prefer it, fall back to the next one if unavailable\n\nApple = VideoToolbox, the only universal hardware encoder on macOS (Apple Silicon M-series and Intel Macs).\n\nA card is only proven usable by actually encoding a frame (being listed by ffmpeg does not mean it works);\npicking the wrong one never fails — it just falls back to another available encoder.",
    "实际编码一帧来测试每个硬件编码器是否真的可用。\n注意：ffmpeg -encoders 里列出来不代表能用（没有对应显卡时会初始化失败）。": "Encode one frame for real to test whether each hardware encoder actually works.\nNote: being listed in ffmpeg -encoders does not mean it works (initialization fails without the matching GPU).",
    "在实测通过的编码器里挑一个最快的（优先硬件，其次 AV1，最后 H.264 软编）": "Pick the fastest among the encoders that passed the test (hardware first, then AV1, finally H.264 software)",
    "按源视频码率和本机可用的硬件编码器，给出一组「快 / 小 / 均衡」的参数。\n省得逐个试：不同预设的速度与体积差异往往和直觉相反。": "Suggest a set of「fast / small / balanced」parameters based on the source bitrate and the hardware encoders available.\nSaves you from testing one by one: the speed and size differences between presets are often counter-intuitive.",
    "当「一键自检说有 AMF、界面却说未编译」时点这里。\n它会复刻检测的每一步并把中间结果全部列出来，\n能一次定位是查询失败、解析失败还是用了别的文件。": "Click here when「self-check says AMF exists but the UI says not compiled」.\nIt replays every detection step and lists all intermediate results,\nso you can tell at once whether the query failed, parsing failed, or another file was used.",
    "选一个常用分辨率会同时把左边的模式切到「自定义分辨率」。\n手动改宽或高后，这里会自动回到「自定义」。": "Choosing a common resolution also switches the mode on the left to 「Custom Resolution」.\nAfter you edit width or height manually, this returns to「Custom」.",
    "勾选后，改宽则高按当前比例自动跟着变，改高同理，\n避免填出 1280×1080 这类变形的目标分辨率。\n取消勾选则宽高各填各的。": "When checked, editing the width updates the height to keep the current aspect ratio, and vice versa,\nso you cannot enter a distorted target like 1280×1080.\nUnchecked: width and height are edited independently.",
    "输出时源画面按原始比例缩放进目标框，多出来的部分补黑边。\n不勾选则直接拉伸到目标尺寸（画面可能变形）。\n注意：这只影响输出画面，不限制你在这里填的宽高。": "When outputting, the source picture is scaled into the target box at its original aspect ratio and the rest is filled with black bars.\nUnchecked: the picture is stretched straight to the target size (may look distorted).\nNote: this affects the output picture only, not the width/height you enter here.",
    "实测各 AAC Profile 能否产出可正常解码的音频。\n注意：ffmpeg 原生 aac 编码器的 HE-AAC 会产出无法解码的坏流，这类问题只有真编一遍再解一遍才会暴露。": "Test whether each AAC profile really produces decodable audio.\nNote: the native ffmpeg aac encoder produces a broken, undecodable stream for HE-AAC — only encoding and then decoding it again reveals this.",
    "影响「用此文件参数填充编码设置」和「批量导入」填哪个 Profile：\n  · 勾选（默认）→ 一律填 AAC-LC\n  · 取消勾选　　→ 如实传导源文件的 Profile\n　　　　　　　　　　（HE-AAC / HE-AACv2 等）\n\n为什么要默认勾选：AAC-LC 兼容性最好、任何 ffmpeg 都能输出。\n而 HE-AAC 需要 libfdk_aac，用原生 aac 编码器会产出\n**能生成但解不开的坏流** —— 不实测根本发现不了。\n\n你仍可以手动改上面的下拉框，改了会自动取消这个勾选\n（手动选择优先）。": "Affects which profile is filled by「Fill Encoding Params from This File」and batch import:\n  · Checked (default) → always AAC-LC\n  · Unchecked　　　　→ pass through the source file's real profile\n　　　　　　　　　　(HE-AAC / HE-AACv2 etc.)\n\nWhy checked by default: AAC-LC has the best compatibility and any ffmpeg can output it.\nHE-AAC needs libfdk_aac; with the native aac encoder you get\na **stream that is produced but cannot be decoded** — you would never notice without testing.\n\nYou can still change the dropdown above manually; doing so unchecks this box\n(manual choice wins).",
    "删除": "Delete",
    "勾上后只列出与第一个文件有差异的行，\n不用在长列表里来回翻。": "When checked, only rows that differ from the first file are listed,\nso you don't have to scroll back and forth through a long list.",
    "扫描电脑上 Shotcut / ShanaEncoder / 格式工厂 等软件自带的 ffmpeg。\n它们通常编译了完整的硬件编码支持（含 AMD 的 amf）。": "Scan for the ffmpeg bundled with Shotcut / ShanaEncoder / Format Factory and similar apps.\nThey usually ship with full hardware encoding support (including AMD's amf).",
    "把 Shotcut / ShanaEncoder 等软件自带的 ffmpeg 复制进本软件组件目录。\n会自动连带复制同目录的 av*.dll / sw*.dll 等运行库，并复制完立刻试运行验证。": "Copy the ffmpeg bundled with Shotcut / ShanaEncoder etc. into this app's component folder.\nRuntime libraries in the same folder (av*.dll / sw*.dll etc.) are copied too, and a trial run verifies it right after copying.",
    "直接把指定的 ffmpeg.exe（含同目录 dll）复制进组件目录。\n不用重新打包就能换掉内置的 ffmpeg。": "Copy the specified ffmpeg.exe (with the DLLs in its folder) straight into the component folder.\nLets you replace the bundled ffmpeg without repackaging.",
    "列出这台机器上所有能找到的 ffmpeg，以及各自带哪些硬件编码器。\n如果你的 PATH 里配了功能更全的 ffmpeg，可以在这里切过去。": "List every ffmpeg found on this machine and the hardware encoders each one has.\nIf your PATH contains a more capable ffmpeg, you can switch to it here.",
    "把「软件实际在用什么」原样输出：文件指纹、-encoders 原始输出、\n全部候选对比。贴给开发者可一次定位，不用反复试。": "Dump「what the app is actually using」as-is: file fingerprints, raw -encoders output, and a comparison of all candidates.\nPaste it to the developer to pinpoint an issue in one go, without repeated trial and error.",
    "把硬件编码不可用的各种原因逐项实测一遍（NVIDIA / Intel / AMD / Apple 都查），输出一份可复制的完整报告。": "Test each possible reason why hardware encoding is unavailable (NVIDIA / Intel / AMD / Apple all checked) and print a complete report you can copy.",
    "不切换软件设置，直接测试任意一个 ffmpeg.exe 的硬件编码能力。\n适合在正式切换前先确认下载的包到底行不行。": "Test the hardware encoding capability of any ffmpeg.exe without changing the app's settings.\nHandy for confirming whether a downloaded build really works before switching to it.",
    "列出已确认包含硬件编码支持（NVENC / QSV / AMF / VideoToolbox）的 ffmpeg 下载源。": "List ffmpeg download sources known to include hardware encoding support (NVENC / QSV / AMF / VideoToolbox).",
    "查看 ffmpeg 组件和下载残留占用了多少空间，并可清理。\n组件默认安装在软件目录的 vendor\\ffmpeg\\bin 下。": "Show how much space the ffmpeg component and download leftovers take up, and clean them up.\nThe component is installed under vendor\\ffmpeg\\bin in the app folder.",
    "ffmpeg 组件 / 硬件编码": "FFmpeg Component / Hardware Encoding",
    # ---------- 补充批次：状态文本 / 下拉项 / 对话框提示 ----------
    "硬件编码：自动检测失败，可点右侧按钮重试": "HW encoding: auto-detect failed — click the button on the right to retry",
    "硬件编码：没有可用的 ffmpeg": "HW encoding: no usable ffmpeg",
    "硬件编码：正在逐个实测，请稍候…": "HW encoding: testing each encoder, please wait…",
    "硬件编码：❌ 没有可用的（将用 CPU 软编 libx264）　点右侧按钮看原因": "HW encoding: ❌ none available (will fall back to CPU libx264) — click the button on the right for details",
    "ffmpeg 未就绪 —— 点击右上角「ffmpeg 状态」安装": "ffmpeg not ready — click \"ffmpeg Status\" at the top right to install",
    "等待 ffmpeg 就绪后自动检测": "Will auto-detect once ffmpeg is ready",
    "AMF 深度诊断…": "AMF Deep Diagnostics…",
    "未找到": "Not found",
    "高一点（CRF 18，文件稍大）": "Higher quality (CRF 18, larger file)",
    "推荐（CRF 20）": "Recommended (CRF 20)",
    "标准（CRF 23，文件较小）": "Standard (CRF 23, smaller file)",
    "（替换模式：不生成副本，直接覆盖原文件）": "(Replace mode: overwrite originals, no copies)",
    "转换质量：": "Conversion quality:",
    "转换目标（多数文件的参数，已自动识别，无需修改）": "Conversion target (parameters shared by most files, auto-detected — no need to change)",
    "原文件不会被改动。转换完成后，我把这些新文件和未转换的放在一起，你直接重新导入即可。": "Originals are untouched. When done, the new files are placed alongside the unconverted ones — just re-import them.",
    "重要视频请先备份！确认已经备份好，再点「继续」。": "Back up important videos first! Confirm you have a backup before clicking \"Continue\".",
    "这次对齐可能无法达到目的": "This alignment may not achieve its goal",
    "（未找到 ffprobe，跳过校验）": "(ffprobe not found, skipping verification)",
    "（无法加载校验模块，跳过）": "(Could not load verification module, skipping)",
    "没有检测到可用的硬件编码器。": "No usable hardware encoder detected.",
    "这不影响使用 —— 软件会自动用 CPU 软编（libx264）": "This does not block anything — the app falls back to CPU encoding (libx264)",
    "完成合并，只是速度慢一些。": "and completes the merge, just more slowly.",
    "点「选最快可用」即可切换过去；": "Click \"Pick Fastest\" to switch to it;",
    "也可以直接在上面的下拉框里选（可用的都带 ✔）。": "or pick one directly from the dropdown above (usable ones are marked ✔).",
    "检测到可用的硬件编码器：": "Usable hardware encoders detected:",
    "无法选择": "Cannot select",
    "已选择": "Selected",
    "智能推荐": "Smart Recommendation",
    "先在左侧导入视频，我再按它们的码率给建议。": "Import videos on the left first, then I'll suggest based on their bitrate.",
    "求快": "Prioritize speed",
    "求小": "Prioritize size",
    "更看重哪一点？": "Which matters more?",
    "已应用：": "Applied:",
    "为什么风扇不狂转？": "Why aren't the fans spinning up?",
    "为什么编码不行、解码却没问题？": "Why does encoding fail when decoding works?",
    "关于临时缓存文件": "About temporary cache files",
    "AV1 的特别处理": "Special handling for AV1",
    "选择要导入的 ffmpeg.exe": "Select ffmpeg.exe to import",
    "选择要使用的 ffmpeg.exe": "Select ffmpeg.exe to use",
    "选择要使用的 ffmpeg": "Select ffmpeg to use",
    "ffmpeg 可执行文件 (ffmpeg.exe ffmpeg);;所有文件 (*.*)": "ffmpeg executable (ffmpeg.exe ffmpeg);;All files (*.*)",
    "ffmpeg (ffmpeg.exe);;所有文件 (*.*)": "ffmpeg (ffmpeg.exe);;All files (*.*)",
    "ffmpeg (ffmpeg.exe ffmpeg);;所有文件 (*)": "ffmpeg (ffmpeg.exe ffmpeg);;All files (*)",
    "怎么选：": "How to choose:",
    "下载后解压，点「手动选择 ffmpeg」指定 bin 目录下的程序即可；": "After downloading and extracting, click \"Manual select ffmpeg\" and point to the binary in the bin folder;",
    "或点「导入本地 ffmpeg（复制到软件）」把它复制进来。": "or click \"Import local ffmpeg (copy into app)\" to copy it in.",
    "下面是完整诊断报告，可直接全选复制。": "Full diagnostic report below — select all and copy it.",
    "已替换": "Replaced",
    "ffmpeg 组件默认安装位置：": "Default install location of the ffmpeg component:",
    "—— 释放磁盘空间 ——": "— Free up disk space —",
    "删除 ffmpeg 组件（释放空间）": "Delete ffmpeg component (free space)",
    "手动选择（未切换软件设置）": "Manual select (does not change app settings)",
    "ffmpeg 能力测试": "ffmpeg capability test",
    "没有在这台机器上找到任何可用的 ffmpeg。": "No usable ffmpeg found on this machine.",
    "选择 ffmpeg 可执行文件": "Select ffmpeg executable",
    "选择 ffprobe 可执行文件": "Select ffprobe executable",
    "（没有找到已安装的组件）": "(No installed component found)",
    "已找到以下 ffmpeg（标注了各自实测可用的硬件编码）：": "Found the following ffmpeg builds (each annotated with its tested hardware encoders):",
    "详细说明": "Details",
    "关闭此提示": "Dismiss this hint",
    "<b>发现 {odd} 个文件和其他 {maj} 个不一样</b><br>只要把这 {odd} 个转成和大家一样的参数，整批 {total} 个就能<b>无损合并</b>——几分钟完成，画质音质零损失。<br>如果不处理，整批都要重新编码（慢很多，画质还会再损失一次）。": "<b>Found {odd} file(s) different from the other {maj}</b><br>Convert just those {odd} to match the rest, and all {total} can be <b>merged losslessly</b> — done in minutes with zero quality loss.<br>Otherwise the whole batch gets re-encoded (much slower, and quality degrades once more).",
    "将使用编码器：<b>{enc}</b>（目标视频格式 {fmt}，优先{kind}）": "Encoder to use: <b>{enc}</b> (target video format {fmt}, {kind} preferred)",
    "硬件（GPU）": "hardware (GPU)",
    "软件（CPU）": "software (CPU)",
    "⚠ 替换会<b>直接覆盖原文件且无法撤销</b>。重要视频请先备份！\n　（未勾选时输出到上面的文件夹，原文件不动）": "⚠ Replacing <b>overwrites the originals and cannot be undone</b>. Back up important videos first!\n　(When unchecked, files go to the folder above and originals stay untouched)",
    "选择音频输出文件夹": "Select audio output folder",
    "已开启：收尾时会重写整个文件": "On: the whole file is rewritten at the end",
    "已关闭：合并完成即结束，不额外重写文件": "Off: finishes when merging is done, no extra rewrite",
    "好处：网页播放 / 边下边播可以立刻开始": "Benefit: web playback / progressive streaming can start immediately",
    "代价：写完后再重写一遍，大文件会停在最后一步（日志里会显示「正在整理文件索引」）": "Cost: the file is rewritten after writing; large files pause at the last step (shown as \"Building file index\" in the log)",
    "本地播放（PotPlayer / VLC / 电视 / 手机相册）看不出区别": "No visible difference for local playback (PotPlayer / VLC / TV / phone gallery)",
    "大输出文件能省下几十秒收尾时间": "Saves tens of seconds of finalizing time for large outputs",
    "要放网上点播时再勾回来即可": "Tick it again when you need online streaming",
    "视频文件 (*.mp4 *.m4v *.mov *.mkv);;所有文件 (*)": "Video files (*.mp4 *.m4v *.mov *.mkv);;All files (*)",
    "选择父文件夹（其下每个子文件夹会生成一个任务）": "Select parent folder (each subfolder becomes one task)",
    "该文件夹内没有找到 mp4/m4v/mov/mkv 视频文件。": "No mp4/m4v/mov/mkv video files found in this folder.",
    "所选目录及其子文件夹中没有找到视频文件。": "No video files found in the selected directory or its subfolders.",
    "批量导入完成": "Batch import complete",
    "未找到 ffprobe，无法读取视频参数。请先在右上角「ffmpeg 状态」里安装或指定。": "ffprobe not found — cannot read video parameters. Install or specify it via \"ffmpeg Status\" at the top right.",
    "请先导入至少 2 个视频文件。": "Please import at least 2 video files.",
    "没有等待中的任务。请先把文件加入队列。": "No pending tasks. Add files to the queue first.",
    "已暂停（当前文件处理完后暂停）": "Paused (will pause after the current file finishes)",
    "（已清空输出名/目录，避免下一批沿用）": "(Output name/folder cleared to avoid reuse by the next batch)",
    "选中了多个任务，这里打开的是最后点击的那个；双击某一行可直接打开该任务。": "Multiple tasks selected — opening the last clicked one. Double-click a row to open that task directly.",
    "发现功能更全的 ffmpeg": "Found a more capable ffmpeg",
    "正在处理任务": "Processing task",
    "队列中还有任务正在处理，确定要退出吗？（当前任务会被取消）": "Tasks are still running. Quit anyway? (the current task will be cancelled)",
    "停止后当前任务会被取消，已生成的输出文件会保留。确定停止吗？": "Stopping cancels the current task; already generated output files are kept. Continue?",


    '硬件编码：❌ 没有可用的（将用 CPU 软编 libx264）\u3000点右侧按钮看原因': 'Hardware encoding: ❌ none available (will use CPU software encoder libx264)  Click the button on the right for details',
    '检测硬件编码': 'Detect HW Encoding',
    '下面是复刻检测每一步的结果。点「复制全部」发给开发者，可一次定位问题。': "Below is the result of each detection step. Click 'Copy All' and send it to the developer to pinpoint the issue at once.",
    '✔ ffmpeg 就绪': '✔ ffmpeg ready',

    "检测": "Detect",

    "系统 PATH": "system PATH",
    "随包附带": "bundled",
    "手动指定": "manually specified",
    "ffmpeg 未就绪 —— 点击右上角「ffmpeg 状态」安装": "ffmpeg not ready - click \"ffmpeg Status\" at the top right to install",

    "未找到 ffmpeg。请点击「安装/定位 ffmpeg」下载静态构建，或手动选择 ffmpeg.exe。": "ffmpeg not found. Click \"Install/Locate ffmpeg\" to download a static build, or pick ffmpeg.exe manually.",
    "找到 ffmpeg 但缺少 ffprobe：": "ffmpeg found but ffprobe missing: ",
    "ffmpeg 无法执行，请检查文件是否完整。": "ffmpeg cannot be executed; please check whether the file is intact.",

    # ---- 任务详情 / 输出摘要里**运行时填进表格**的中文 ----
    # 这些不是控件上写死的标签，而是填充单元格时才拼出来的文本，
    # 构造时的那次翻译遍历覆盖不到；靠 apply_translation 新加的
    # 「单元格内容」扫描来兜住，前提是字典里有对应条目。
    "视频编码器": "Video Encoder",
    "音频编码器": "Audio Encoder",
    "码率控制": "Rate Control",
    "Preset(速度/压缩率)": "Preset (speed / compression)",
    # 源码里两处写法不一致：一处半角括号、一处全角括号。
    # _remember 记住的是**首次见到的那一种**，后续再切语言就一直拿它去查字典，
    # 所以两种都必须收进去，否则永远有一半命中不了。
    "Preset（速度/压缩率）": "Preset (speed / compression)",
    "旋转处理": "Rotation",
    "保留旋转标记": "Keep rotation flag",
    "中间缓存容器": "Intermediate Cache Container",
    "视频附加参数": "Extra Video Options",
    "音频附加参数": "Extra Audio Options",
    "输出文件夹": "Output Folder",
    "提取音频": "Extract Audio",
    "实际输出": "Actual Output",
    "（尚未生成）": "(Not generated yet)",
    "(尚未生成)": "(Not generated yet)",
    "(无)": "(None)",
    "（无）": "(None)",
    "2ch(立体声)": "2ch (Stereo)",
    "2ch（立体声）": "2ch (Stereo)",
    "AAC-LC(标准,兼容性最好)": "AAC-LC (standard, best compatibility)",
    "AAC-LC（标准，兼容性最好）": "AAC-LC (standard, best compatibility)",
    "兼容性检测": "Compatibility Check",
    "未执行（未勾选无损检测）": "Not run (lossless check not enabled)",
    "先检测参数一致性,一致则无损合并(-c copy),不一致转码合并": (
        "Check parameter consistency first: merge losslessly (-c copy) if "
        "consistent, otherwise merge by transcoding"),
    "先检测参数一致性，一致则无损合并（-c copy），不一致转码合并": (
        "Check parameter consistency first: merge losslessly (-c copy) if "
        "consistent, otherwise merge by transcoding"),

    # ---- 带 HTML 的整段富文本 ----
    # 外层有 <span style=...>，按纯文本查字典必然查不到，
    # 只能整段（含标签）一起收进字典，英文版保留同样的 HTML 结构。
    "标<span style='background:#fff4c2; color:#1a1a1a;'>&nbsp;黄&nbsp;</span>的单元格"
    "＝与第一个文件不同且<b>影响合并</b>；"
    "标<span style='background:#e8f0fe; color:#1a1a1a;'>&nbsp;蓝&nbsp;</span>的"
    "＝不同但<b>不影响</b>（编码器自动算出，无法手动改）。\n"
    "时长 / 码率 / 文件大小本来就各不相同，未标色，也<b>不影响</b>合并。": (
        "Cells marked <span style='background:#fff4c2; color:#1a1a1a;'>"
        "&nbsp;yellow&nbsp;</span> differ from the first file and "
        "<b>affect merging</b>; those marked <span style="
        "'background:#e8f0fe; color:#1a1a1a;'>&nbsp;blue&nbsp;</span> differ "
        "but <b>do not affect</b> it (computed by the encoder, cannot be "
        "changed manually).\nDuration / bitrate / file size naturally vary and "
        "are not marked — they <b>do not affect</b> merging either."),
    "标<span style='background:#5c4a00; color:#ffe9a8;'>&nbsp;黄&nbsp;</span>的单元格"
    "＝与第一个文件不同且<b>影响合并</b>；"
    "标<span style='background:#1e3a5f; color:#a8c8ff;'>&nbsp;蓝&nbsp;</span>的"
    "＝不同但<b>不影响</b>（编码器自动算出，无法手动改）。\n"
    "时长 / 码率 / 文件大小本来就各不相同，未标色，也<b>不影响</b>合并。": (
        "Cells marked <span style='background:#5c4a00; color:#ffe9a8;'>"
        "&nbsp;yellow&nbsp;</span> differ from the first file and "
        "<b>affect merging</b>; those marked <span style="
        "'background:#1e3a5f; color:#a8c8ff;'>&nbsp;blue&nbsp;</span> differ "
        "but <b>do not affect</b> it (computed by the encoder, cannot be "
        "changed manually).\nDuration / bitrate / file size naturally vary and "
        "are not marked — they <b>do not affect</b> merging either."),
}

_cache: dict[str, str] = {}

# 原文缓存：key = (id(宿主对象), 索引)，用于 QTableWidgetItem 这类
# 非 QObject、没法挂 property 又必须在切换回中文时还原的对象。
_orig_store: dict[tuple, str] = {}


def _remember(key: tuple, cur: str) -> str:
    """记住中文原文并返回它（第二次调用起直接返回记住的值）。"""
    if key in _orig_store:
        return _orig_store[key]
    _orig_store[key] = cur
    return cur


def current_lang() -> str:
    return _lang


# ==================================================================
# 反向查表：英文 -> 中文
# ------------------------------------------------------------------
# 界面代码里有很多"直接写英文"的下拉项，中文模式下它们不在 STRINGS 的
# key 里，于是原样显示英文。这里按 value 反查回中文。
# ==================================================================
_REVERSE: dict[str, str] = {}


def _build_reverse() -> None:
    _REVERSE.clear()
    for zh, en in STRINGS.items():
        if zh != en and en not in _REVERSE:
            _REVERSE[en] = zh


_build_reverse()

# 专有名词：这些即使恰好命中译文也不能翻，否则技术标识会被改成中文
_NO_REVERSE = {
    # GPU 厂商 / 硬件
    "AMD", "NVIDIA", "Intel", "Apple", "Auto",
    # 编码器
    "aac", "aac_mf", "aac_at", "libfdk_aac", "libmp3lame", "ac3", "flac", "copy",
    "libx264", "libx265", "libsvtav1", "libaom-av1", "libvpx-vp9", "mpeg4",
    "h264_amf", "hevc_amf", "av1_amf", "h264_nvenc", "hevc_nvenc",
    "h264_qsv", "hevc_qsv", "h264_vaapi", "hevc_vaapi",
    "h264_videotoolbox", "hevc_videotoolbox", "av1_nvenc", "av1_qsv",
    # 容器 / 像素格式 / 其它技术标识
    "mp4", "mov", "mkv", "m4a", "mp3", "wav",
    "yuv420p", "yuv420p10le", "yuv422p", "yuv422p10le", "yuv444p", "nv12",
    "AAC-LC", "MPEG-TS", "MKV", "AV1", "HEVC", "CRF",
}

# 技术标识的长相：全小写、只含字母数字与 _ - . （如 libx264 / hevc_amf）
_TECH_RE = re.compile(r"^[a-z0-9_.-]+$")


def _normalize_punct(text: str) -> str:
    """英文模式下把全角标点换成半角。

    很多 tooltip / 标签是"英文句子 + 中文标点"（例如
    「Fill Encoding Params from This File」里的「」），
    整句已经在字典里翻好了，唯独标点是全角 —— 看起来就像没翻完。
    """
    if not text:
        return text
    out = text
    # 中文引号「」『』 -> 英文双引号
    out = out.replace("\u300c", '"').replace("\u300d", '"')
    out = out.replace("\u300e", '"').replace("\u300f", '"')
    # 全角括号 -> 半角
    out = out.replace("\uff08", "(").replace("\uff09", ")")
    # 全角空格 / 全角冒号 / 全角逗号
    out = out.replace("\u3000", " ")
    out = out.replace("\uff1a", ":").replace("\uff0c", ",")
    # 中文顿号 -> 逗号
    out = out.replace("\u3001", ",")
    # 全角加号 / 全角斜杠（"＋ 加入队列" 这类按钮文案）
    out = out.replace("\uff0b", "+")
    # 全角感叹号 / 问号 / 句号
    out = out.replace("\uff01", "!").replace("\uff1f", "?")
    out = out.replace("\u3002", ".")
    return out


# 标题前缀装饰符：①②③…、⚙、✓、• 等。
# "① 源视频参数" 整串查字典查不到（字典里只有"源视频参数"），
# 于是英文界面里 Tab 标题就漏翻了。这里把前缀剥掉再查，查到后拼回前缀。
_PREFIX_RE = re.compile(r"^([①②③④⑤⑥⑦⑧⑨⑩⚙✓✔✔•·—\-\*\s]+)(.+)$")


def _strip_prefix(text: str):
    """把装饰前缀与主体拆开，返回 (前缀, 主体)；无前缀则前缀为空串。"""
    if not text:
        return "", text
    m = _PREFIX_RE.match(text)
    if not m:
        return "", text
    return m.group(1), m.group(2)


def _translate_with_prefix(text: str, lookup) -> str:
    """先整串查；查不到就剥掉装饰前缀查主体，查到后把前缀拼回去。"""
    out = lookup(text)
    if out != text:
        return out
    prefix, body = _strip_prefix(text)
    if not prefix:
        return out
    got = lookup(body)
    return prefix + got if got != body else text


def _reverse_lookup(text: str) -> str:
    """中文模式下：把英文短语还原成中文。查不到 / 命中保护名单则原样返回。"""
    if not text:
        return text
    if text in _NO_REVERSE:
        return text
    if _TECH_RE.match(text):
        return text
    return _REVERSE.get(text, text)


def set_lang(lang: str) -> None:
    global _lang
    _lang = lang if lang in LANGUAGES else "zh"


def tr(text: str) -> str:
    """把中文原文翻译成当前语言。查不到就原样返回。"""
    if _lang == "zh" or not text:
        return text
    if text in _cache:
        return _cache[text]
    out = STRINGS.get(text)
    if out is None:
        # 去首尾空白再试一次，容忍 " 导入文件夹 " 这类写法
        s = text.strip()
        out = STRINGS.get(s, text)
        if out is not text and text.startswith(" "):
            out = text[:len(text) - len(text.lstrip())] + out
        # 仍是原文 → 试剥掉装饰前缀（"① 源视频参数" / "⚙ 软件自动调整"）
        if out == text:
            out = _translate_with_prefix(text, lambda t: STRINGS.get(t, t))
    if out is not None:
        out = _normalize_punct(out)
    _cache[text] = out
    return out


# ==================================================================
# 运行时扫描：递归遍历控件树并替换文案
# ==================================================================
def _trans(orig: str) -> str:
    """把原文按当前语言转换（供已记住原文的对象使用）。

    中文模式下会顺便把"写死在代码里的英文"反查回中文，
    否则这些项在任何语言下都只显示英文。
    英文模式下顺带把全角标点换成半角。
    """
    if not orig:
        return orig
    if _lang == "zh":
        return _reverse_lookup(orig)
    out = _translate_with_prefix(orig, lambda s: STRINGS.get(s, s))
    return _normalize_punct(out)


# ---- 富文本（HTML）翻译 ------------------------------------------------
# 界面里有一批 QLabel 设了 RichText（"标<span ...>黄</span>的单元格＝…"），
# 整串带标签，字典按纯文本查必然查不到 —— 于是英文界面里这些说明永远停在中文。
# 这里把 HTML 拆成「标签」与「标签之间的文本」两段交替的序列，
# 只翻译文本段，标签原样保留，再拼回去。
_TAG_SPLIT_RE = re.compile(r"(<[^>]+>)")


def tr_rich(html: str) -> str:
    """翻译富文本：只翻标签外的文字，标签原样保留。

    纯文本（不含 < >）走普通 tr()，行为不变。
    """
    if not html or "<" not in html or ">" not in html:
        return tr(html)
    parts = _TAG_SPLIT_RE.split(html)
    out = []
    for seg in parts:
        if not seg:
            continue
        # 标签段：原样保留（属性里有颜色值，翻了会坏）
        if seg.startswith("<") and seg.endswith(">"):
            out.append(seg)
            continue
        # 纯空格/换行的文本段也原样，免得被 trim 掉影响排版
        if not seg.strip():
            out.append(seg)
            continue
        out.append(tr(seg))
    return "".join(out)


def _collect_actions(root):
    """收集控件上所有 QAction，含菜单栏各级子菜单（findChildren 会漏掉这些）。"""
    seen, out = set(), []

    def _walk(obj):
        if obj is None:
            return
        try:
            acts = obj.actions()
        except Exception:  # noqa: BLE001
            return
        for a in acts:
            if not isinstance(a, object) or id(a) in seen:
                continue
            try:
                if not a.text():
                    continue
            except Exception:  # noqa: BLE001
                continue
            seen.add(id(a))
            out.append(a)
            try:
                sub = a.menu()
            except Exception:  # noqa: BLE001
                sub = None
            if sub is not None:
                _walk(sub)

    try:
        _walk(root)
    except Exception:  # noqa: BLE001
        pass
    try:
        mb = root.menuBar()
        if mb is not None:
            _walk(mb)
    except Exception:  # noqa: BLE001
        pass
    return out


def apply_translation(root) -> int:
    """把 root 及其所有子控件的文案切换为当前语言，返回替换处数。"""
    if root is None:
        return 0
    n = 0
    # 运行时拼接出来的文本（如状态栏 ffmpeg 来源）不会自动翻译，
    # 让窗口自行重建一遍。
    try:
        hook = getattr(root, 'retranslate_dynamic', None)
        if callable(hook):
            hook()
    except Exception:
        pass
    try:
        from PySide6.QtWidgets import (QWidget, QTabWidget, QMenu, QMenuBar,
                                       QAbstractButton, QLabel, QGroupBox,
                                       QLineEdit, QComboBox, QToolButton)
    except Exception:  # noqa: BLE001
        return 0

    # 原文缓存在控件属性上：中→英→中 来回切换时才能还原，
    # 否则第二次切换会拿英文去查字典（字典的 key 是中文），查不到就卡在英文。
    PROP = "_i18n_orig"
    PROP_ITEMS = "_i18n_items"

    def _translate(w) -> int:
        if w is None:
            return 0
        cnt = 0
        # QMenu 的 title 与它对应的 QAction.text 是同一份文本，
        # 两边都改会让"中文原文"被英文覆盖，切换回中文就还原不了。
        # 这里跳过 title，统一交给下面的 QAction 循环处理。
        #
        # QSpinBox 的 text() 是「数值 + suffix」合成出来的，
        # 拿它去 setText 会被当成用户输入重新解析 —— 有改坏数值的风险，
        # 对这类控件只翻译 suffix / prefix。
        try:
            from PySide6.QtWidgets import QAbstractSpinBox
            is_spin = isinstance(w, QAbstractSpinBox)
        except Exception:  # noqa: BLE001
            is_spin = False
        skip = {"title"} if isinstance(w, QMenu) else set()
        if is_spin:
            skip.add("text")
        for getter, setter in (
            ("text", "setText"),
            ("title", "setTitle"),
            ("toolTip", "setToolTip"),
            ("placeholderText", "setPlaceholderText"),
            ("windowTitle", "setWindowTitle"),
            ("statusTip", "setStatusTip"),
            ("suffix", "setSuffix"),
            ("prefix", "setPrefix"),
        ):
            try:
                g = getattr(w, getter, None)
                if not callable(g):
                    continue
                cur = g()
            except Exception:  # noqa: BLE001
                continue
            if not isinstance(cur, str) or not cur or getter in skip:
                continue

            # 取中文原文（已记录过就直接取）
            try:
                stored = w.property(PROP)
            except Exception:  # noqa: BLE001
                stored = None
            orig_map = stored if isinstance(stored, dict) else {}
            orig = orig_map.get(getter)
            if orig is None:
                orig = cur                      # 首次：当前值即原文
                orig_map = dict(orig_map)
                orig_map[getter] = orig
                try:
                    w.setProperty(PROP, orig_map)
                except Exception:  # noqa: BLE001
                    pass

            # 富文本（RichText）带 HTML 标签，整串按纯文本查字典必然查不到。
            # 对这类控件改走 tr_rich：只翻标签外的文字，标签原样保留。
            is_rich = False
            if getter == "text":
                try:
                    from PySide6.QtCore import Qt as _Qt
                    is_rich = (w.textFormat() == _Qt.RichText)
                except Exception:  # noqa: BLE001
                    is_rich = False
                # AutoText 也算：setText("差异在：<b>分辨率</b>…") 时
                # textFormat() 仍是 AutoText，但内容会被当富文本渲染，
                # 整串查字典同样查不到 —— 按"内容里有没有标签"判断最稳。
                if not is_rich and "<" in cur and ">" in cur:
                    is_rich = True
            if is_rich:
                new = tr_rich(orig)
            else:
                new = _trans(orig)
            if new != cur:
                try:
                    getattr(w, setter)(new)
                    cnt += 1
                except Exception:  # noqa: BLE001
                    pass

        # ---- 下拉框的**选项** ----
        # 只翻标签、不翻选项的话，英文界面里标签是英文、点开全是中文，等于没翻。
        # 编码器名（aac / libmp3lame / hevc_amf…）天然不在字典里，不会被误翻。
        # 可编辑下拉框也要翻**候选项**（itemText）。
        # 之前跳过它是误判：用户的输入在 lineEdit 里，而 itemText 是固定候选，
        # 两者不是一回事。跳过导致带微调箭头的那批（恰恰都是可编辑的）
        # 在中文模式下永远显示英文。
        # 编码器名（aac / libmp3lame / hevc_amf…）不在字典里，不会被误翻。
        if isinstance(w, QComboBox):
            try:
                stored = w.property(PROP_ITEMS)
                origs = list(stored) if isinstance(stored, list) else None
                if origs is None or len(origs) != w.count():
                    origs = [w.itemText(i) for i in range(w.count())]
                    w.setProperty(PROP_ITEMS, origs)
                was_blocked = w.signalsBlocked()
                w.blockSignals(True)
                try:
                    cur_text = w.currentText() if w.isEditable() else None
                    for i, o in enumerate(origs):
                        new = _trans(o)
                        if new != w.itemText(i):
                            w.setItemText(i, new)
                            cnt += 1
                    # 可编辑框：候选改了之后把用户原本的输入放回去，
                    # 免得被候选项顶掉（例如手输的分辨率）。
                    if cur_text is not None and w.currentText() != cur_text:
                        try:
                            w.setEditText(cur_text)
                        except Exception:  # noqa: BLE001
                            pass
                finally:
                    w.blockSignals(was_blocked)
            except Exception:  # noqa: BLE001
                pass
        return cnt

    n += _translate(root)

    # 递归子控件
    # 子对话框（ffmpeg 状态窗等）也可能是"运行时拼接"的文案持有者，
    # 它们有自己的 retranslate_dynamic —— 只调 root 的会漏掉，
    # 于是英文界面里子窗口仍是一段中文。
    try:
        for child in root.findChildren(QWidget):
            n += _translate(child)
    except Exception:  # noqa: BLE001
        pass

    # QAction（菜单、工具栏）
    # 注意：只用 _collect_actions() 会漏掉"直接以窗口为 parent 构造"的游离
    # QAction（例如只用来注册快捷键、没加进任何菜单的那个）——
    # 它们不出现在 QWidget.actions() 里，只存在于 children 中，
    # 必须靠 findChildren(QAction) 才能捞到。
    try:
        from PySide6.QtGui import QAction
        _seen_act = set()
        _all_acts = []
        for act in list(_collect_actions(root)) + list(root.findChildren(QAction)):
            if id(act) in _seen_act:
                continue
            _seen_act.add(id(act))
            _all_acts.append(act)
        for act in _all_acts:
            try:
                old = act.text()
                if old:
                    orig = _orig_store.get(("act", id(act)), None)
                    if orig is None:
                        orig = _orig_store[("act", id(act))] = old
                    new = _trans(orig)
                    if new != old:
                        act.setText(new)
                        n += 1
                old_tip = act.toolTip()
                if old_tip:
                    new_tip = tr(old_tip)
                    if new_tip != old_tip:
                        act.setToolTip(new_tip)
            except Exception:  # noqa: BLE001
                pass
    except Exception:  # noqa: BLE001
        pass

    # 表格 / 树的表头
    try:
        from PySide6.QtWidgets import QTableWidget, QTreeWidget, QHeaderView
        for tw in root.findChildren(QTableWidget):
            try:
                hdr = tw.horizontalHeader()
                model = tw.model()
                for col in range(tw.columnCount()):
                    item = tw.horizontalHeaderItem(col)
                    if item is not None:
                        old = item.text()
                        if old:
                            new = _trans(_remember(("hdr", id(tw), col), old))
                            if new != old:
                                item.setText(new)
                                n += 1
                    elif model is not None:
                        old = model.headerData(col, 1)
                        if isinstance(old, str) and old:
                            new = tr(old)
                            if new != old:
                                model.setHeaderData(col, 1, new)
                                n += 1
            except Exception:  # noqa: BLE001
                pass

            # ---- 表格**单元格** ----
            # 任务详情里的「兼容性检测 / 合并策略 / 输出文件夹 / 结果」都是
            # refresh_all() 运行时填进 QTableWidgetItem 的，填完如果不翻译，
            # 英文界面里这些行永远是中文（表头翻了、内容没翻，看起来更怪）。
            #
            # 只翻「纯参数标签」类文本：文件名、路径、数值、编码器名
            # （aac / hevc_amf…）本来就长那样，翻了反而错，交给 _trans 的保护规则挡掉。
            try:
                for r in range(tw.rowCount()):
                    for c in range(tw.columnCount()):
                        it = tw.item(r, c)
                        if it is None:
                            continue
                        old = it.text()
                        if not old:
                            continue
                        new = _trans(_remember(("cell", id(it)), old))
                        if new != old:
                            it.setText(new)
                            n += 1
            except Exception:  # noqa: BLE001
                pass
    except Exception:  # noqa: BLE001
        pass

    # 下拉框条目
    try:
        from PySide6.QtWidgets import QComboBox
        for cb in root.findChildren(QComboBox):
            try:
                for i in range(cb.count()):
                    old = cb.itemText(i)
                    if not old:
                        continue
                    new = _trans(_remember(("cb", id(cb), i), old))
                    if new != old:
                        cb.setItemText(i, new)
                        n += 1
            except Exception:  # noqa: BLE001
                pass
    except Exception:  # noqa: BLE001
        pass

    # ---- 表格 / 树的**单元格内容** ----
    #
    # 只翻表头是不够的：任务详情里的「等待中 / 已完成 / 需转码合并」这类
    # 状态与提示是**运行时填进单元格**的，构造时的那次遍历根本还没这些行。
    # 于是切到英文后，主窗口是英文、弹出的详情框却整屏中文。
    #
    # 安全性：_trans 只在字典里查得到才替换，查不到原样返回 ——
    # 文件名「第001集.mp4」、编码名「hevc」、分辨率「1884x1080」都不在字典，
    # 天然不会被误翻；而「等待中」这类恰好是我们要翻的。
    try:
        from PySide6.QtWidgets import (
            QTableWidget, QTreeWidget, QTableWidgetItem, QLabel as _QLabel,
        )
        _MAX_CELL = 200   # 超长的多半是日志/参数值，跳过省时间
        for tw in list(root.findChildren(QTableWidget)) + list(root.findChildren(QTreeWidget)):
            try:
                rows = tw.rowCount()
                cols = tw.columnCount()
            except Exception:  # noqa: BLE001
                continue
            for r in range(rows):
                for c in range(cols):
                    try:
                        it = tw.item(r, c)
                    except Exception:  # noqa: BLE001
                        it = None
                    if it is not None:
                        old = it.text()
                        if old and len(old) <= _MAX_CELL:
                            new = _trans(_remember(("cell", id(tw), r, c), old))
                            if new != old:
                                it.setText(new)
                                n += 1
                    # 单元格里塞的 QLabel（多行提示用），也要跟着换
                    try:
                        cw = tw.cellWidget(r, c)
                    except Exception:  # noqa: BLE001
                        cw = None
                    if isinstance(cw, _QLabel):
                        old = cw.text()
                        if old and len(old) <= _MAX_CELL:
                            new = _trans(_remember(("cellw", id(cw)), old))
                            if new != old:
                                cw.setText(new)
                                n += 1
    except Exception:  # noqa: BLE001
        pass

    # TabWidget 页签
    try:
        from PySide6.QtWidgets import QTabWidget
        for tw in root.findChildren(QTabWidget):
            try:
                for i in range(tw.count()):
                    old = tw.tabText(i)
                    if old:
                        new = _trans(_remember(("tab", id(tw), i), old))
                        if new != old:
                            tw.setTabText(i, new)
                            n += 1
                    old_tip = tw.tabToolTip(i)
                    if old_tip:
                        new_tip = tr(old_tip)
                        if new_tip != old_tip:
                            tw.setTabToolTip(i, new_tip)
            except Exception:  # noqa: BLE001
                pass
    except Exception:  # noqa: BLE001
        pass

    # ---- 最后一步：重建"运行时拼接"出来的文案 ----
    #
    # 必须放在**所有 _translate 之后**。因为 _translate 会把控件上缓存的
    # 中文原文重新写回去（那是它做中→英→中来回切换的依据）。
    # 如果这里先跑，刚才 tr() 拼好的英文会被立刻覆盖回中文 ——
    # 实测：hook 在前 → 英文变中文；hook 在后 → 正常。
    #
    # 子对话框（ffmpeg 状态窗）和提示卡片（HintCard）都靠这个钩子重建。
    try:
        _hooks = []
        _h = getattr(root, 'retranslate_dynamic', None)
        if callable(_h):
            _hooks.append(_h)
        for child in root.findChildren(QWidget):
            _h2 = getattr(child, 'retranslate_dynamic', None)
            if callable(_h2):
                _hooks.append(_h2)
        for _h3 in _hooks:
            try:
                _h3()
            except Exception:  # noqa: BLE001
                pass
    except Exception:  # noqa: BLE001
        pass

    return n


def _config_path() -> str:
    import os
    return os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "config.json")


def system_locale_raw() -> str:
    """系统区域设置的原始字符串（如 zh_CN / en_US），取不到返回空串。

    先问 Qt（Windows / macOS 上最准），再退到环境变量，最后是 Python locale。
    """
    try:
        from PySide6.QtCore import QLocale
        name = (QLocale.system().name() or "").strip()
        # "C" / "POSIX" 表示用户没设过区域，不算有效信息
        if name and name.lower() not in ("c", "posix"):
            return name
    except Exception:  # noqa: BLE001
        pass
    import os
    for var in ("LC_ALL", "LC_MESSAGES", "LANG"):
        v = (os.environ.get(var) or "").strip()
        if v and v.lower() not in ("c", "posix"):
            return v.split(".")[0]
    try:
        import locale
        v = (locale.getdefaultlocale()[0] or "").strip()
        if v:
            return v
    except Exception:  # noqa: BLE001
        pass
    return ""


def detect_system_lang() -> str:
    """按系统语言猜界面语言：中文系 → zh，其它一律 en。

    取不到任何线索时返回 zh，与历史上的默认行为保持一致
    （避免老用户升级后界面语言莫名改变）。
    """
    raw = system_locale_raw()
    if raw.lower().startswith("zh"):
        return "zh"
    # 完全取不到信息时，沿用中文
    return "en" if raw else "zh"


def load_lang_from_config() -> str:
    """从 config.json 读取语言设置。

    首次启动（没有 config.json，或里面还没有 ui_lang）时不存在"上次的选择"，
    这时跟随系统语言：中文系用中文，其它一律英文。
    用户一旦手动切过，就以 config 里存的为准。
    """
    import json
    import os
    p = _config_path()
    if not os.path.exists(p):
        return detect_system_lang()
    try:
        with open(p, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except Exception:  # noqa: BLE001
        return detect_system_lang()
    v = data.get("ui_lang")
    if v in LANGUAGES:
        return v
    return detect_system_lang()       # 老配置里没这个键 → 当作首次


def save_lang_to_config(lang: str) -> None:
    """把语言设置写回 config.json（只改这一个键，不动其它配置）。"""
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
    data["ui_lang"] = lang
    try:
        with open(p, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)
    except Exception:  # noqa: BLE001
        pass
