"""开机自启动（HKCU\\...\\Run，不需要管理员权限）。"""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Optional

LOGGER = logging.getLogger(__name__)

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
VALUE_NAME = "SoundFerry"
AUTOSTART_FLAG = "--autostart"


def _exe_command() -> str:
    if getattr(sys, "frozen", False):  # PyInstaller 打包后
        return f'"{sys.executable}" {AUTOSTART_FLAG}'
    # 源码运行时用 pythonw，避免弹出黑色控制台
    exe = Path(sys.executable)
    pythonw = exe.with_name("pythonw.exe")
    interpreter = pythonw if pythonw.exists() else exe
    script = Path(__file__).resolve().parent.parent / "app.py"
    return f'"{interpreter}" "{script}" {AUTOSTART_FLAG}'


def current_command() -> Optional[str]:
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            value, _ = winreg.QueryValueEx(key, VALUE_NAME)
            return str(value)
    except FileNotFoundError:
        return None
    except OSError:
        LOGGER.debug("读取自启动项失败", exc_info=True)
        return None


def is_enabled() -> bool:
    return current_command() is not None


def enable() -> bool:
    try:
        import winreg

        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
            winreg.SetValueEx(key, VALUE_NAME, 0, winreg.REG_SZ, _exe_command())
        return True
    except OSError:
        LOGGER.warning("写入自启动项失败", exc_info=True)
        return False


def disable() -> bool:
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
            winreg.DeleteValue(key, VALUE_NAME)
        return True
    except FileNotFoundError:
        return True
    except OSError:
        LOGGER.warning("删除自启动项失败", exc_info=True)
        return False
