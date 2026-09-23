# EasyMerger

**简体中文** | [English](#english)

**通用短剧/漫剧等视频无损合并 / 参数对齐 / 批量导入排队工具。**

## 😣解决的痛点：
- 1.引入合并前的智能判断：市面上的视频合并拼接软件比如（shanaencoder），几乎都没有无损合并（-c copy）前的判断，让用户自己判断。一批视频比如短剧剧集看似参数一致，但其实不一致，使用无损合并导致输出视频花屏、卡声音等。本软件引入合并前智能判断功能，不用人工判断（人工也判断不准），参数相同走无损合并通道（耗时超短），参数不同转码合并通道。
- 2.批量添加（每文件夹一个任务）：以每个文件夹下检测到的第一个视频为基准转码参数（需转码合并的情况下），多批剧集一键导入任务直接开始，上午导入任务，下午验收成果，中途几乎不用管，省心。
- 3.尽最大可能保证视频原画质：为了强化这一功能我们在无损合并的基础上引入了一键对齐异类和智能直通功能，这些功能不但保住了画质还节省了多余转码的时间。

核心目标只有一个：**能用无损合并就绝不用转码。**

软件会自动检测一批视频的参数是否一致，一致就 `-c copy` 秒合（画质零损失）；
只有少数几个文件参数不一样时，用「一键对齐异类」只转换那几个，剩下的照样无损合并 ——
而不是把上百个文件全部重新编码一遍。

适用于**任何"一批同格式片段需要首尾相接合成一个文件"**的场景，例如：

- 会议 / 课程 / 直播的分段录屏
- 行车记录仪、运动相机、监控导出的片段
- 手机分段拍摄的素材
- 动画、剧集(短剧、漫剧等)、有声书、播客录像
- 任何来自同一批转码、但参数略有出入的视频文件

---

## ✨ 主要特性

| 特性 | 说明 |
|---|---|
| **参数一致性检测** | 逐项比对分辨率 / 帧率 / 编码 / Profile / 采样率等，标出差异并分组统计 |
| **智能直通** | 视频参数一致时只统一音频，视频流 `copy`，零损失且极快 |
| **一键对齐异类** | 只转换参数不同的那几个文件，转完自动校验是否与多数派一致 |
| **硬件编码** | 支持 AMD AMF / NVIDIA NVENC / Intel QSV / Apple VideoToolbox，可手动指定用哪张卡 |
| **批量导入排队** | 每个文件夹一个任务，排队顺序处理，支持暂停 / 继续 / 停止 |
| **音频 Profile 处理** | 自动识别各编码器的实际能力，区分"能编码"与"能解码" |
| **容错回退** | 单文件直通失败时自动诊断码流 → 换中间容器 → 重编码救回，不让整个任务陪葬 |
| **主题与语言** | 浅色 / 深色主题，简体中文 / English 界面（菜单 → 视图） |

---

## 📦 下载与安装

从 [Releases](https://github.com/golanse/easymerger/releases) 页面下载
`easymerger_Vx.xx_Windows.zip`，解压后双击 `easymerger.exe` 即可。

**关于 ffmpeg**：

- 发布包内**已包含** FFmpeg 二进制（GPL 构建），开箱即用
- 如果想用功能更全的构建（例如带 `libfdk_aac`、可输出 HE-AAC），
  可在软件内「ffmpeg 状态」→「手动选择 ffmpeg」切换为你自己的 `ffmpeg.exe`
- 若下载的是"精简包"（不含 ffmpeg），请把 `ffmpeg.exe` / `ffprobe.exe`
  放到 `vendor/ffmpeg/bin/` 目录

---

## 🚀 快速上手

1. **导入** —— 「导入文件夹」或「批量导入（每文件夹一个任务）」
2. **看检测结论** —— 列表下方会显示能否无损合并、有几项差异、涉及哪些文件
3. **能无损就直接合并** —— 设置输出文件夹与文件名，「＋ 加入队列」→「▶ 开始队列」
4. **不能无损就先对齐** —— 点「⚡ 一键对齐异类」，只转换那几个；转完重新导入即可无损合并

> 💡 典型场景：同一批素材里往往只有少数几个文件的参数与主流不同
> （分辨率、帧率或 AAC Profile）。用「一键对齐异类」处理这几个，
> 比整批转码快几十倍，且其余文件的画质完全不损失。

---

## 🛠 从源码运行 / 打包

要求 Python 3.9+ 与 PySide6。

```bash
pip install -r requirements.txt

# 直接运行
python main.py

# Windows 打包（需先把 ffmpeg.exe / ffprobe.exe 放入 vendor/ffmpeg/bin/）
pyinstaller build.spec
```

自检脚本：

```bash
python scripts/verify_all.py     # 语法 / 未定义名 / 回归测试
python scripts/check_undefined.py # 纯标准库实现的未定义名检查
```

---

## 📄 许可证

### 本软件：MIT

EasyMerger 自身的源代码采用 **MIT 许可证**，详见 [LICENSE](LICENSE)。
你可以自由使用、修改、分发，包括商业用途，只需保留版权声明。

### 内置的 FFmpeg：GPL / LGPL ⚠️

本项目的**发布包内附带 FFmpeg 二进制**（`ffmpeg` / `ffprobe`），
它是独立于本软件的第三方程序，依据 **GNU 通用公共许可证（GPL）第 2 版或第 3 版**
或 **GNU 宽通用公共许可证（LGPL）第 2.1 版或第 3 版** 授权，
具体取决于所用构建的配置。

EasyMerger 通过**命令行子进程**调用 FFmpeg，不链接、不修改其代码，
因此 FFmpeg 的许可证不影响本软件自身的 MIT 授权。
但分发其二进制需要履行相应义务，特此说明：

**源码获取方式（任选其一）**

- 官方 Git 仓库：<https://git.ffmpeg.org/ffmpeg.git>
- GitHub 镜像：<https://github.com/FFmpeg/FFmpeg>
- 官方下载页：<https://ffmpeg.org/download.html>

你也可以向本项目作者索取所分发 FFmpeg 二进制的完整对应源码，
我们将提供获取方式，或按介质成本提供书面报价。

**关于 nonfree 构建**

部分 FFmpeg 构建会启用 `--enable-nonfree` 以包含 `libfdk_aac` 等组件。
这类组件**不属于自由软件、不可再分发**，仅供个人自行编译使用。
本项目的 Release **不会**包含此类构建；如果你自己编译了 nonfree 版本，
请仅自用，不要对外分发。

### 其它第三方组件

| 组件 | 许可证 |
|---|---|
| FFmpeg | GPLv2+ / GPLv3+ / LGPL |
| Qt for Python (PySide6) | LGPLv3 / GPLv2 |
| PyInstaller | GPLv2 + 例外条款 |
| Python | PSF License |

---

## ☕ 支持本项目

如果 EasyMerger 帮到了你，欢迎请作者喝杯咖啡 ☕

<div align="center">
  <img src="https://raw.githubusercontent.com/golanse/easymerger/main/docs/sponsor-wechat.jpg" width="220" alt="微信打赏">
  &nbsp;&nbsp;&nbsp;&nbsp;
  <img src="https://raw.githubusercontent.com/golanse/easymerger/main/docs/sponsor-usdt.jpg" width="220" alt="USDT打赏">
</div>

<div align="center">
  <b>微信 WeChat</b> &nbsp;|&nbsp; <b>USDT(主网:solana)</b> &nbsp;|&nbsp; <a href="https://www.paypal.com/paypalme/piger2008"target="_blank"><b>PAYPAL</b></a>
</div>

> 打赏完全自愿，不会解锁任何功能。本项目永久开源免费，功能对所有用户一致。

---

## 🤝 参与贡献

欢迎提交 Issue 与 Pull Request。提交前请先运行 `python scripts/verify_all.py` 确认自检通过。

---
---

<a name="english"></a>

# English

[简体中文](#easymerger) | **English**

**A general-purpose tool for lossless video merging, parameter alignment, and batch queuing.**

## 😣 Pain point being solved：
- 1. Introducing intelligent pre-merge judgment: Most video merging and stitching software on the market, such as ShanaEncoder, do not provide pre-merge lossless judgment (-c copy), leaving users to decide on their own. A batch of videos, such as short drama episodes, may seem to have consistent parameters, but actually do not. Using lossless merging can result in issues like output video screen distortion and audio stuttering. This software introduces an intelligent pre-merge judgment feature, eliminating the need for manual evaluation (which is often inaccurate). Videos with the same parameters use the lossless merge channel (extremely fast), while videos with different parameters go through the transcoding merge channel.
- 2. Batch addition (one task per folder): The first video detected in each folder serves as the reference for transcoding parameters (when transcoding merge is needed). Multiple batches of episodes can be imported with one click to start tasks immediately. Tasks imported in the morning can be reviewed in the afternoon with minimal intervention, making it worry-free.
- 3. Maximizing the preservation of original video quality: To strengthen this feature, we introduced one-click alignment for different formats and intelligent passthrough in addition to lossless merging. These functions not only maintain video quality but also save time by avoiding unnecessary transcoding.

It has exactly one core goal: **never transcode if it can concatenate losslessly.**

EasyMerger inspects a batch of videos and checks whether their parameters match.
If they do, it merges them with `-c copy` in seconds (zero quality loss).
If only a handful of files differ, **Align Outliers** converts just those few,
and the rest are still merged losslessly — instead of re-encoding hundreds of files.

It works for **any set of same-format clips that need to be joined end-to-end**, for example:

- Segmented meeting / lecture / livestream recordings
- Clips from dashcams, action cameras, or surveillance systems
- Footage shot in multiple takes on a phone
- Animation, TV series, audiobooks, podcast recordings
- Any batch of files that came from the same encode but ended up with slightly different parameters

---

## ✨ Features

| Feature | Description |
|---|---|
| **Parameter check** | Compares resolution / fps / codec / profile / sample rate item by item, highlights differences and groups them by value |
| **Smart passthrough** | When video parameters match, only audio is normalized; video stream is `copy` — lossless and very fast |
| **Align outliers** | Converts only the files that differ, then verifies they now match the majority |
| **Hardware encoding** | AMD AMF / NVIDIA NVENC / Intel QSV / Apple VideoToolbox, with manual GPU selection |
| **Batch queue** | One task per folder, processed in order, with pause / resume / stop |
| **Audio profile handling** | Probes each encoder's real capabilities; distinguishes "can encode" from "can decode" |
| **Fault-tolerant fallback** | If passthrough fails for one file: diagnose the stream → switch intermediate container → re-encode to rescue it, instead of failing the whole task |
| **Theme & language** | Light / dark theme, Simplified Chinese / English UI (menu → View) |

---

## 📦 Download & Install

Download `easymerger_Vx.xx_Windows.zip` from the
[Releases](https://github.com/golanse/easymerger/releases) page, extract it,
and run `easymerger.exe`.

**About FFmpeg**:

- Release packages **already include** FFmpeg binaries (GPL build) — works out of the box
- To use a fuller build (e.g. one with `libfdk_aac` for HE-AAC output),
  switch to your own `ffmpeg.exe` via 「FFmpeg status」→「Select FFmpeg manually」
- If you downloaded a "slim package" (no FFmpeg), place `ffmpeg.exe` / `ffprobe.exe`
  into the `vendor/ffmpeg/bin/` directory

---

## 🚀 Quick Start

1. **Import** — 「Import folder」or「Batch import (one task per folder)」
2. **Read the check result** — the panel below the list shows whether lossless merging is possible, how many parameters differ, and which files are affected
3. **If lossless, merge directly** — set output folder and filename, then「＋ Add to queue」→「▶ Start queue」
4. **If not, align first** — click「⚡ Align outliers」to convert only those few files; re-import afterwards for a lossless merge

> 💡 Typical case: in one batch, usually only a few files differ from the majority
> (resolution, frame rate, or AAC profile). Aligning just those few is
> dozens of times faster than transcoding the whole batch, and the other files
> keep their original quality entirely.

---

## 🛠 Run from Source / Build

Requires Python 3.9+ and PySide6.

```bash
pip install -r requirements.txt

# Run directly
python main.py

# Windows build (place ffmpeg.exe / ffprobe.exe into vendor/ffmpeg/bin/ first)
pyinstaller build.spec
```


Self-check scripts:

```bash
python scripts/verify_all.py     # syntax / undefined names / regression tests
python scripts/check_undefined.py # undefined-name check using only the standard library
```

---

## 📄 License

### This software: MIT

EasyMerger's own source code is licensed under the **MIT License** — see [LICENSE](LICENSE).
You are free to use, modify, and distribute it, including for commercial purposes,
provided the copyright notice is retained.

### Bundled FFmpeg: GPL / LGPL ⚠️

Release packages of this project **ship FFmpeg binaries** (`ffmpeg` / `ffprobe`),
which are third-party programs independent of this software. They are licensed under
the **GNU General Public License (GPL) v2 or v3** or the
**GNU Lesser General Public License (LGPL) v2.1 or v3**, depending on how the
particular build was configured.

EasyMerger invokes FFmpeg as a **command-line subprocess** and does not link to or
modify its code, so FFmpeg's license does not affect the MIT licensing of this
software itself. However, redistributing its binaries carries corresponding
obligations, which are stated here:

**How to obtain the source (any one of these)**

- Official Git repository: <https://git.ffmpeg.org/ffmpeg.git>
- GitHub mirror: <https://github.com/FFmpeg/FFmpeg>
- Official download page: <https://ffmpeg.org/download.html>

You may also request from the author of this project the complete corresponding
source of the FFmpeg binaries being distributed; we will provide the means to
obtain it, or a written quotation covering media costs.

**About nonfree builds**

Some FFmpeg builds enable `--enable-nonfree` to include components such as
`libfdk_aac`. Such components are **not free software and may not be
redistributed**; they are for personal, self-compiled use only.
Releases of this project will **not** include such builds. If you compile a
nonfree build yourself, please keep it private and do not distribute it.

### Other third-party components

| Component | License |
|---|---|
| FFmpeg | GPLv2+ / GPLv3+ / LGPL |
| Qt for Python (PySide6) | LGPLv3 / GPLv2 |
| PyInstaller | GPLv2 with exception |
| Python | PSF License |

---

## ☕ Support

If EasyMerger helped you, you can buy me a coffee ☕

<div align="center">
  <img src="https://raw.githubusercontent.com/golanse/easymerger/main/docs/sponsor-wechat.jpg" width="220" alt="WeChat Pay">
  &nbsp;&nbsp;&nbsp;&nbsp;
  <img src="https://raw.githubusercontent.com/golanse/easymerger/main/docs/sponsor-usdt.jpg" width="220" alt="USDT">
</div>

<div align="center">
  <b>WeChat Pay</b> &nbsp;|&nbsp; <b>USDT(network:solana)</b> &nbsp;|&nbsp; <a href="https://www.paypal.com/paypalme/piger2008"target="_blank"><b>PAYPAL</b></a>
</div>

> Sponsorship is entirely optional and does not unlock any features.
> This project is permanently free and open-source; all features are the same for everyone.

---

## 🤝 Contributing

Issues and pull requests are welcome. Please run `python scripts/verify_all.py`
and make sure the self-check passes before submitting.
