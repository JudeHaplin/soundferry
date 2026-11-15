"""面板行为 GUI 测试：点击外部自动关闭 / 图钉置顶不关闭 / 快捷键反馈。"""
import sys, time, ctypes
sys.path.insert(0, ".")

from soundferry.webui import UiServer, SHELL
from soundferry.shell import AppShell

server = UiServer(8791)
server.start_hotkeys()
shell = AppShell(server)
import soundferry.webui as webui
webui.SHELL = shell
server.state.set_volume(46)

user32 = ctypes.windll.user32

def shot(name):
    try:
        ctypes.windll.user32.SetProcessDPIAware()
        from PIL import ImageGrab
        img = ImageGrab.grab()
        img.save(rf"C:\Users\JudeH\WorkBuddy\Work\XiaomiSoundAirPlay\{name}.png")
    except Exception as e:
        print("shot fail:", e)

def test():
    shell._after_start()  # 真实应用里由 shell.run() 调用：启动监视线程 + 圆角
    time.sleep(2.5)  # 等窗口创建
    print("== 1. 弹出面板 ==")
    shell.show_panel()
    time.sleep(1.2)
    print("visible:", shell._panel_visible)
    shot("panel_open")

    print("== 2. 焦点移到主窗口（模拟点击其他区域）→ 面板应自动收起 ==")
    shell.show_main()
    time.sleep(2.0)
    print("visible:", shell._panel_visible, "(expect False)")

    print("== 3. 再弹面板 + 置顶 → 焦点移走仍应保持 ==")
    shell.show_panel()
    time.sleep(1.0)
    shell.set_panel_pinned(True)
    shell.show_main()
    time.sleep(2.0)
    print("visible:", shell._panel_visible, "(expect True)")
    shot("panel_pinned")

    print("== 4. 取消置顶 → 焦点移走应自动收起 ==")
    shell.set_panel_pinned(False)
    shell.show_main()
    time.sleep(2.0)
    print("visible:", shell._panel_visible, "(expect False)")

    print("== 5. 快捷键音量+ 反馈（Ctrl+Shift+=）==")
    VK_CTRL, VK_SHIFT, VK_EQ = 0x11, 0x10, 0xBB
    user32.keybd_event(VK_CTRL, 0, 0, 0)
    user32.keybd_event(VK_SHIFT, 0, 0, 0)
    user32.keybd_event(VK_EQ, 0, 0, 0)
    time.sleep(0.1)
    user32.keybd_event(VK_EQ, 0, 2, 0)
    user32.keybd_event(VK_SHIFT, 0, 2, 0)
    user32.keybd_event(VK_CTRL, 0, 2, 0)
    time.sleep(1.2)
    print("volume:", server.state.volume, "(expect 51)")
    print("panel visible (flash):", shell._panel_visible, "(expect True)")
    shot("panel_flash")
    time.sleep(3.0)  # 等自动收起
    print("after 2.6s auto-hide:", shell._panel_visible, "(expect False)")

    print("ALL DONE")
    shell.quit_app()

import webview
webview.start(gui="edgechromium", func=test, private_mode=False,
              storage_path=r"C:\Users\JudeH\AppData\Roaming\SoundFerry\webview-test")
