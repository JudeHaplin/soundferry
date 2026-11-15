"""系统托盘：声渡驻留在 Windows 状态栏里。

- 图标随连接状态变化：空闲（灰点）/ 连接中（黄点）/ 推流中（绿点）/ 出错（红点）
- 左键单击托盘图标：在桌面右下角弹出「快速音量调节」小面板
- 连接状态发生改变时：自动弹出小面板提醒用户
- 右键菜单：打开主界面 / 开始·停止推流 / 开机自启 / 退出
"""

from __future__ import annotations

import logging
import threading
import time
from pathlib import Path
from typing import Callable, Optional

from . import APP_NAME

LOGGER = logging.getLogger(__name__)


def _asset_path(name: str) -> Optional[Path]:
    import sys

    candidates = []
    frozen_base = getattr(sys, "_MEIPASS", None)
    if frozen_base:
        candidates.append(Path(frozen_base) / "assets" / name)
    candidates.append(Path(__file__).resolve().parent.parent / "assets" / name)
    candidates.append(Path.cwd() / "assets" / name)
    for path in candidates:
        if path.is_file():
            return path
    return None


class TrayController:
    """管理 pystray 托盘图标与快速面板。"""

    #: 状态变化后自动弹出面板的最小间隔（秒），避免抖动时连环弹窗
    POPUP_MIN_INTERVAL = 4.0

    def __init__(
        self,
        state,  # webui.AppState
        url: str,
        open_window: Callable[[str], None],
        open_panel: Callable[[str], None],
        on_quit: Callable[[], None],
    ) -> None:
        self.state = state
        self.url = url
        self._open_window = open_window
        self._open_panel = open_panel
        self._on_quit = on_quit

        self._icon = None
        self._images: dict[str, object] = {}
        self._state_key = ""
        self._last_popup = 0.0
        self._poll_stop = threading.Event()
        self._started = threading.Event()

    # ---- 资源 -------------------------------------------------------------
    def _load_images(self) -> bool:
        from PIL import Image

        ok = True
        for key in ("idle", "busy", "on", "err"):
            path = _asset_path(f"tray_{key}.png")
            if path is None:
                LOGGER.warning("缺少托盘图标资源 tray_%s.png", key)
                ok = False
                break
            self._images[key] = Image.open(path)
        if not ok:
            fallback = _asset_path("soundferry.ico")
            if fallback is not None:
                single = Image.open(fallback)
                self._images = {key: single for key in ("idle", "busy", "on", "err")}
                ok = True
        return ok and bool(self._images)

    # ---- 状态映射 ---------------------------------------------------------
    def _current_key(self) -> str:
        with self.state.lock:
            running = self.state.running
            status = self.state.status
        if running:
            return "on"
        if "出错" in status:
            return "err"
        if "正在" in status or "扫描" in status:
            return "busy"
        return "idle"

    _TITLES = {
        "idle": f"{APP_NAME} · 未连接",
        "busy": f"{APP_NAME} · 连接中…",
        "on": f"{APP_NAME} · 正在推流",
        "err": f"{APP_NAME} · 出错",
    }

    # ---- 启动 / 停止 ------------------------------------------------------
    def start(self) -> bool:
        try:
            import pystray
        except Exception:  # noqa: BLE001
            LOGGER.warning("pystray 不可用，托盘未启用", exc_info=True)
            return False
        if not self._load_images():
            return False

        try:
            self._icon = pystray.Icon(
                "SoundFerry",
                icon=self._images.get("idle"),
                title=self._TITLES["idle"],
                menu=self._build_menu(),
            )
            self._icon.run_detached()
        except Exception:  # noqa: BLE001
            LOGGER.warning("托盘启动失败", exc_info=True)
            self._icon = None
            return False

        threading.Thread(target=self._poll_loop, name="tray-poll", daemon=True).start()
        self._started.set()
        LOGGER.info("托盘已启动")
        return True

    def stop(self) -> None:
        self._poll_stop.set()
        icon, self._icon = self._icon, None
        if icon is not None:
            try:
                icon.stop()
            except Exception:  # noqa: BLE001
                LOGGER.debug("托盘停止失败", exc_info=True)

    @property
    def active(self) -> bool:
        return self._started.is_set() and self._icon is not None

    # ---- 菜单 -------------------------------------------------------------
    def _build_menu(self):
        import pystray

        def open_main():
            threading.Thread(target=lambda: self._open_window(self.url), daemon=True).start()

        def open_panel():
            self._popup_panel(force=True)

        def toggle_stream():
            def work():
                try:
                    with self.state.lock:
                        running = self.state.running
                    if running:
                        self.state.stop()
                    else:
                        self.state.refresh_devices()
                        time.sleep(0.4)
                        ok, message = self.state.start({})
                        if not ok:
                            LOGGER.info("托盘启动推流失败：%s", message)
                except Exception:  # noqa: BLE001
                    LOGGER.exception("托盘切换推流失败")

            threading.Thread(target=work, daemon=True).start()

        def toggle_autostart():
            from . import autostart

            def work():
                try:
                    if autostart.is_enabled():
                        autostart.disable()
                    else:
                        autostart.enable()
                    self.state._log("已从托盘切换开机自启动")
                except Exception:  # noqa: BLE001
                    LOGGER.exception("托盘切换开机自启失败")

            threading.Thread(target=work, daemon=True).start()

        def quit_app():
            threading.Thread(target=self._on_quit, daemon=True).start()

        def stream_label(item):
            with self.state.lock:
                return "停止推流" if self.state.running else "开始推流"

        def autostart_checked(item):
            from . import autostart

            try:
                return autostart.is_enabled()
            except Exception:  # noqa: BLE001
                return False

        return pystray.Menu(
            pystray.MenuItem("打开声渡主界面", lambda: open_main()),
            pystray.MenuItem("快速调节音量", lambda: open_panel(), default=True),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem(stream_label, lambda: toggle_stream()),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("开机自启动", lambda: toggle_autostart(), checked=autostart_checked),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("退出声渡", lambda: quit_app()),
        )

    # ---- 状态轮询 ---------------------------------------------------------
    def _poll_loop(self) -> None:
        import time as _time

        while not self._poll_stop.wait(0.8):
            try:
                key = self._current_key()
            except Exception:  # noqa: BLE001
                continue
            if key != self._state_key:
                previous = self._state_key
                self._state_key = key
                icon = self._icon
                if icon is not None:
                    try:
                        icon.icon = self._images[key]
                        icon.title = self._TITLES[key]
                        icon.update_menu()
                    except Exception:  # noqa: BLE001
                        LOGGER.debug("更新托盘图标失败", exc_info=True)
                # 连接状态发生改变（开始推流 / 停止推流 / 出错）→ 弹出快速面板提醒
                if previous in ("", "busy") and key == "busy":
                    continue  # 瞬态不弹
                if key in ("on", "err") or (previous == "on" and key != "on"):
                    self._popup_panel()
                elif previous and previous != key:
                    self._popup_panel()

    # ---- 快速面板 ---------------------------------------------------------
    def _popup_panel(self, force: bool = False) -> None:
        now = time.monotonic()
        if not force and now - self._last_popup < self.POPUP_MIN_INTERVAL:
            return
        self._last_popup = now
        LOGGER.info("弹出快速音量面板")
        try:
            self._open_panel(self.url)
        except Exception:  # noqa: BLE001
            LOGGER.exception("弹出快速面板失败")
