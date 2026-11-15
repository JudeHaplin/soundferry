"""声渡 SoundFerry —— 程序入口。

双击运行会打开界面；带 ``--autostart`` 时静默在后台待命（开机自启用）。
如果已经有实例在跑，再双击只会把界面窗口重新打开，不会重复启动服务。
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import threading
import traceback
from pathlib import Path
from typing import Optional

if not getattr(sys, "frozen", False):
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from soundferry import APP_DIR_NAME, APP_NAME, APP_NAME_ZH, __version__  # noqa: E402

CONFIG_DIR = Path(os.environ.get("APPDATA", Path.home())) / APP_DIR_NAME
INSTANCE_FILE = CONFIG_DIR / "instance.json"
LOG_FILE = CONFIG_DIR / "logs" / "app.log"


def _fatal(title: str, message: str) -> None:
    try:
        import ctypes

        ctypes.windll.user32.MessageBoxW(None, message, title, 0x10)
    except Exception:  # noqa: BLE001
        print(f"{title}: {message}")


def _setup_logging() -> None:
    handlers: list[logging.Handler] = []
    try:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(LOG_FILE, encoding="utf-8"))
    except OSError:
        pass
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=handlers or None,
    )


def _running_instance_url() -> Optional[str]:
    """返回已有实例的界面地址（没有则 None）。"""
    try:
        data = json.loads(INSTANCE_FILE.read_text(encoding="utf-8"))
        pid = int(data["pid"])
        port = int(data["port"])
    except Exception:  # noqa: BLE001
        return None
    try:
        import psutil

        if not psutil.pid_exists(pid):
            return None
    except Exception:  # noqa: BLE001
        return None
    try:
        import urllib.request

        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/ping", timeout=1.2) as resp:
            if resp.status == 200:
                return f"http://127.0.0.1:{port}/"
    except Exception:  # noqa: BLE001
        return None
    return None


def _write_instance(port: int) -> None:
    try:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        INSTANCE_FILE.write_text(
            json.dumps({"pid": os.getpid(), "port": port, "version": __version__}), encoding="utf-8"
        )
    except OSError:
        pass


def _clear_instance() -> None:
    try:
        INSTANCE_FILE.unlink(missing_ok=True)
    except OSError:
        pass


def _main() -> int:
    parser = argparse.ArgumentParser(description=f"{APP_NAME} · AirPlay 无线音频")
    parser.add_argument("--no-window", action="store_true", help="不打开界面窗口，只启动服务")
    parser.add_argument("--autostart", action="store_true", help="开机自启：后台静默运行")
    parser.add_argument("--port", type=int, default=0, help="指定端口，默认自动选择")
    args = parser.parse_args()

    _setup_logging()
    logging.getLogger("soundferry").info("启动 %s v%s (frozen=%s)", APP_NAME, __version__, getattr(sys, "frozen", False))

    try:
        from soundferry.webui import UiServer, launch_panel, launch_window
    except ModuleNotFoundError as exc:
        _fatal("无法启动", f"缺少依赖：{exc.name}\n\n请重新安装本软件。")
        return 1

    # 已有实例：叫出它的主窗口（原生外壳优先，失败退回浏览器窗口）
    existing = _running_instance_url()
    if existing:
        if not args.no_window:
            try:
                import urllib.request

                port = existing.rsplit(":", 1)[1].rstrip("/")
                req = urllib.request.Request(
                    f"http://127.0.0.1:{port}/api/show-main", data=b"{}", method="POST"
                )
                urllib.request.urlopen(req, timeout=2)
            except Exception:  # noqa: BLE001
                launch_window(existing)
        return 0

    try:
        server = UiServer(args.port, watchdog=not args.autostart)
        _write_instance(server.port)

        # 系统托盘：图标随连接状态变化，左键弹出快速音量面板。
        # 托盘启动失败时退回老行为（界面关掉且空闲就自动退出）。
        tray = None
        try:
            from soundferry.tray import TrayController

            tray = TrayController(
                server.state,
                server.url,
                open_window=launch_window,
                open_panel=launch_panel,
                on_quit=server.shutdown_all,
            )
            if tray.start():
                server.has_tray = True
                server.on_shutdown.append(tray.stop)
            else:
                tray = None
        except Exception:  # noqa: BLE001
            logging.getLogger("soundferry").warning("托盘初始化失败", exc_info=True)
            tray = None

        # 全局快捷键（音量/静音/快速面板）：尽早启动，任何窗口形态下都可用
        try:
            server.start_hotkeys()
        except Exception:  # noqa: BLE001
            logging.getLogger("soundferry").warning("全局快捷键初始化失败", exc_info=True)

        if args.autostart:
            server.start_auto_connect()

        # 原生窗口外壳（WebView2）：优先使用；失败回退 Chrome 应用窗口方案。
        if not args.no_window:
            try:
                import soundferry.shell as shell_mod
                import soundferry.webui as webui_mod

                if shell_mod.available():
                    webui_mod.SHELL = shell_mod.AppShell(server)
                    # HTTP 服务挪到后台线程；有原生窗口+托盘驻留，不需要空闲退出
                    server._watchdog = False
                    server.on_shutdown.append(webui_mod.SHELL.quit_app)
                    threading.Thread(target=server.serve_forever, daemon=True).start()
                    if webui_mod.SHELL.run():
                        # 原生窗口循环退出 = 程序结束（quit 时钩子已停托盘/引擎）
                        server.shutdown_all()
                        _clear_instance()
                        return 0
                    logging.getLogger("soundferry").warning("原生外壳未能启动，回退浏览器窗口方案")
                    webui_mod.SHELL = None
            except Exception:  # noqa: BLE001
                logging.getLogger("soundferry").warning("原生外壳初始化失败，回退浏览器方案", exc_info=True)
                try:
                    import soundferry.webui as webui_mod

                    webui_mod.SHELL = None
                except Exception:  # noqa: BLE001
                    pass

        open_window = not args.no_window and not args.autostart
        if open_window:
            import time

            def open_later() -> None:
                time.sleep(0.8)
                launch_window(server.url)

            threading.Thread(target=open_later, daemon=True).start()

        try:
            server.serve_forever()
        finally:
            if tray is not None:
                tray.stop()
            _clear_instance()
        return 0
    except Exception:  # noqa: BLE001
        _clear_instance()
        _fatal(f"{APP_NAME_ZH} 异常", traceback.format_exc()[-1500:])
        return 1


if __name__ == "__main__":
    raise SystemExit(_main())
