"""声渡 SoundFerry —— 把 Windows 的声音（整机 / 指定应用）无线投到 AirPlay 音箱。

底层基于 pyatv 的 RAOP 实现（即 AirPlay 音频协议），
本项目在它之上加了实时 PCM 注入，因此不需要虚拟声卡、不需要 ffmpeg、也不落地任何临时文件。
"""

APP_NAME_ZH = "声渡"
APP_NAME_EN = "SoundFerry"
APP_NAME = f"{APP_NAME_ZH} {APP_NAME_EN}"
APP_DIR_NAME = "SoundFerry"  # 同时用作 %APPDATA% 配置目录名与安装后的文件夹名

__version__ = "1.5.0"
VERSION = __version__
