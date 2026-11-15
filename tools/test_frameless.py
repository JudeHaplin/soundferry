"""验证：无边框窗口、pywebview-drag-region 拖拽、DWM 圆角、按内容自适应尺寸、截图。"""
import sys, threading, time, ctypes
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import webview

HTML = """
<!doctype html><html><head><meta charset="utf-8"><style>
html,body{margin:0;background:#eef1f7;font-family:"Microsoft YaHei UI",sans-serif;}
.bar{height:44px;display:flex;align-items:center;padding:0 14px;background:#fff;
  border-bottom:1px solid #e2e7f0;border-radius:16px 16px 0 0;}
.card{margin:12px;height:220px;background:#fff;border-radius:16px;display:flex;
  align-items:center;justify-content:center;color:#2f6bef;font-size:14px;}
</style></head><body>
<div class="bar pywebview-drag-region">拖这里可以移动窗口 ✋</div>
<div class="card">内容区（用于测试自适应高度）</div>
</body></html>
"""

w = webview.create_window("无边框测试", html=HTML, width=420, height=300, frameless=True,
                          easy_drag=False, background_color="#eef1f7", resizable=True)


def after_start():
    time.sleep(2.5)
    info = {}
    # 1) 找 HWND
    try:
        hwnd = w.gui.Handle.ToInt64() if hasattr(w.gui, "Handle") else None
        info["hwnd"] = hwnd
    except Exception as exc:  # noqa: BLE001
        info["hwnd_err"] = str(exc)
        hwnd = None
    # 2) DWM 圆角（Win11）
    if hwnd:
        try:
            pref = ctypes.c_int(2)  # DWMWCP_ROUND
            ctypes.windll.dwmapi.DwmSetWindowAttribute(ctypes.c_void_p(hwnd), 33, ctypes.byref(pref), 4)
            info["dwm_round"] = True
        except Exception as exc:  # noqa: BLE001
            info["dwm_round"] = str(exc)
    # 3) 按内容高度 resize
    try:
        h = w.evaluate_js("Math.ceil(document.documentElement.getBoundingClientRect().height)")
        info["content_h"] = h
        w.resize(420, int(h))
        info["resize"] = True
    except Exception as exc:  # noqa: BLE001
        info["resize"] = str(exc)
    time.sleep(1.5)
    try:
        from PIL import Image, ImageGrab
        img = ImageGrab.grab()
        img.crop((0, 0, 1400, 900)).save("frameless_test.png")
        info["shot"] = True
    except Exception as exc:  # noqa: BLE001
        info["shot"] = str(exc)
    print("RESULT:", info)
    time.sleep(1)
    w.destroy()


webview.start(after_start, gui="edgechromium", private_mode=False)
print("loop ended")
