"""验证 pywebview 跨线程 show/move/destroy 的可靠性（模拟托盘/HTTP 线程调用）。"""
import sys, threading, time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import webview
from PIL import ImageGrab

from soundferry.webui import UiServer

server = UiServer(8791)
threading.Thread(target=server.serve_forever, daemon=True).start()
time.sleep(4)

main_w = webview.create_window("声渡主窗口", server.url, width=600, height=800, x=200, y=80,
                               background_color="#edf0f7")
panel_w = webview.create_window("声渡快速调节", server.url.rstrip("/") + "/panel",
                                width=348, height=520, hidden=True, resizable=False, on_top=True,
                                x=1200, y=200, background_color="#edf0f7")

result = {}


def other_thread():
    """模拟托盘/HTTP 线程：显示面板 → 移动 → 截图 → 隐藏 → 显示主窗 → 销毁全部。"""
    time.sleep(3)
    try:
        panel_w.show()
        panel_w.move(1300, 150)
        result["show_panel"] = True
    except Exception as exc:  # noqa: BLE001
        result["show_panel"] = f"FAIL {exc}"
    time.sleep(3)
    try:
        img = ImageGrab.grab()
        img.resize((img.width // 3, img.height // 3)).save("shell_test2.png")
        result["screenshot"] = True
    except Exception as exc:  # noqa: BLE001
        result["screenshot"] = f"FAIL {exc}"
    try:
        panel_w.hide()
        result["hide_panel"] = True
    except Exception as exc:  # noqa: BLE001
        result["hide_panel"] = f"FAIL {exc}"
    try:
        main_w.hide()
        main_w.show()
        result["show_main"] = True
    except Exception as exc:  # noqa: BLE001
        result["show_main"] = f"FAIL {exc}"
    time.sleep(2)
    try:
        panel_w.destroy()
        main_w.destroy()
        result["destroy"] = True
    except Exception as exc:  # noqa: BLE001
        result["destroy"] = f"FAIL {exc}"


threading.Thread(target=other_thread, daemon=True).start()
webview.start(gui="edgechromium", private_mode=False)
print("loop exited; results:", result)
server.shutdown_all()
