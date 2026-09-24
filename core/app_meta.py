"""软件名称与版本 —— 全项目唯一出处。

之前名字和版本散落在 main.py / build.spec / selfcheck.py / 打包脚本里，
改名要改七八个文件，漏一处就出现"标题是新名字、安装包还是旧名字"的尴尬。
现在统一在这里定义，其余各处一律引用。
"""

from __future__ import annotations

__all__ = ["APP_NAME", "APP_NAME_DISPLAY", "VERSION", "VERSION_TAG",
           "FULL_TITLE", "ORG_NAME", "BUNDLE_ID"]

APP_NAME = "easymerger"        # 用于文件名、安装包名（不含空格，避免路径问题）
APP_NAME_DISPLAY = "EasyMerger"  # 用于窗口标题等展示
VERSION = "1.47"
VERSION_TAG = "V1.47"           # 界面上显示的完整版本标识

ORG_NAME = "easymerger"
BUNDLE_ID = "com.easymerger.app"   # macOS 的 Bundle Identifier

# 窗口标题：EasyMerger V1.0  ·  ……
FULL_TITLE = f"{APP_NAME_DISPLAY}  ·  无损拼接 / 转码拼接 / 批量排队　[{VERSION_TAG}]"
