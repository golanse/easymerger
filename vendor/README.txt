这个目录用来放 ffmpeg 组件（ffmpeg.exe / ffprobe.exe）。

来源有两种：
1）双击上级目录的「2_install_ffmpeg.bat」自动下载（软件会自动装到这里）
2）手动下载 https://github.com/BtbN/FFmpeg-Builds/releases
   取 ffmpeg-master-latest-win64-gpl.zip，解压后
   把 bin 目录里的 ffmpeg.exe 和 ffprobe.exe 复制到本目录的 bin\ 下

最终结构应该是：
    vendor\ffmpeg\bin\ffmpeg.exe
    vendor\ffmpeg\bin\ffprobe.exe

打包成 exe 时（3_build_exe.bat），这两个文件会被一起打进去，
生成的软件在任何电脑上都能直接用，不需要用户另外安装 ffmpeg。
