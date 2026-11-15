"""原生窗口外壳：用 Windows 自带的 WebView2 内核承载界面。

界面还是原来那套 HTML（本地服务渲染），但窗口是真正的 Win32 窗口：
- **无边框**：没有系统标题栏，界面里自绘顶栏（可拖动、带窗口控制按钮）
- 尺寸按界面内容自适应，并受屏幕分辨率约束
- 通过 DWM 开启 Win11 原生圆角
- 关闭（✕）= 收进托盘；托盘或界面里退出才真正结束
- WebView2 运行时 Windows 10/11 系统自带，无需额外安装

WebView2 不可用时自动回退到原来的 Chrome 应用窗口方案。
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Optional

from . import APP_NAME

LOGGER = logging.getLogger(__name__)

PANEL_W, PANEL_H = 320, 118


def available() -> bool:
    """判断 WebView2 运行时是否存在（Win10/11 默认自带）。"""
    from pathlib import Path

    candidates = [
        Path(r"C:\Program Files (x86)\Microsoft\EdgeWebView\Application"),
        Path(r"C:\Program Files\Microsoft\EdgeWebView\Application"),
        Path(os_localappdata()) / "Microsoft/EdgeWebView/Application",
    ]
    return any(p.is_dir() for p in candidates)


def os_localappdata() -> str:
    import os

    return os.environ.get("LOCALAPPDATA", "")


class AppShell:
    """管理主窗口与快速面板两个原生窗口。"""

    def __init__(self, server) -> None:
        import webview

        self.server = server
        self._webview = webview
        self._quitting = False
        self._main = None
        self._panel = None
        # 面板状态：可见性 / 常驻置顶（图钉）/ 显示时间（防误判）
        self._panel_visible = False
        self._panel_pinned = False
        self._panel_shown_at = 0.0
        self._auto_hide_timer: Optional[threading.Timer] = None

        work_w, work_h, _scale, _dpi = _screen_dips()
        main_w = int(min(460, max(400, work_w * 0.34)))
        main_h = int(min(900, max(600, work_h * 0.9)))

        common = dict(
            background_color="#eef1f7",
            frameless=True,
            easy_drag=False,  # 拖动交给页面里的 pywebview-drag-region 区域
        )
        self._main = webview.create_window(
            APP_NAME,
            server.url,
            width=main_w,
            height=main_h,
            x=max(0, (work_w - main_w) // 2 + int(work_w * 0.1)),
            y=max(0, (work_h - main_h) // 2 - 10),
            **common,
        )
        self._panel = webview.create_window(
            f"{APP_NAME} · 快速调节",
            server.url.rstrip("/") + "/panel",
            width=PANEL_W,
            height=PANEL_H,
            hidden=True,
            resizable=False,
            on_top=True,
            x=max(12, work_w - PANEL_W - 16),
            y=max(12, work_h - PANEL_H - 12),
            **common,
        )
        self._main.events.closing += self._on_main_closing
        self._panel.events.closing += self._on_panel_closing

    # ---- 事件 -------------------------------------------------------------
    def _on_main_closing(self):
        if self._quitting:
            return None  # 允许真正关闭
        try:
            self._main.hide()  # 关闭 = 收进托盘
        except Exception:  # noqa: BLE001
            LOGGER.debug("隐藏主窗口失败", exc_info=True)
        return False

    def _on_panel_closing(self):
        if self._quitting:
            return None
        try:
            self._panel.hide()  # 面板「关闭」只是收起，可再次打开
        except Exception:  # noqa: BLE001
            LOGGER.debug("隐藏面板失败", exc_info=True)
        self._panel_visible = False
        self._panel_pinned = False
        return False

    # ---- 运行 -------------------------------------------------------------
    def run(self) -> bool:
        """阻塞运行界面循环；WebView2 启动失败返回 False（调用方回退 Chrome 方案）。"""
        try:
            self._webview.start(
                gui="edgechromium",
                func=self._after_start,
                private_mode=False,
                storage_path=_storage_path(),
            )
            return True
        except Exception:  # noqa: BLE001
            LOGGER.exception("原生窗口启动失败，回退浏览器方案")
            return False

    def _after_start(self) -> None:
        self._round_all()
        threading.Thread(target=self._panel_watcher, name="soundferry-panelwatch", daemon=True).start()
        LOGGER.info("原生窗口外壳已就绪")

    # ---- 快速面板：可见性 / 置顶 / 点击外部自动关闭 ------------------------
    def _panel_hwnd(self) -> int:
        """面板的原生窗口句柄（拿不到返回 0）。"""
        try:
            native = getattr(self._panel, "native", None)
            if native is not None and getattr(native, "Handle", None) is not None:
                return int(native.Handle.ToInt64())
        except Exception:  # noqa: BLE001
            pass
        # 兜底：按标题匹配底层窗体
        try:
            BV = self._webview.platforms.winforms.BrowserView
            title = f"{APP_NAME} · 快速调节"
            for bv in list(getattr(BV, "instances", {}).values()):
                native = getattr(bv, "pywebview_window", None)
                form = getattr(native, "native", None)
                if form is not None and getattr(form, "Text", "") == title:
                    return int(form.Handle.ToInt64())
        except Exception:  # noqa: BLE001
            pass
        return 0

    def _panel_watcher(self) -> None:
        """面板未置顶时，点击其他区域（前台窗口不是面板）就自动收起。"""
        import ctypes

        user32 = ctypes.windll.user32
        while not self._quitting:
            time.sleep(0.35)
            try:
                if not self._panel_visible or self._panel_pinned:
                    continue
                if time.monotonic() - self._panel_shown_at < 0.9:
                    continue  # 刚弹出，等窗口完成激活
                hwnd = self._panel_hwnd()
                if not hwnd:
                    continue
                fg = user32.GetForegroundWindow()
                # 前台窗口存在且不是面板 → 用户点了别处 → 收起
                if fg and fg != hwnd:
                    self.hide_panel()
            except Exception:  # noqa: BLE001
                LOGGER.debug("面板监视异常", exc_info=True)

    def set_panel_pinned(self, pinned: bool) -> None:
        self._panel_pinned = bool(pinned)
        if self._panel_pinned and self._auto_hide_timer is not None:
            self._auto_hide_timer.cancel()
            self._auto_hide_timer = None

    # ---- DWM 原生圆角 -----------------------------------------------------
    def _round_all(self) -> None:
        """给所有窗口开 Win11 圆角（DWMWA_WINDOW_CORNER_PREFERENCE）。"""
        import ctypes

        for form in self._native_forms():
            try:
                hwnd = int(form.Handle.ToInt64())
                pref = ctypes.c_int(2)  # DWMWCP_ROUND
                ctypes.windll.dwmapi.DwmSetWindowAttribute(
                    ctypes.c_void_p(hwnd), 33, ctypes.byref(pref), 4
                )
            except Exception:  # noqa: BLE001
                LOGGER.debug("设置圆角失败", exc_info=True)

    def _native_forms(self) -> list:
        """找到底层 WinForms 窗体对象（pywebview 内部结构，做兼容性兜底）。"""
        forms = []
        try:
            BV = self._webview.platforms.winforms.BrowserView
            forms.extend(list(getattr(BV, "instances", {}).values()))
        except Exception:  # noqa: BLE001
            pass
        if not forms:
            try:
                forms.extend(list(self._webview.windows.native or []))
            except Exception:  # noqa: BLE001
                pass
        return forms

    # ---- 尺寸（由页面按内容上报） -----------------------------------------
    def fit_main(self, width: int, height: int) -> None:
        try:
            work_w, work_h, _scale, _dpi = _screen_dips()
            width = int(max(360, min(width, work_w * 0.9)))
            height = int(max(480, min(height, work_h * 0.94)))
            self._main.resize(width, height)
        except Exception:  # noqa: BLE001
            LOGGER.debug("调整主窗口尺寸失败", exc_info=True)

    def fit_panel(self, width: int, height: int) -> None:
        try:
            work_w, work_h, _scale, _dpi = _screen_dips()
            width = int(max(260, min(width, work_w * 0.9)))
            height = int(max(80, min(height, work_h * 0.6)))
            self._panel.resize(width, height)
        except Exception:  # noqa: BLE001
            LOGGER.debug("调整面板尺寸失败", exc_info=True)

    # ---- 窗口控制（页面标题栏按钮 / JS 调用） -----------------------------
    def minimize_main(self) -> None:
        try:
            self._main.minimize()
        except Exception:  # noqa: BLE001
            LOGGER.debug("最小化失败", exc_info=True)

    def hide_main(self) -> None:
        try:
            self._main.hide()
        except Exception:  # noqa: BLE001
            LOGGER.debug("隐藏主窗口失败", exc_info=True)

    def hide_panel(self) -> None:
        try:
            self._panel.hide()
        except Exception:  # noqa: BLE001
            LOGGER.debug("隐藏面板失败", exc_info=True)
        self._panel_visible = False
        self._panel_pinned = False  # 收起后恢复「点击外部自动关闭」的默认行为
        if self._auto_hide_timer is not None:
            self._auto_hide_timer.cancel()
            self._auto_hide_timer = None

    # ---- 供其他线程调用 ---------------------------------------------------
    def show_main(self) -> None:
        try:
            self._main.show()
            self._main.restore()
        except Exception:  # noqa: BLE001
            LOGGER.exception("显示主窗口失败")

    def show_panel(self, auto_hide: float | None = None) -> None:
        """显示快速面板。

        auto_hide: 可选，N 秒后未置顶则自动收起（快捷键闪现反馈用）。
        """
        try:
            work_w, work_h, _scale, _dpi = _screen_dips()
            self._panel.move(max(12, work_w - PANEL_W - 16), max(12, work_h - PANEL_H - 12))
            self._panel.show()
            self._panel.restore()
        except Exception:  # noqa: BLE001
            LOGGER.exception("显示快速面板失败")
            return
        self._panel_visible = True
        self._panel_shown_at = time.monotonic()
        if self._auto_hide_timer is not None:
            self._auto_hide_timer.cancel()
            self._auto_hide_timer = None
        if auto_hide:

            def _later():
                if self._panel_visible and not self._panel_pinned:
                    self.hide_panel()

            t = threading.Timer(auto_hide, _later)
            t.daemon = True
            t.start()
            self._auto_hide_timer = t

    def quit_app(self) -> None:
        """真正退出：销毁窗口让 webview 循环返回。"""
        self._quitting = True
        for w in (self._panel, self._main):
            try:
                if w is not None:
                    w.destroy()
            except Exception:  # noqa: BLE001
                LOGGER.debug("销毁窗口失败", exc_info=True)


# ---------------------------------------------------------------------------
def _screen_dips() -> tuple[int, int, float, int]:
    from .webui import _screen_dips as _impl

    return _impl()


def _storage_path():
    import os
    from pathlib import Path

    from . import APP_DIR_NAME

    base = Path(os.environ.get("APPDATA", Path.home())) / APP_DIR_NAME / "webview"
    try:
        base.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    return str(base)
