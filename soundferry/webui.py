"""本地界面服务：一个极小的 HTTP 服务 + 单页界面。

之所以不用 tkinter：精简版 Python 没有 tkinter，而网页界面反而更好看、更好维护。
启动时用 Chrome/Edge 的「应用窗口」模式打开，没有地址栏，看起来就是一个独立软件。
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import threading
import time
import webbrowser
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Optional

from . import APP_DIR_NAME, APP_NAME, APP_NAME_EN, APP_NAME_ZH, __version__
from . import autostart
from .audio import list_output_devices
from .engine import LATENCY_PRESETS, StreamEngine, recover_stale_mute, scan_devices
from .hotkeys import DEFAULT_HOTKEYS, HOTKEY_LABELS
from .proc_loopback import find_pids_by_exe, list_audio_apps, recover_stale_quiet

LOGGER = logging.getLogger(__name__)

CONFIG_DIR = Path(os.environ.get("APPDATA", Path.home())) / APP_DIR_NAME
CONFIG_FILE = CONFIG_DIR / "config.json"
LEGACY_DIR = Path(os.environ.get("APPDATA", Path.home())) / "XiaomiSoundAirPlay"

# 原生窗口外壳（soundferry.shell.AppShell）。由 app.py 在启动时注入；
# 有外壳时所有「开窗口」的请求都走原生窗口，否则回退 Chrome 应用窗口。
SHELL = None

LATENCY_OPTIONS = [
    {"label": "稳定 1.5s", "value": 1.5},
    {"label": "均衡 1.0s", "value": 1.0},
    {"label": "低延迟 0.6s", "value": 0.6},
]

# ---- 主题 -------------------------------------------------------------------
# 「有风」对齐 WorkBuddy 森林绿水彩主题（窗外有风，心里有光）。
THEMES = [
    {
        "id": "wind",
        "label": "有风",
        "preview": ["#8fce9d", "#e8f0e9"],
        "desc": "窗外有风，心里有光",
    },
    {"id": "mist", "label": "晨雾", "preview": ["#7ea6f5", "#e9efff"], "desc": "清晨海雾的蓝"},
    {"id": "night", "label": "夜航", "preview": ["#5b8dff", "#1c212b"], "desc": "深夜航行"},
    {"id": "windnight", "label": "夜风", "preview": ["#59ac65", "#18211a"], "desc": "夜里也有风"},
]

DEFAULT_HOTKEY_IDS = ["vol_up", "vol_down", "mute", "panel"]


def load_config() -> dict:
    for path in (CONFIG_FILE, LEGACY_DIR / "config.json"):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
        except Exception:  # noqa: BLE001
            continue
    return {}


def save_config(data: dict) -> None:
    try:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        CONFIG_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:  # noqa: BLE001
        LOGGER.debug("保存配置失败", exc_info=True)


class AppState:
    """线程安全的共享状态。"""

    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.config = load_config()
        recover_stale_mute()
        recover_stale_quiet()

        self.status = "准备就绪"
        self.level = 0
        self.running = False
        self.scanning = False
        self.devices: list[dict[str, Any]] = []
        self.sources: list[dict[str, Any]] = []
        self.apps: list[dict[str, Any]] = []
        self.logs: deque[dict[str, str]] = deque(maxlen=120)

        self.selected_device = self.config.get("device_identifier")
        self.mode = str(self.config.get("capture_mode", "device"))
        self.selected_source = self.config.get("capture_index")
        self.selected_app = self.config.get("app_exe")
        self.volume = int(self.config.get("volume", 100))
        self.latency = float(self.config.get("latency", 1.0))
        self.mute_local = bool(self.config.get("mute_local", True))
        self.auto_connect = bool(self.config.get("auto_connect", False))
        self.theme = str(self.config.get("theme", "wind"))
        if self.theme not in {t["id"] for t in THEMES}:
            self.theme = "wind"
        self.hotkeys_enabled = bool(self.config.get("hotkeys_enabled", True))
        self._hotkey_bindings: dict[str, str] = {}
        self._last_volume = int(self.config.get("last_volume", 60))

        self.last_poll = time.monotonic()
        self.client_seen = False

        self.engine = StreamEngine(
            on_status=self._on_status,
            on_level=self._on_level,
            on_error=self._on_error,
        )

    # ---- 引擎回调 -------------------------------------------------------
    def _log(self, text: str) -> None:
        stamp = time.strftime("%H:%M:%S")
        with self.lock:
            self.logs.append({"t": stamp, "text": text})

    def _on_status(self, text: str) -> None:
        with self.lock:
            self.status = text
        self._log(text)

    def _on_level(self, level: int) -> None:
        with self.lock:
            self.level = max(0, min(100, int(level)))

    def _on_error(self, text: str) -> None:
        with self.lock:
            self.status = f"出错：{text}"
            self.running = False
        self._log(f"[错误] {text}")

    # ---- 列表刷新 -------------------------------------------------------
    def refresh_devices(self, quiet: bool = False) -> None:
        with self.lock:
            if self.scanning:
                return
            self.scanning = True
        if not quiet:
            self._log("正在扫描局域网内的 AirPlay 设备…")

        def work() -> None:
            devices: list[dict[str, Any]] = []
            try:
                for item in scan_devices():
                    devices.append(
                        {
                            "name": item.name,
                            "address": item.address,
                            "identifier": item.identifier,
                        }
                    )
            except Exception as exc:  # noqa: BLE001
                self._log(f"[错误] 扫描失败：{exc}")
            with self.lock:
                sig = "|".join(d["identifier"] for d in devices)
                changed = sig != getattr(self, "_device_sig", None)
                self._device_sig = sig
                self.devices = devices
                self.scanning = False
                if devices and not self.selected_device:
                    self.selected_device = devices[0]["identifier"]
            if not quiet or changed:
                self._log(
                    f"发现 {len(devices)} 台设备。" if devices
                    else "没有发现设备，请确认电脑和音箱在同一个 Wi-Fi。"
                )

        threading.Thread(target=work, daemon=True).start()

    def refresh_sources(self) -> None:
        def work() -> None:
            try:
                sources = [
                    {
                        "index": device.index,
                        "name": device.name,
                        "friendly": device.friendly_name,
                        "sample_rate": device.sample_rate,
                        "channels": device.channels,
                        "is_default": device.is_default,
                    }
                    for device in list_output_devices()
                ]
            except Exception as exc:  # noqa: BLE001
                self._log(f"[错误] 读取音频设备失败：{exc}")
                return
            with self.lock:
                self.sources = sources
                if self.selected_source is None or not any(
                    source["index"] == self.selected_source for source in sources
                ):
                    default = next((s for s in sources if s["is_default"]), sources[0] if sources else None)
                    self.selected_source = default["index"] if default else None

        threading.Thread(target=work, daemon=True).start()

    def refresh_apps(self, quiet: bool = False) -> None:
        def work() -> None:
            try:
                apps = [
                    {"exe": item["exe"], "label": item["label"], "playing": bool(item.get("playing"))}
                    for item in list_audio_apps()
                ]
            except Exception as exc:  # noqa: BLE001
                self._log(f"[错误] 读取应用列表失败：{exc}")
                return
            with self.lock:
                self.apps = apps
                if self.selected_app and not any(
                    app["exe"].lower() == str(self.selected_app).lower() for app in apps
                ):
                    # 记住的应用当前没运行，仍然保留选择，避免用户设置被清掉
                    pass
                if not self.selected_app and apps:
                    playing = next((a for a in apps if a["playing"]), None)
                    self.selected_app = (playing or apps[0])["exe"]
            if not quiet:
                self._log(f"发现 {len(apps)} 个可投送的应用。")

        threading.Thread(target=work, daemon=True).start()

    # ---- 动作 -----------------------------------------------------------
    def _resolve_app(self, exe: str) -> Optional[tuple[int, str]]:
        target = (exe or "").lower()
        if not target:
            return None
        apps = []
        try:
            apps = list_audio_apps()
        except Exception:  # noqa: BLE001
            LOGGER.debug("枚举应用失败", exc_info=True)
        for item in apps:
            if item["exe"].lower() == target and item.get("pid"):
                return int(item["pid"]), item["exe"]
        pids = find_pids_by_exe(exe)
        if pids:
            return pids[0], exe
        return None

    def start(self, payload: dict[str, Any]) -> tuple[bool, str]:
        with self.lock:
            if self.running:
                return False, "已经在推流了"

            device = payload.get("device") or self.selected_device
            mode = str(payload.get("mode") or self.mode or "device")
            if not device:
                return False, "请先选择目标音箱"

            latency = float(payload.get("latency", self.latency))
            # 每次连接都把音箱音量重置为 1%，防止突然炸响；之后用滑杆调节
            volume = 1
            mute = bool(payload.get("mute", self.mute_local))

            capture_spec: dict[str, Any] = {"mode": mode}
            if mode == "app":
                app_exe = str(payload.get("app") or self.selected_app or "")
                if not app_exe:
                    return False, "请先选择要投送的应用"
            else:
                source = payload.get("source")
                if source is None:
                    source = self.selected_source
                if source is None:
                    return False, "请先选择声音来源"

            self.selected_device = device
            self.mode = mode
            self.latency = latency
            self.volume = volume
            self.mute_local = mute
            self.running = True
            self.status = "正在启动…"

        if mode == "app":
            resolved = self._resolve_app(app_exe)
            if resolved is None:
                with self.lock:
                    self.running = False
                    self.status = "准备就绪"
                return False, f"「{Path(app_exe).stem}」当前没有运行，请先打开它再试"
            pid, real_name = resolved
            capture_spec.update({"pid": pid, "name": real_name})
            with self.lock:
                self.selected_app = app_exe
                self.config.update(
                    {
                        "device_identifier": device,
                        "capture_mode": mode,
                        "app_exe": app_exe,
                        "latency": latency,
                        "volume": volume,
                        "mute_local": mute,
                    }
                )
                save_config(self.config)
            self._log(f"开始推流：只投送「{real_name}」（延迟 {latency:g}s，音量 {volume}%）")
        else:
            with self.lock:
                self.selected_source = int(source)
                self.config.update(
                    {
                        "device_identifier": device,
                        "capture_mode": mode,
                        "capture_index": int(source),
                        "latency": latency,
                        "volume": volume,
                        "mute_local": mute,
                    }
                )
                save_config(self.config)
            capture_spec["index"] = int(source)
            self._log(f"开始推流：整个电脑（延迟 {latency:g}s，音量 {volume}%）")

        self.engine.start(
            device,
            capture_spec,
            latency_s=latency,
            volume=volume,
            mute_local=mute,
        )
        return True, "ok"

    def stop(self) -> tuple[bool, str]:
        with self.lock:
            if not self.running:
                return False, "当前没有在推流"
            self.status = "正在停止…"
        self._log("停止推流")
        threading.Thread(target=self._stop_worker, daemon=True).start()
        return True, "ok"

    def _stop_worker(self) -> None:
        self.engine.stop()
        self.engine.wait(timeout=10)
        with self.lock:
            self.running = False
            self.status = "已停止"
            self.level = 0

    def set_volume(self, volume: int) -> None:
        volume = max(0, min(100, int(volume)))
        with self.lock:
            self.volume = volume
            self.config["volume"] = volume
            if volume > 0:
                self._last_volume = volume
                self.config["last_volume"] = volume
            running = self.running
        if running:
            self.engine.set_volume(volume)

    def set_settings(self, payload: dict[str, Any]) -> None:
        """保存「下次启动才生效」的偏好。"""
        with self.lock:
            if "mute" in payload:
                self.mute_local = bool(payload["mute"])
                self.config["mute_local"] = self.mute_local
            if "latency" in payload:
                self.latency = float(payload["latency"])
                self.config["latency"] = self.latency
            if "device" in payload and payload["device"]:
                self.selected_device = str(payload["device"])
                self.config["device_identifier"] = self.selected_device
            if "source" in payload and payload["source"] is not None:
                self.selected_source = int(payload["source"])
                self.config["capture_index"] = self.selected_source
            if "mode" in payload and payload["mode"] in ("device", "app"):
                self.mode = str(payload["mode"])
                self.config["capture_mode"] = self.mode
            if "app" in payload and payload["app"]:
                self.selected_app = str(payload["app"])
                self.config["app_exe"] = self.selected_app
            if "auto_connect" in payload:
                self.auto_connect = bool(payload["auto_connect"])
                self.config["auto_connect"] = self.auto_connect
            if "theme" in payload and str(payload["theme"]) in {t["id"] for t in THEMES}:
                self.theme = str(payload["theme"])
                self.config["theme"] = self.theme
            save_config(self.config)

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            self.last_poll = time.monotonic()
            self.client_seen = True
            return {
                "app_name": APP_NAME,
                "app_name_zh": APP_NAME_ZH,
                "version": __version__,
                "native": SHELL is not None,  # 原生无边框窗口（决定是否显示自绘窗口控制按钮）
                "status": self.status,
                "level": self.level,
                "running": self.running,
                "scanning": self.scanning,
                "devices": self.devices,
                "sources": self.sources,
                "apps": self.apps,
                "logs": list(self.logs),
                "selected_device": self.selected_device,
                "selected_source": self.selected_source,
                "selected_app": self.selected_app,
                "mode": self.mode,
                "volume": self.volume,
                "latency": self.latency,
                "mute_local": self.mute_local,
                "autostart": autostart.is_enabled(),
                "auto_connect": self.auto_connect,
                "latency_options": LATENCY_OPTIONS,
                "theme": self.theme,
                "themes": THEMES,
                "hotkeys_enabled": self.hotkeys_enabled,
                "hotkeys": dict(self._hotkey_bindings),
                "hotkey_labels": HOTKEY_LABELS,
                "panel_pinned": bool(getattr(SHELL, "_panel_pinned", False)),
            }


class Handler(BaseHTTPRequestHandler):
    state: AppState
    server_ref: "UiServer | None" = None

    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A003
        LOGGER.debug("http: " + fmt, *args)

    # ---- 工具 -----------------------------------------------------------
    def _send(self, code: int, body: bytes, content_type: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionAbortedError):
            pass

    def _json(self, data: dict[str, Any], code: int = 200) -> None:
        self._send(code, json.dumps(data, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

    def _read_json(self) -> dict[str, Any]:
        try:
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b"{}"
            return json.loads(raw.decode("utf-8")) if raw else {}
        except Exception:  # noqa: BLE001
            return {}

    # ---- 路由 -----------------------------------------------------------
    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/" or self.path.startswith("/index"):
            self._send(200, PAGE.encode("utf-8"), "text/html; charset=utf-8")
            return
        if self.path.startswith("/panel"):
            self._send(200, PANEL_PAGE.encode("utf-8"), "text/html; charset=utf-8")
            return
        if self.path.startswith("/favicon.ico") or self.path.startswith("/icon.ico"):
            from .tray import _asset_path

            path = _asset_path("soundferry.ico")
            if path is not None:
                self._send(200, path.read_bytes(), "image/x-icon")
                return
        if self.path.startswith("/api/state"):
            self._json(self.state.snapshot())
            return
        if self.path.startswith("/api/ping"):
            self._json({"app": APP_NAME_EN, "version": __version__, "pid": os.getpid()})
            return
        self._json({"error": "not found"}, 404)

    def do_POST(self) -> None:  # noqa: N802
        path = self.path.split("?")[0]
        payload = self._read_json()

        if path == "/api/scan":
            self.state.refresh_devices()
            self._json({"ok": True})
        elif path == "/api/scan-sources":
            self.state.refresh_sources()
            self._json({"ok": True})
        elif path == "/api/scan-apps":
            self.state.refresh_apps()
            self._json({"ok": True})
        elif path == "/api/start":
            ok, message = self.state.start(payload)
            self._json({"ok": ok, "message": message})
        elif path == "/api/stop":
            ok, message = self.state.stop()
            self._json({"ok": ok, "message": message})
        elif path == "/api/volume":
            self.state.set_volume(int(payload.get("volume", 100)))
            self._json({"ok": True})
        elif path == "/api/open-panel":
            # 从主界面里唤出右下角快速音量面板（与托盘左键同一个窗口）
            threading.Thread(target=lambda: launch_panel(self.server_ref.url), daemon=True).start()
            self._json({"ok": True})
        elif path == "/api/show-main":
            # 第二个实例请求正在运行的实例显示主窗口
            threading.Thread(target=lambda: launch_window(self.server_ref.url), daemon=True).start()
            self._json({"ok": True})
        elif path == "/api/win/fit":
            # 页面按内容上报期望尺寸（无边框窗口自适应）
            if SHELL is not None:
                kind = payload.get("kind", "main")
                try:
                    if kind == "panel":
                        SHELL.fit_panel(int(payload.get("w", 320)), int(payload.get("h", 118)))
                    else:
                        SHELL.fit_main(int(payload.get("w", 440)), int(payload.get("h", 800)))
                except Exception:  # noqa: BLE001
                    pass
            self._json({"ok": True})
        elif path == "/api/win/minimize":
            if SHELL is not None:
                SHELL.minimize_main()
            self._json({"ok": True})
        elif path == "/api/win/hide":
            # 标题栏 ✕：收进托盘（等价于点托盘关闭）
            if SHELL is not None:
                SHELL.hide_main()
            self._json({"ok": True})
        elif path == "/api/win/hide-panel":
            if SHELL is not None:
                SHELL.hide_panel()
            self._json({"ok": True})
        elif path == "/api/panel/pin":
            # 图钉开关：置顶常驻（点击外部不关闭）/ 取消置顶
            if SHELL is not None:
                SHELL.set_panel_pinned(bool(payload.get("pinned")))
            self._json({"ok": True, "pinned": bool(getattr(SHELL, "_panel_pinned", False))})
        elif path == "/api/hotkey":
            self._handle_hotkey(payload)
        elif path == "/api/hotkeys/enabled":
            enabled = bool(payload.get("enabled"))
            self.state.hotkeys_enabled = enabled
            self.state.config["hotkeys_enabled"] = enabled
            save_config(self.state.config)
            if self.server_ref is not None and self.server_ref.hotkey_manager is not None:
                self.server_ref.hotkey_manager.update(
                    self.state._hotkey_bindings, enabled=enabled
                )
            self.state._log("全局快捷键已开启" if enabled else "全局快捷键已关闭")
            self._json({"ok": True})
        elif path == "/api/hotkeys/reset":
            self.state._hotkey_bindings = dict(DEFAULT_HOTKEYS)
            self.state.config["hotkeys"] = {}
            save_config(self.state.config)
            errors = {}
            if self.server_ref is not None and self.server_ref.hotkey_manager is not None:
                errors = self.server_ref.hotkey_manager.update(dict(DEFAULT_HOTKEYS))
            self.state._log("快捷键已恢复默认")
            self._json({"ok": not errors, "errors": errors})
        elif path == "/api/settings":
            self.state.set_settings(payload)
            self._json({"ok": True})
        elif path == "/api/autostart":
            enabled = bool(payload.get("enabled"))
            ok = autostart.enable() if enabled else autostart.disable()
            self.state._log(
                ("已开启开机自启动" if enabled else "已关闭开机自启动")
                if ok
                else "[错误] 修改开机自启动失败（可能被安全软件拦截）"
            )
            self._json({"ok": ok, "autostart": autostart.is_enabled()})
        elif path == "/api/quit":
            self._json({"ok": True})
            if self.server_ref is not None:
                threading.Thread(target=self.server_ref.shutdown_all, daemon=True).start()
        else:
            self._json({"error": "not found"}, 404)

    def _handle_hotkey(self, payload: dict[str, Any]) -> None:
        from .hotkeys import parse_combo

        hk_id = str(payload.get("id", ""))
        keys = str(payload.get("keys", ""))
        if hk_id not in DEFAULT_HOTKEY_IDS:
            self._json({"ok": False, "message": "未知的快捷键"})
            return
        if parse_combo(keys) is None:
            self._json({"ok": False, "message": "无效的组合键：需要 Ctrl / Alt / Win 修饰键"})
            return
        clash = next(
            (i for i, v in self.state._hotkey_bindings.items() if v == keys and i != hk_id),
            None,
        )
        if clash is not None:
            self._json(
                {
                    "ok": False,
                    "message": f"这个组合键已用于「{HOTKEY_LABELS.get(clash, clash)}」，请换一个",
                }
            )
            return
        server = self.server_ref
        old = dict(self.state._hotkey_bindings)
        new = {**old, hk_id: keys}
        errors: dict[str, str] = {}
        if server is not None and server.hotkey_manager is not None:
            errors = server.hotkey_manager.update(new)
        if hk_id in errors:
            # 注册失败（被占用）：回滚到旧绑定
            if server is not None and server.hotkey_manager is not None:
                server.hotkey_manager.update(old)
            self._json({"ok": False, "message": errors[hk_id]})
            return
        self.state._hotkey_bindings = new
        self.state.config["hotkeys"] = {k: v for k, v in new.items() if DEFAULT_HOTKEYS.get(k) != v}
        save_config(self.state.config)
        self.state._log(f"快捷键已设置：「{HOTKEY_LABELS.get(hk_id, hk_id)}」→ {keys}")
        self._json({"ok": True})


class UiServer:
    def __init__(self, port: int = 0, watchdog: bool = True) -> None:
        self.state = AppState()
        Handler.state = self.state
        Handler.server_ref = self
        self.httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
        self.httpd.daemon_threads = True
        self.port = self.httpd.server_address[1]
        self._stop = threading.Event()
        self._watchdog = watchdog
        self._auto_scan = True  # 空闲时静默周期扫描设备/应用
        self.has_tray = False  # 托盘存活时界面关掉也不自动退出
        self.on_shutdown: list[Callable[[], None]] = []  # 退出时额外清理（如停托盘）
        self.hotkey_manager = None  # app.py 里调用 start_hotkeys() 注入

    # ---- 全局快捷键 ------------------------------------------------------
    def start_hotkeys(self):
        from .hotkeys import DEFAULT_HOTKEYS, HotkeyManager

        manager = HotkeyManager(self.hotkey_action)
        self.state._hotkey_bindings = {
            **DEFAULT_HOTKEYS,
            **(self.state.config.get("hotkeys") or {}),
        }
        manager.start()  # 先起线程（消息循环就绪）
        errors = manager.update(self.state._hotkey_bindings, enabled=self.state.hotkeys_enabled)
        if errors:
            LOGGER.warning("部分全局快捷键注册失败：%s", errors)
        self.hotkey_manager = manager
        self.on_shutdown.append(manager.stop)
        return manager

    def hotkey_action(self, name: str) -> None:
        state = self.state
        if name in ("vol_up", "vol_down"):
            with state.lock:
                v = state.volume
            state.set_volume(v + (5 if name == "vol_up" else -5))
            self._flash_panel()
        elif name == "mute":
            with state.lock:
                v = state.volume
            if v > 0:
                state.set_volume(0)
            else:
                state.set_volume(getattr(state, "_last_volume", 60))
            self._flash_panel()
        elif name == "panel":
            shell = SHELL
            if shell is None:
                return
            visible = getattr(shell, "_panel_visible", False)
            pinned = getattr(shell, "_panel_pinned", False)
            if visible and not pinned:
                shell.hide_panel()
            else:
                shell.show_panel()

    def _flash_panel(self) -> None:
        """快捷键调音量时的视觉反馈：面板没开就闪现 2.6 秒。"""
        shell = SHELL
        if shell is None:
            return
        if getattr(shell, "_panel_visible", False):
            return
        shell.show_panel(auto_hide=2.6)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/"

    def serve_forever(self) -> None:
        self.state.refresh_devices()
        self.state.refresh_sources()
        self.state.refresh_apps()
        if self._watchdog:
            threading.Thread(target=self._idle_watchdog, daemon=True).start()
        if self._auto_scan:
            threading.Thread(target=self._auto_scan_loop, daemon=True).start()
        self.httpd.serve_forever(poll_interval=0.4)

    def _auto_scan_loop(self) -> None:
        """空闲时静默周期扫描：设备/应用有变化才写日志（AirFlash 式自动发现）。"""
        cycle = 0
        while not self._stop.wait(20):
            cycle += 1
            with self.state.lock:
                running = self.state.running
            if running:
                continue
            self.state.refresh_devices(quiet=True)
            self.state.refresh_apps(quiet=True)
            if cycle % 5 == 0:
                self.state.refresh_sources()

    def _idle_watchdog(self) -> None:
        """界面关掉后，如果没在推流就自动退出，避免后台残留进程。

        有托盘图标时不算「残留」——软件是刻意驻留的，不自动退出。
        """
        while not self._stop.is_set():
            time.sleep(5)
            if self.has_tray:
                return
            with self.state.lock:
                idle = time.monotonic() - self.state.last_poll
                seen = self.state.client_seen
                running = self.state.running
            if seen and idle > 180 and not running:
                LOGGER.info("界面已关闭，退出程序")
                self.shutdown_all()
                return

    def shutdown_all(self) -> None:
        if self._stop.is_set():
            return
        self._stop.set()
        for hook in list(self.on_shutdown):
            try:
                hook()
            except Exception:  # noqa: BLE001
                LOGGER.debug("退出清理钩子失败", exc_info=True)
        try:
            self.state.engine.stop()
            self.state.engine.wait(timeout=5)
        finally:
            self.httpd.shutdown()

    # ---- 开机后自动连接 --------------------------------------------------
    def start_auto_connect(self, delay: float = 10.0) -> None:
        """开机自启时，如果用户勾选了「自动开始推流」，就等网络就绪后再连。"""

        def work() -> None:
            time.sleep(delay)
            with self.state.lock:
                enabled = self.state.auto_connect and not self.state.running
                device = self.state.selected_device
                mode = self.state.mode
            if not enabled or not device:
                return
            for attempt in range(3):
                ok, message = self.state.start({})
                if ok:
                    return
                time.sleep(8)
                LOGGER.info("自动连接第 %d 次失败：%s", attempt + 1, message)

        threading.Thread(target=work, daemon=True).start()


def find_browser() -> Optional[str]:
    """找一个支持 --app 模式的 Chromium 浏览器。"""
    home = Path(os.environ.get("LOCALAPPDATA", ""))
    candidates = [
        Path(os.environ.get("PROGRAMFILES", r"C:\Program Files")) / "Google/Chrome/Application/chrome.exe",
        Path(os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)")) / "Google/Chrome/Application/chrome.exe",
        home / "Google/Chrome/Application/chrome.exe",
        Path(os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)")) / "Microsoft/Edge/Application/msedge.exe",
        Path(os.environ.get("PROGRAMFILES", r"C:\Program Files")) / "Microsoft/Edge/Application/msedge.exe",
        home / "Microsoft/Edge/Application/msedge.exe",
    ]
    for path in candidates:
        if path.is_file():
            return str(path)
    return None


def _screen_dips() -> tuple[int, int, int, int]:
    """返回屏幕工作区（去掉任务栏）的宽高，单位为 DIP（逻辑像素）。

    Windows 显示缩放（125%/150%…）下，Chrome 的 --window-position/size 以
    逻辑像素解释，所以这里把物理像素换算回 DIP。
    """
    import ctypes

    try:
        user32 = ctypes.windll.user32
        try:
            user32.SetProcessDPIAware()
        except Exception:  # noqa: BLE001
            pass
        dpi = 96
        try:
            dpi = user32.GetDpiForSystem() or 96
        except Exception:  # noqa: BLE001
            pass
        scale = dpi / 96.0

        class RECT(ctypes.Structure):
            _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                        ("right", ctypes.c_long), ("bottom", ctypes.c_long)]

        rect = RECT()
        # SPI_GETWORKAREA = 0x0030
        if user32.SystemParametersInfoW(0x0030, 0, ctypes.byref(rect), 0):
            work_w, work_h = rect.right, rect.bottom
        else:
            work_w = user32.GetSystemMetrics(0)
            work_h = user32.GetSystemMetrics(1)
        return int(work_w / scale), int(work_h / scale), scale, dpi
    except Exception:  # noqa: BLE001
        return 1920, 1040, 1.0, 96


def _run_app_window(url: str, width: int, height: int, *, anchor: str = "center") -> None:
    """用 Chrome/Edge 应用窗口模式打开页面，按分辨率自动定位。"""
    browser = find_browser()
    work_w, work_h, _scale, _dpi = _screen_dips()
    width = max(300, min(width, work_w - 24))
    height = max(360, min(height, work_h - 24))
    if anchor == "br":  # 右下角：贴着任务栏上方，像系统通知面板
        x = max(12, work_w - width - 18)
        y = max(12, work_h - height - 14)
    else:  # 居中偏右
        x = max(0, (work_w - width) // 2 + int(work_w * 0.08))
        y = max(0, (work_h - height) // 2 - 10)
    if browser:
        try:
            subprocess.Popen(
                [
                    browser,
                    f"--app={url}",
                    f"--window-size={width},{height}",
                    f"--window-position={x},{y}",
                    "--no-first-run",
                    "--no-default-browser-check",
                ],
                close_fds=True,
            )
            return
        except Exception:  # noqa: BLE001
            LOGGER.debug("应用窗口启动失败，退回默认浏览器", exc_info=True)
    webbrowser.open(url)


def launch_window(url: str) -> None:
    """主界面：优先用原生窗口外壳，否则 Chrome 应用窗口。"""
    if SHELL is not None:
        try:
            SHELL.show_main()
            return
        except Exception:  # noqa: BLE001
            LOGGER.exception("原生窗口显示失败，回退浏览器")
    work_w, work_h, _scale, _dpi = _screen_dips()
    width = int(min(600, max(540, work_w * 0.45)))
    height = int(min(840, max(720, work_h * 0.92)))
    _run_app_window(url, width, height, anchor="center")


def launch_panel(url: str) -> None:
    """快速音量面板：优先原生窗口，否则 Chrome 应用窗口（右下角）。"""
    if SHELL is not None:
        try:
            SHELL.show_panel()
            return
        except Exception:  # noqa: BLE001
            LOGGER.exception("原生面板显示失败，回退浏览器")
    _run_app_window(url.rstrip("/") + "/panel", 348, 500, anchor="br")


def run(open_window: bool = True) -> None:
    server = UiServer()
    if open_window:
        threading.Thread(target=lambda: (time.sleep(0.6), launch_window(server.url)), daemon=True).start()
    LOGGER.info("界面地址: %s", server.url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.shutdown_all()


# ---------------------------------------------------------------------------
# 界面本体（内嵌，避免额外文件与路径问题）
# 设计语言对齐 AirFlash：浅灰蓝底 + 白色圆角卡片、单一蓝色强调、扁平无渐变。
# 窗口为无边框原生窗：页面自绘标题栏（可拖动 + 窗口控制按钮），
# 宽度收紧贴合卡片内容，高度按内容自适应上报给外壳（/api/win/fit）。
# ---------------------------------------------------------------------------
PAGE = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>声渡 SoundFerry</title>
<link rel="icon" href="/favicon.ico">
<style>
  /* ---- 主题（data-theme 切换） ----
     有风 = WorkBuddy 森林绿水彩（窗外有风，心里有光）；晨雾 = 经典海蓝 */
  :root{
    --radius:16px; --tile:46px;
    --bg:#f6f8f5; --bg-top:#e4f0e5; --bg-soft:#e8f0e9; --card:#ffffff;
    --line:#dce7dd; --text:#12261a; --text-2:#41544a; --muted:#7f8c81;
    --accent:#3c8c4e; --accent-dark:#1f7237; --accent-soft:#e2efe3;
    --track:#d7e3d8; --hover-soft:rgba(18,38,26,.07);
    --tile-a:#59ac65; --tile-b:#2f7a41; --pulse:60,140,78;
    --hint-bg:#f0f6ee; --hint-line:#d7e8d8;
    --ok:var(--accent); --err:#d94a45; --warn:#c77700;
  }
  :root[data-theme="mist"]{
    --bg:#eef1f7; --bg-top:#e4eaf5; --bg-soft:#eef1f7; --card:#ffffff;
    --line:#e4e8f1; --text:#1a2030; --text-2:#525b6e; --muted:#8b93a5;
    --accent:#2f6bef; --accent-dark:#2657c8; --accent-soft:#e9efff;
    --track:#dde3ee; --hover-soft:rgba(26,32,48,.07);
    --tile-a:#3f7bff; --tile-b:#2657c8; --pulse:47,107,239;
    --hint-bg:#fff7e8; --hint-line:#ffe7bd;
  }
  :root[data-theme="night"]{
    --bg:#14171f; --bg-top:#171b25; --bg-soft:#1a1f2a; --card:#1c212c;
    --line:#2a3140; --text:#e9edf5; --text-2:#b7c0ce; --muted:#7e8798;
    --accent:#5b8dff; --accent-dark:#3f6fd6; --accent-soft:rgba(91,141,255,.16);
    --track:#2a3140; --hover-soft:rgba(233,237,245,.08);
    --tile-a:#5b8dff; --tile-b:#3f6fd6; --pulse:91,141,255;
    --hint-bg:#26200e; --hint-line:#453a15;
  }
  :root[data-theme="windnight"]{
    --bg:#10150f; --bg-top:#131a12; --bg-soft:#161f16; --card:#18211a;
    --line:#26352a; --text:#e6efe6; --text-2:#b7c8ba; --muted:#7e8f83;
    --accent:#59ac65; --accent-dark:#3c8c4e; --accent-soft:rgba(89,172,101,.16);
    --track:#26352a; --hover-soft:rgba(230,239,230,.08);
    --tile-a:#59ac65; --tile-b:#2f7a41; --pulse:89,172,101;
    --hint-bg:#231f0c; --hint-line:#403613;
  }
  *{box-sizing:border-box; -webkit-tap-highlight-color:transparent;}
  html{font-size:clamp(12px, calc(8.6px + .34vw), 16.5px);}
  body{margin:0; background:var(--bg); color:var(--text); overflow-x:hidden;
    font-family:"SF Pro Text","MiSans","HarmonyOS Sans SC","Segoe UI Variable Text","Segoe UI","Microsoft YaHei UI",system-ui,sans-serif;
    -webkit-font-smoothing:antialiased;}
  /* 顶部一点主题色氛围（有风主题像被窗外的光照进来） */
  body::before{content:""; position:fixed; left:0; right:0; top:0; height:230px;
    background:linear-gradient(180deg, var(--bg-top), transparent); pointer-events:none; z-index:-1;}

  /* ---- 自绘标题栏（无边框窗口） ---- */
  .titlebar{height:2.6rem; display:flex; align-items:center; gap:.55rem; padding:0 .5rem 0 .8rem;
    user-select:none;}
  .tb-logo{width:1.5rem; height:1.5rem; border-radius:.45rem; background:var(--accent);
    display:flex; align-items:center; justify-content:center; flex:none;}
  .tb-logo svg{width:.95rem; height:.95rem;}
  .tb-name{font-size:.82rem; font-weight:600; letter-spacing:.01em;}
  .tb-dot{width:.42rem; height:.42rem; border-radius:50%; background:var(--track); margin-left:.3rem; flex:none;}
  .tb-dot.on{background:var(--ok); animation:tbpulse 1.8s infinite;}
  .tb-dot.busy{background:var(--warn); animation:tbpulse 1.2s infinite;}
  .tb-dot.err{background:var(--err);}
  @keyframes tbpulse{0%{box-shadow:0 0 0 0 rgba(var(--pulse),.4);}70%{box-shadow:0 0 0 .35rem rgba(var(--pulse),0);}100%{box-shadow:0 0 0 0 rgba(var(--pulse),0);}}
  .tb-space{flex:1; height:100%;}
  .winbtn{width:2.2rem; height:2.2rem; border:0; background:transparent; color:var(--muted);
    border-radius:.55rem; display:flex; align-items:center; justify-content:center;
    cursor:pointer; padding:0; transition:background .12s, color .12s;}
  .winbtn:hover{background:var(--hover-soft); color:var(--text);}
  .winbtn.close:hover{background:#e81123; color:#fff;}
  .winbtn svg{width:.9rem; height:.9rem;}

  .wrap{padding:.15rem .95rem 1rem;}

  /* ---- 卡片 ---- */
  .card{background:var(--card); border:1px solid var(--line); border-radius:var(--radius);
    padding:1.05rem 1.1rem; margin-bottom:.85rem;
    box-shadow:0 1px 2px rgba(23,32,54,.04), 0 10px 26px -20px rgba(30,50,120,.4);}
  .row{display:flex; align-items:center; gap:.75rem;}
  .grow{flex:1; min-width:0;}

  /* ---- 接收器卡片 ---- */
  .rc-top{display:flex; align-items:center; gap:.8rem;}
  .tile{width:var(--tile); height:var(--tile); border-radius:.85rem;
    background:linear-gradient(140deg, var(--tile-a), var(--tile-b)); color:#fff; flex:none;
    display:flex; align-items:center; justify-content:center;
    box-shadow:0 4px 10px -4px rgba(var(--pulse),.55); transition:filter .2s;}
  .tile.dim{background:var(--accent-soft); color:var(--accent); box-shadow:none;}
  .tile svg{width:1.5rem; height:1.5rem;}
  .rc-name{font-size:.95rem; font-weight:600; line-height:1.25;}
  .rc-status{font-size:.78rem; color:var(--muted); margin-top:.14rem;}
  .rc-status.live{color:var(--accent); font-weight:500;}
  .play{width:var(--tile); height:var(--tile); border-radius:50%; border:1.5px solid var(--accent);
    background:var(--card); color:var(--accent); cursor:pointer; flex:none;
    display:flex; align-items:center; justify-content:center; transition:all .15s; padding:0;}
  .play:hover{background:var(--accent-soft); transform:scale(1.04);}
  .play.on{background:var(--accent); color:#fff; border-color:var(--accent);}
  .play:disabled{opacity:.45; cursor:not-allowed;}
  .play svg{width:1.2rem; height:1.2rem;}
  .rc-vol{display:flex; align-items:center; gap:.7rem; margin-top:.95rem;}
  .vol-num{font-size:.86rem; font-weight:600; color:var(--text-2); width:2.6rem; text-align:right;
    font-variant-numeric:tabular-nums; flex:none;}
  button.chip{border:1px solid var(--line); background:var(--card); color:var(--text-2);
    border-radius:99px; padding:.4rem .8rem; font-size:.76rem; cursor:pointer; font-family:inherit;
    flex:none; transition:all .15s;}
  button.chip:hover{border-color:var(--accent); color:var(--accent); background:var(--accent-soft);}
  button.chip.active{background:var(--accent); border-color:var(--accent); color:#fff;}
  button.chip:disabled{opacity:.45; cursor:not-allowed;}
  input[type=range]{width:100%; -webkit-appearance:none; height:1.5rem; background:transparent; margin:0; cursor:pointer;}
  input[type=range]::-webkit-slider-runnable-track{height:.35rem; border-radius:99px;
    background:linear-gradient(to right, var(--accent) var(--p,100%), var(--track) var(--p,100%));}
  input[type=range]::-webkit-slider-thumb{-webkit-appearance:none; width:1.1rem; height:1.1rem; border-radius:50%;
    background:var(--accent); margin-top:-.38rem; box-shadow:0 1px 3px rgba(var(--pulse),.4); transition:transform .12s;}
  input[type=range]:active::-webkit-slider-thumb{transform:scale(1.2);}

  /* ---- 表单 ---- */
  select{width:100%; appearance:none; border:1px solid var(--line); background:var(--bg-soft);
    border-radius:.65rem; padding:.5rem 2rem .5rem .75rem; font-size:.85rem; color:var(--text);
    font-family:inherit; cursor:pointer; text-overflow:ellipsis; transition:border-color .15s;
    background-image:url("data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' width='12' height='12' viewBox='0 0 12 12'><path d='M2 4.5L6 8.5L10 4.5' fill='none' stroke='%238b93a5' stroke-width='1.7' stroke-linecap='round'/></svg>");
    background-repeat:no-repeat; background-position:right .7rem center;}
  .rc-name select{margin:-.25rem 0 -.25rem -.4rem; max-width:100%; background:var(--bg-soft);
    font-weight:600; font-size:.92rem;}
  select:hover{border-color:var(--accent);}
  select:focus{outline:none; border-color:var(--accent);}
  select:disabled{opacity:.55; cursor:not-allowed;}
  .field{display:flex; align-items:center; gap:.7rem;}
  .field + .field{margin-top:.75rem;}
  label.tag{font-size:.82rem; color:var(--text-2); flex:none;}
  .seg{display:flex; background:var(--bg-soft); border-radius:.7rem; padding:.2rem; gap:.2rem; flex:1;}
  .seg button{flex:1; border:0; background:transparent; color:var(--text-2); font-size:.8rem;
    padding:.45rem .3rem; border-radius:.55rem; cursor:pointer; font-family:inherit; transition:all .15s; white-space:nowrap;}
  .seg button.active{background:var(--card); color:var(--accent); font-weight:600;
    box-shadow:0 1px 3px rgba(23,32,54,.12);}
  .seg button:disabled{cursor:not-allowed; opacity:.6;}
  button.mini{border:1px solid var(--line); background:var(--card); color:var(--text-2); border-radius:.6rem;
    padding:.45rem .75rem; font-size:.76rem; cursor:pointer; font-family:inherit; flex:none; transition:all .15s;}
  button.mini:hover{border-color:var(--accent); color:var(--accent); background:var(--accent-soft);}
  button.mini:disabled{opacity:.45; cursor:not-allowed;}
  .hint{font-size:.76rem; color:var(--warn); line-height:1.6; margin-top:.8rem; display:none;
    background:var(--hint-bg); border:1px solid var(--hint-line); border-radius:.65rem; padding:.5rem .75rem;}
  .hint.show{display:block;}

  /* ---- 开关 ---- */
  .switch{position:relative; width:2.4rem; height:1.4rem; flex:none;}
  .switch input{opacity:0; width:0; height:0;}
  .slider{position:absolute; inset:0; background:#d5dae3; border-radius:99px; transition:.2s; cursor:pointer;}
  .slider:before{content:""; position:absolute; width:1.05rem; height:1.05rem; left:.17rem; top:.17rem;
    background:#fff; border-radius:50%; transition:.2s; box-shadow:0 1px 3px rgba(0,0,0,.25);}
  .switch input:checked + .slider{background:var(--accent);}
  .switch input:checked + .slider:before{transform:translateX(1rem);}
  .sw-label{font-size:.84rem; color:var(--text);}
  .sw-sub{font-size:.7rem; color:var(--muted); margin-top:.12rem;}

  /* ---- 主题选择器 ---- */
  .swatches{display:flex; gap:.45rem; flex-wrap:wrap;}
  .swatch{display:flex; align-items:center; gap:.42rem; border:1px solid var(--line);
    background:var(--card); border-radius:.65rem; padding:.32rem .62rem .32rem .36rem;
    cursor:pointer; font-family:inherit; font-size:.78rem; color:var(--text-2); transition:all .15s;}
  .swatch:hover{border-color:var(--accent); color:var(--accent);}
  .swatch.active{border-color:var(--accent); color:var(--accent); background:var(--accent-soft); font-weight:600;}
  .swatch .dotc{width:1.05rem; height:1.05rem; border-radius:50%; flex:none;
    background:linear-gradient(135deg, var(--c1), var(--c2)); box-shadow:inset 0 0 0 1px rgba(0,0,0,.1);}

  /* ---- 全局快捷键 ---- */
  .hk-row{display:flex; align-items:center; gap:.6rem; margin-top:.6rem;}
  .hk-row:first-child{margin-top:.15rem;}
  .hk-label{font-size:.82rem; color:var(--text); flex:1; min-width:0;}
  .hk-key{border:1px solid var(--line); background:var(--bg-soft); color:var(--text-2);
    border-radius:.55rem; padding:.42rem .7rem; font-size:.76rem; font-family:inherit; cursor:pointer;
    min-width:9.6rem; text-align:center; transition:all .15s; white-space:nowrap; user-select:none;}
  .hk-key:hover{border-color:var(--accent); color:var(--accent);}
  .hk-key.listen{border-color:var(--accent); color:var(--accent); background:var(--accent-soft);
    animation:hkblink 1s infinite;}
  @keyframes hkblink{50%{opacity:.5;}}
  .hk-key.err{border-color:var(--err); color:var(--err);}

  /* ---- 底部状态 ---- */
  .statusbar{padding:.2rem .2rem 0;}
  .status-main{display:flex; align-items:center; gap:.5rem;}
  .dot{width:.5rem; height:.5rem; border-radius:50%; background:var(--track); flex:none;}
  .dot.on{background:var(--ok); animation:pulse 1.8s infinite;}
  .dot.busy{background:var(--warn); animation:pulse 1.2s infinite;}
  .dot.err{background:var(--err);}
  @keyframes pulse{0%{box-shadow:0 0 0 0 rgba(var(--pulse),.4);}70%{box-shadow:0 0 0 .45rem rgba(var(--pulse),0);}100%{box-shadow:0 0 0 0 rgba(var(--pulse),0);}}
  .status-text{font-size:.88rem; font-weight:600; color:var(--text);}
  .status-text.live{color:var(--accent);}
  .meter{flex:1; height:.3rem; background:var(--track); border-radius:99px; overflow:hidden; margin-left:.6rem;}
  .meter > i{display:block; height:100%; width:0%; background:var(--accent);
    border-radius:99px; transition:width .18s ease-out;}
  .status-sub{font-size:.73rem; color:var(--muted); margin-top:.3rem; padding-left:1rem;}

  /* ---- 日志 ---- */
  details.log{border:1px solid var(--line); border-radius:var(--radius); background:var(--card);
    margin-bottom:.85rem; overflow:hidden; box-shadow:0 1px 2px rgba(23,32,54,.04);}
  details.log summary{padding:.6rem 1.05rem; font-size:.76rem; color:var(--muted); cursor:pointer;
    list-style:none; letter-spacing:.08em; user-select:none;}
  details.log summary::-webkit-details-marker{display:none;}
  details.log summary:hover{color:var(--accent);}
  details.log summary::before{content:"▸ ";}
  details.log[open] summary::before{content:"▾ ";}
  #log{height:6rem; overflow-y:auto; padding:0 1.05rem .7rem;
    font-family:"Cascadia Mono",Consolas,monospace; font-size:.72rem; line-height:1.8; color:var(--muted);}
  #log div{white-space:pre-wrap; word-break:break-all;}
  #log .t{color:var(--track); margin-right:.4rem;}
  #log .e{color:var(--err);}
  .foot{display:flex; justify-content:space-between; align-items:center;
    font-size:.68rem; color:var(--muted); padding:0 .25rem .2rem;}
  .foot button{border:0; background:transparent; color:var(--muted); cursor:pointer;
    font-size:.68rem; font-family:inherit; text-decoration:underline; padding:0;}
  .foot button:hover{color:var(--err);}
</style>
</head>
<body>
  <!-- 无边框窗口的自绘标题栏：整条可拖动，右侧是窗口控制按钮 -->
  <div class="titlebar pywebview-drag-region">
    <div class="tb-logo">
      <svg viewBox="0 0 24 24" fill="none"><path d="M12 5l6.5 9.5H5.5L12 5z" fill="#fff"/><path d="M3.5 18c1.6-1.1 3.2-1.1 4.8 0s3.2 1.1 4.8 0 3.2-1.1 4.8 0" stroke="#fff" stroke-width="1.6" stroke-linecap="round" opacity=".9"/><path d="M5.5 21c1.5-1 3-1 4.5 0s3 1 4.5 0 3-1 4.5 0" stroke="#fff" stroke-width="1.6" stroke-linecap="round" opacity=".55"/></svg>
    </div>
    <span class="tb-name">声渡</span>
    <span class="tb-dot" id="tbdot"></span>
    <div class="tb-space"></div>
    <button class="winbtn" id="winmin" title="最小化" style="display:none;">
      <svg viewBox="0 0 12 12" fill="none"><path d="M2 6h8" stroke="currentColor" stroke-width="1.4" stroke-linecap="round"/></svg>
    </button>
    <button class="winbtn close" id="winclose" title="收进托盘（程序继续运行）" style="display:none;">
      <svg viewBox="0 0 12 12" fill="none"><path d="M2.5 2.5l7 7M9.5 2.5l-7 7" stroke="currentColor" stroke-width="1.4" stroke-linecap="round"/></svg>
    </button>
  </div>

<div class="wrap">
  <!-- 目标音箱（接收器卡片） -->
  <div class="card">
    <div class="rc-top">
      <div class="tile dim" id="tile">
        <svg viewBox="0 0 24 24" fill="none"><rect x="8.2" y="3" width="7.6" height="18" rx="2.6" stroke="currentColor" stroke-width="1.7"/><circle cx="12" cy="8" r="1.6" stroke="currentColor" stroke-width="1.5"/><circle cx="12" cy="14.5" r="2.6" stroke="currentColor" stroke-width="1.5"/></svg>
      </div>
      <div class="grow">
        <div class="rc-name"><select id="device"></select></div>
        <div class="rc-status" id="rcstatus">未连接</div>
      </div>
      <button class="play" id="go" title="开始推流">
        <svg id="goicon" viewBox="0 0 24 24" fill="currentColor"><path d="M8.5 5.8v12.4c0 .8.9 1.3 1.6.9l9.2-6.2c.6-.4.6-1.4 0-1.8L10.1 4.9c-.7-.4-1.6.1-1.6.9z"/></svg>
      </button>
    </div>
    <div class="rc-vol">
      <input type="range" id="volume" min="0" max="100" step="1" value="1">
      <span class="vol-num" id="volnum">1%</span>
      <button class="chip" id="mute2">静音</button>
    </div>
    <div class="hint" id="hint"></div>
  </div>

  <!-- 声音来源 -->
  <div class="card">
    <div class="field">
      <label class="tag">声音来源</label>
      <div class="seg" id="modeseg">
        <button data-mode="device">整个电脑</button>
        <button data-mode="app">指定应用</button>
      </div>
    </div>
    <div class="field" id="row-device">
      <label class="tag">输出设备</label>
      <div class="grow"><select id="source"></select></div>
      <button class="mini" id="resrc">刷新</button>
    </div>
    <div class="field" id="row-app" style="display:none;">
      <label class="tag">应用</label>
      <div class="grow"><select id="app"></select></div>
      <button class="mini" id="resapp">刷新</button>
    </div>
  </div>

  <!-- 偏好 -->
  <div class="card">
    <div class="field">
      <label class="tag">主题</label>
      <div class="grow swatches" id="themes"></div>
    </div>
    <div class="field">
      <label class="tag">缓冲延迟</label>
      <div class="grow"><select id="latency"></select></div>
    </div>
    <div class="field">
      <label class="tag">电脑本机</label>
      <div class="grow">
        <div class="sw-label" id="mute-label">推流时静音电脑本机</div>
        <div class="sw-sub" id="mute-sub">声音只从音箱出，电脑不出声</div>
      </div>
      <label class="switch"><input type="checkbox" id="mute" checked><span class="slider"></span></label>
    </div>
    <div class="field">
      <label class="tag">启动</label>
      <div class="grow">
        <div class="sw-label">开机自动启动并驻留托盘</div>
        <div class="sw-sub" id="autoconn-sub">开机后自动开始推流可再次开启</div>
      </div>
      <label class="switch"><input type="checkbox" id="autostart"><span class="slider"></span></label>
    </div>
    <div class="field" id="row-autoconn" style="display:none;">
      <label class="tag"></label>
      <div class="grow"><div class="sw-label">开机后自动开始推流</div><div class="sw-sub">启动约 10 秒后自动连接上次的音箱</div></div>
      <label class="switch"><input type="checkbox" id="autoconn"><span class="slider"></span></label>
    </div>
  </div>

  <!-- 全局快捷键 -->
  <div class="card">
    <div class="field">
      <label class="tag">全局快捷键</label>
      <div class="grow">
        <div class="sw-label">在游戏、看片时也能直接调</div>
        <div class="sw-sub" id="hk-sub">点右边的组合键框后，按下你想用的按键</div>
      </div>
      <label class="switch"><input type="checkbox" id="hks" checked><span class="slider"></span></label>
    </div>
    <div id="hkrows"></div>
    <div class="hk-row">
      <span class="hk-label" style="color:var(--muted); font-size:.74rem;">调音量时会顺带闪现右下角面板</span>
      <button class="mini" id="hkreset">恢复默认</button>
    </div>
  </div>

  <!-- 底部状态 -->
  <div class="statusbar">
    <div class="status-main">
      <span class="dot" id="dot"></span>
      <span class="status-text" id="status">准备就绪</span>
      <div class="meter"><i id="meter"></i></div>
    </div>
    <div class="status-sub" id="rcsub">未选择目标音箱</div>
  </div>

  <details class="log">
    <summary>运行日志</summary>
    <div id="log"></div>
  </details>

  <div class="foot">
    <span id="ver">声渡 SoundFerry · AirPlay / RAOP</span>
    <span>关闭窗口后驻留托盘 · <button id="quit">退出程序</button></span>
  </div>
</div>

<script>
const $ = (id) => document.getElementById(id);
let STATE = null;
let latency = 1.0;
let running = false;
let mode = "device";
let deviceSig = "", sourceSig = "", appSig = "";
let volumeDirty = false;
let lastVol = 1;
let lastFitH = 0;

const ICON_PLAY = '<path d="M8.5 5.8v12.4c0 .8.9 1.3 1.6.9l9.2-6.2c.6-.4.6-1.4 0-1.8L10.1 4.9c-.7-.4-1.6.1-1.6.9z"/>';
const ICON_STOP = '<rect x="7" y="7" width="10" height="10" rx="2"/>';

/* ---- 无边框窗口：按内容自适应尺寸 ---- */
function fitWindow(){
  try{
    const h = Math.ceil(document.documentElement.getBoundingClientRect().height)
            + Math.ceil(parseFloat(getComputedStyle(document.body).paddingTop || 0));
    if (Math.abs(h - lastFitH) > 6){
      lastFitH = h;
      post("/api/win/fit", {kind: "main", w: 440, h: h});
    }
  }catch(e){}
}

/* ---- 窗口控制按钮 ---- */
$("winmin").onclick = () => post("/api/win/minimize");
$("winclose").onclick = () => post("/api/win/hide");

function paintVolume(v){
  $("volnum").textContent = v + "%";
  $("volume").style.setProperty("--p", Math.min(100, v) + "%");
  if (v > 0) lastVol = v;
  $("mute2").textContent = (v === 0) ? "取消静音" : "静音";
  $("mute2").classList.toggle("active", v === 0);
}

function buildLatency(){
  const sel = $("latency");
  sel.innerHTML = "";
  (STATE.latency_options || []).forEach(opt => {
    const o = document.createElement("option");
    o.value = opt.value;
    o.textContent = opt.label;
    sel.appendChild(o);
  });
  sel.value = String(latency);
}

function setOptions(el, items, selected, placeholder){
  el.innerHTML = "";
  if (!items.length){
    const o = document.createElement("option");
    o.textContent = placeholder;
    o.value = "";
    el.appendChild(o);
    return;
  }
  items.forEach(item => {
    const o = document.createElement("option");
    o.value = item.value;
    o.textContent = item.label;
    el.appendChild(o);
  });
  if (selected !== undefined && selected !== null) el.value = String(selected);
}

/* ---- 主题 ---- */
function syncTheme(){
  if (STATE.theme) document.documentElement.dataset.theme = STATE.theme;
  const wrap = $("themes");
  if (!wrap.children.length){
    (STATE.themes || []).forEach(t => {
      const b = document.createElement("button");
      b.className = "swatch";
      b.dataset.id = t.id;
      b.title = t.desc || "";
      const dot = document.createElement("span");
      dot.className = "dotc";
      dot.style.setProperty("--c1", t.preview[0]);
      dot.style.setProperty("--c2", t.preview[1]);
      b.appendChild(dot);
      b.appendChild(document.createTextNode(t.label));
      b.onclick = () => post("/api/settings", {theme: t.id});
      wrap.appendChild(b);
    });
  }
  [...wrap.children].forEach(b => b.classList.toggle("active", b.dataset.id === STATE.theme));
}

/* ---- 全局快捷键 ---- */
const HK_CODES = {
  ArrowUp:"↑", ArrowDown:"↓", ArrowLeft:"←", ArrowRight:"→",
  Space:"空格", Enter:"回车", Backspace:"退格", Home:"Home", End:"End",
  PageUp:"PgUp", PageDown:"PgDn", Insert:"Ins", Delete:"Del",
  Semicolon:";", Equal:"=", Comma:",", Minus:"-", Period:".", Slash:"/",
  Backquote:"`", BracketLeft:"[", Backslash:"\\", BracketRight:"]", Quote:"'",
  NumpadAdd:"Num+", NumpadSubtract:"Num-", NumpadMultiply:"Num×",
  NumpadDivide:"Num÷", NumpadDecimal:"Num."
};
function prettyCombo(c){
  return String(c).split("+").map(p => {
    if (HK_CODES[p]) return HK_CODES[p];
    if (p.startsWith("Key")) return p.slice(3);
    if (p.startsWith("Digit")) return p.slice(5);
    if (/^Numpad\d$/.test(p)) return "Num" + p.slice(6);
    return p;
  }).join(" + ");
}
let hkListen = null;
let hkSig = "";
function syncHotkeys(){
  const sig = JSON.stringify(STATE.hotkeys || {}) + "#" + (STATE.hotkeys_enabled ? 1 : 0);
  if (sig === hkSig) return;
  hkSig = sig;
  const rows = $("hkrows");
  rows.innerHTML = "";
  (STATE.hotkeys_enabled ? ["vol_up","vol_down","mute","panel"] : []).forEach(id => {
    const combo = (STATE.hotkeys || {})[id] || "";
    const row = document.createElement("div");
    row.className = "hk-row";
    const label = document.createElement("span");
    label.className = "hk-label";
    label.textContent = (STATE.hotkey_labels || {})[id] || id;
    const btn = document.createElement("button");
    btn.className = "hk-key";
    btn.dataset.id = id;
    btn.textContent = combo ? prettyCombo(combo) : "未设置";
    btn.title = "点击后按下新组合键（Esc 取消）";
    btn.onclick = () => {
      if (hkListen === id){ hkListen = null; syncHotkeys(); return; }
      hkListen = id;
      syncHotkeys();
    };
    row.appendChild(label);
    row.appendChild(btn);
    rows.appendChild(row);
  });
  if (hkListen){
    const btn = rows.querySelector(`.hk-key[data-id="${hkListen}"]`);
    if (btn) btn.textContent = "按下组合键…（Esc 取消）";
  }
  $("hk-sub").textContent = STATE.hotkeys_enabled
    ? "点右边的组合键框后，按下你想用的按键"
    : "开关打开后才会生效";
}
document.addEventListener("keydown", (e) => {
  if (!hkListen) return;
  e.preventDefault();
  e.stopPropagation();
  if (e.key === "Escape"){ hkListen = null; syncHotkeys(); return; }
  const code = e.code;
  if (/^(Control|Shift|Alt|Meta|Super)(Left|Right)?$/.test(code)) return; // 单按修饰键
  const mods = [];
  if (e.ctrlKey) mods.push("Ctrl");
  if (e.altKey) mods.push("Alt");
  if (e.shiftKey) mods.push("Shift");
  if (e.metaKey) mods.push("Win");
  const ok = /^(Key[A-Z]|Digit\d|Arrow(Up|Down|Left|Right)|F([1-9]|1[0-9]|2[0-4])|Space|Enter|Home|End|PageUp|PageDown|Insert|Delete|Semicolon|Equal|Comma|Minus|Period|Slash|Backquote|BracketLeft|Backslash|BracketRight|Quote|Numpad\d|NumpadAdd|NumpadSubtract|NumpadMultiply|NumpadDivide|NumpadDecimal)$/.test(code);
  if (!ok || !mods.length){
    const btn = document.querySelector(`.hk-key[data-id="${hkListen}"]`);
    if (btn) btn.textContent = mods.length ? "不支持这个键" : "需含 Ctrl / Alt / Win";
    return;
  }
  const id = hkListen;
  const combo = [...mods, code].join("+");
  const btn = document.querySelector(`.hk-key[data-id="${id}"]`);
  if (btn) btn.textContent = prettyCombo(combo);
  post("/api/hotkey", {id: id, keys: combo}).then(res => {
    hkListen = null;
    syncHotkeys();
    if (!res.ok) alert(res.message || "设置失败");
  });
}, true);

function applyMode(m){
  mode = m;
  [...$("modeseg").children].forEach(b => b.classList.toggle("active", b.dataset.mode === m));
  $("row-device").style.display = m === "device" ? "flex" : "none";
  $("row-app").style.display = m === "app" ? "flex" : "none";
  $("mute-label").textContent = m === "app" ? "推流时静音电脑上的该应用" : "推流时静音电脑本机";
  $("mute-sub").textContent = m === "app" ? "它本身几乎听不见，其他应用照常出声" : "声音只从音箱出，电脑不出声";
  setTimeout(fitWindow, 60);
}

function sync(){
  if (!STATE) return;
  const devs = STATE.devices || [];
  const srcs = STATE.sources || [];
  const apps = STATE.apps || [];

  if (STATE.native !== undefined){
    $("winmin").style.display = STATE.native ? "flex" : "none";
    $("winclose").style.display = STATE.native ? "flex" : "none";
  }

  if (devs.map(d => d.identifier).join("|") !== deviceSig){
    deviceSig = devs.map(d => d.identifier).join("|");
    setOptions($("device"), devs.map(d => ({value: d.identifier, label: d.name + " · " + d.address})),
      STATE.selected_device, "未发现 AirPlay 设备");
  }
  if (srcs.map(s => s.index).join("|") !== sourceSig){
    sourceSig = srcs.map(s => s.index).join("|");
    setOptions($("source"),
      srcs.map(s => ({value: s.index, label: s.name.replace(" [Loopback]", "") + (s.is_default ? "（默认）" : "")})),
      STATE.selected_source, "未找到输出设备");
  }
  const asig = apps.map(a => a.exe + ":" + (a.playing ? 1 : 0)).join("|") + "#" + (STATE.selected_app || "");
  if (asig !== appSig){
    appSig = asig;
    const items = apps.map(a => ({value: a.exe, label: a.label + (a.playing ? " · 正在播放" : "")}));
    if (STATE.selected_app && !items.some(i => i.value === STATE.selected_app)){
      items.unshift({value: STATE.selected_app, label: STATE.selected_app + " · 未运行"});
    }
    setOptions($("app"), items, STATE.selected_app, "没有找到可投送的应用");
  }

  if (!running && STATE.mode && STATE.mode !== mode) applyMode(STATE.mode);

  running = !!STATE.running;
  const err = STATE.status.includes("出错");
  const busy = STATE.status.includes("正在") && !running;

  $("tbdot").className = "tb-dot" + (running ? " on" : (err ? " err" : (busy ? " busy" : "")));
  $("dot").className = "dot" + (running ? " on" : (err ? " err" : (busy ? " busy" : "")));
  $("status").textContent = STATE.status;
  $("status").className = "status-text" + (running ? " live" : "");
  $("tile").classList.toggle("dim", !running);
  $("meter").style.width = (running ? Math.max(1.5, STATE.level) : 0) + "%";
  $("goicon").innerHTML = running ? ICON_STOP : ICON_PLAY;
  $("go").classList.toggle("on", running);
  $("go").title = running ? "停止推流" : "开始推流";

  const dev = devs.find(d => d.identifier === $("device").value);
  $("rcstatus").textContent = running
    ? "正在推流" + (dev ? " · " + dev.address : "")
    : (err ? "连接出错" : (busy ? "连接中…" : "未连接"));
  $("rcstatus").className = "rc-status" + (running ? " live" : "");
  $("rcsub").textContent = dev
    ? dev.name + " · 延迟 " + latency + "s" + (mode === "app" && STATE.selected_app ? " · 来源 " + STATE.selected_app.replace(/\.[^.]+$/, "") : "")
    : "未选择目标音箱";

  $("ver").textContent = "声渡 SoundFerry v" + (STATE.version || "") + " · AirPlay / RAOP";

  syncTheme();
  syncHotkeys();
  if (!window._hkDirtyUntil || Date.now() > window._hkDirtyUntil){
    $("hks").checked = !!STATE.hotkeys_enabled;
  }

  const lock = running;
  $("device").disabled = lock;
  $("source").disabled = lock;
  $("app").disabled = lock;
  $("resrc").disabled = lock;
  $("resapp").disabled = lock;
  [...$("modeseg").children].forEach(b => b.disabled = lock);

  if (!volumeDirty){
    $("volume").value = STATE.volume;
    paintVolume(STATE.volume);
  }
  if (!window._muteDirtyUntil || Date.now() > window._muteDirtyUntil){
    $("mute").checked = !!STATE.mute_local;
  }
  if (!window._autoDirtyUntil || Date.now() > window._autoDirtyUntil){
    $("autostart").checked = !!STATE.autostart;
    $("autoconn").checked = !!STATE.auto_connect;
  }
  $("row-autoconn").style.display = $("autostart").checked ? "flex" : "none";
  $("autoconn-sub").textContent = $("autostart").checked ? "开启后开机即自动推流" : "开机后自动开始推流可再次开启";

  // 提示
  const hint = $("hint");
  let text = "", warn = false;
  if (mode === "app"){
    const app = (STATE.apps || []).find(a => a.exe === $("app").value);
    text = "只把「" + ($("app").selectedOptions[0] ? $("app").selectedOptions[0].textContent.split(" ·")[0] : "所选应用")
         + "」的声音投到音箱，其它应用的声音不会混进去。";
    if (running && $("mute").checked){
      text += " 已开启「静音该应用」：它本身在电脑上会几乎静音，其他应用照常出声。";
    }
    if (app && !app.playing) text += " 它当前没有在播放声音。";
    if (running && app && !app.playing) warn = true;
  } else {
    const src = srcs.find(s => String(s.index) === $("source").value);
    if (dev && src && src.friendly.toLowerCase().includes(dev.name.toLowerCase())){
      text = "这个声音来源就是音箱本身（蓝牙）。开启「静音电脑本机」后蓝牙会静音，声音只走 AirPlay，不会双重播放。";
      warn = true;
    }
  }
  hint.textContent = text;
  hint.className = "hint" + (text ? " show" : "") + (warn ? " warn" : "");

  const log = $("log");
  const logs = STATE.logs || [];
  const sig = logs.map(l => l.t + l.text).join("|");
  if (sig !== window._logsSig){
    window._logsSig = sig;
    log.innerHTML = logs.map(l =>
      `<div class="${l.text.startsWith("[错误]") ? "e" : ""}"><span class="t">${l.t}</span>${escapeHtml(l.text)}</div>`
    ).join("");
    log.scrollTop = log.scrollHeight;
  }
  fitWindow();
}

function escapeHtml(s){
  return String(s).replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
}

async function poll(){
  try{
    const r = await fetch("/api/state", {cache: "no-store"});
    STATE = await r.json();
    if (!latency && STATE.latency) latency = STATE.latency;
    if (!$("latency").children.length){ buildLatency(); }
    sync();
  }catch(e){ /* 服务已退出 */ }
}

async function post(url, body){
  const r = await fetch(url, {
    method: "POST", headers: {"Content-Type": "application/json"},
    body: JSON.stringify(body || {})
  });
  return r.json().catch(() => ({}));
}

$("go").onclick = async () => {
  $("go").disabled = true;
  if (running){
    await post("/api/stop");
  } else {
    const body = {
      device: $("device").value,
      mode: mode,
      latency: latency,
      volume: parseInt($("volume").value, 10),
      mute: $("mute").checked
    };
    if (mode === "app") body.app = $("app").value;
    else body.source = parseInt($("source").value, 10);
    const res = await post("/api/start", body);
    if (!res.ok) alert(res.message || "启动失败");
  }
  setTimeout(() => { $("go").disabled = false; poll(); }, 400);
};

[...$("modeseg").children].forEach(b => {
  b.onclick = () => {
    if (running) return;
    applyMode(b.dataset.mode);
    post("/api/settings", {mode: b.dataset.mode});
  };
});

$("resrc").onclick = () => post("/api/scan-sources");
$("resapp").onclick = () => post("/api/scan-apps");
$("quit").onclick = async () => {
  await post("/api/quit");
  document.body.innerHTML = '<div style="padding:60px;text-align:center;color:#8b93a5;font-size:13px;">程序已退出，可以关闭这个窗口了。</div>';
};

$("volume").addEventListener("input", (e) => {
  volumeDirty = true;
  paintVolume(parseInt(e.target.value, 10));
});
$("volume").addEventListener("change", async (e) => {
  const v = parseInt(e.target.value, 10);
  await post("/api/volume", {volume: v});
  volumeDirty = false;
});
$("mute2").onclick = async () => {
  const v = ($("volume").value === "0") ? (lastVol || 1) : 0;
  $("volume").value = v;
  paintVolume(v);
  await post("/api/volume", {volume: v});
};
$("mute").onchange = async () => {
  window._muteDirtyUntil = Date.now() + 1500;
  await post("/api/settings", {mute: $("mute").checked});
};
$("autostart").onchange = async () => {
  window._autoDirtyUntil = Date.now() + 2000;
  const res = await post("/api/autostart", {enabled: $("autostart").checked});
  if (!res.ok) alert("修改开机自启动失败，可能被安全软件拦截了。");
  $("row-autoconn").style.display = $("autostart").checked ? "flex" : "none";
};
$("autoconn").onchange = async () => {
  window._autoDirtyUntil = Date.now() + 1500;
  await post("/api/settings", {auto_connect: $("autoconn").checked});
};
$("latency").onchange = () => {
  latency = parseFloat($("latency").value);
  post("/api/settings", {latency: latency});
};

$("device").onchange = () => post("/api/settings", {device: $("device").value});
$("source").onchange = () => post("/api/settings", {source: parseInt($("source").value, 10)});
$("app").onchange = () => post("/api/settings", {app: $("app").value});
$("hks").onchange = async () => {
  window._hkDirtyUntil = Date.now() + 1500;
  await post("/api/hotkeys/enabled", {enabled: $("hks").checked});
};
$("hkreset").onclick = async () => {
  const res = await post("/api/hotkeys/reset");
  if (res.errors && Object.keys(res.errors).length){
    alert(Object.values(res.errors).join("\n"));
  }
};

paintVolume(1);
applyMode("device");
poll();
setInterval(poll, 600);
setTimeout(fitWindow, 400);
window.addEventListener("resize", fitWindow);
</script>
</body>
</html>
"""

# ---------------------------------------------------------------------------
# 快速音量面板（托盘左键 / 状态变化时弹出，右下角，无边框）
# 只保留：设备名 + 音量条，标题栏藏进拖动条里
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# 快速音量面板（托盘左键 / 快捷键 / 状态变化时弹出，右下角，无边框）
# 极简：设备名 + 音量条 + 图钉（常驻置顶）+ ✕；主题跟随主界面
# ---------------------------------------------------------------------------
PANEL_PAGE = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>声渡 · 快速调节</title>
<link rel="icon" href="/favicon.ico">
<style>
  /* 主题变量：与主界面同一套（data-theme 跟随） */
  :root{
    --bg:#f6f8f5; --bg-soft:#e8f0e9; --card:#ffffff;
    --line:#dce7dd; --text:#12261a; --text-2:#41544a; --muted:#7f8c81;
    --accent:#3c8c4e; --accent-dark:#1f7237; --accent-soft:#e2efe3;
    --track:#d7e3d8; --hover-soft:rgba(18,38,26,.07); --pulse:60,140,78;
  }
  :root[data-theme="mist"]{
    --bg:#eef1f7; --bg-soft:#eef1f7; --card:#ffffff;
    --line:#e4e8f1; --text:#1a2030; --text-2:#525b6e; --muted:#8b93a5;
    --accent:#2f6bef; --accent-dark:#2657c8; --accent-soft:#e9efff;
    --track:#dde3ee; --hover-soft:rgba(26,32,48,.07); --pulse:47,107,239;
  }
  :root[data-theme="night"]{
    --bg:#14171f; --bg-soft:#1a1f2a; --card:#1c212c;
    --line:#2a3140; --text:#e9edf5; --text-2:#b7c0ce; --muted:#7e8798;
    --accent:#5b8dff; --accent-dark:#3f6fd6; --accent-soft:rgba(91,141,255,.16);
    --track:#2a3140; --hover-soft:rgba(233,237,245,.08); --pulse:91,141,255;
  }
  :root[data-theme="windnight"]{
    --bg:#10150f; --bg-soft:#161f16; --card:#18211a;
    --line:#26352a; --text:#e6efe6; --text-2:#b7c8ba; --muted:#7e8f83;
    --accent:#59ac65; --accent-dark:#3c8c4e; --accent-soft:rgba(89,172,101,.16);
    --track:#26352a; --hover-soft:rgba(230,239,230,.08); --pulse:89,172,101;
  }
  *{box-sizing:border-box; -webkit-tap-highlight-color:transparent;}
  html{font-size:clamp(12px, calc(8.6px + .34vw), 16.5px);}
  body{margin:0; background:var(--bg); color:var(--text); padding:.55rem .7rem;
    font-family:"SF Pro Text","MiSans","HarmonyOS Sans SC","Segoe UI Variable Text","Segoe UI","Microsoft YaHei UI",system-ui,sans-serif;
    -webkit-font-smoothing:antialiased; user-select:none;}
  .card{background:var(--card); border:1px solid var(--line); border-radius:14px;
    padding:.7rem .85rem .75rem; box-shadow:0 1px 2px rgba(23,32,54,.05), 0 8px 22px -14px rgba(30,50,120,.45);}
  body[data-pinned="1"] .card{border-color:var(--accent);
    box-shadow:0 1px 2px rgba(var(--pulse),.12), 0 10px 26px -14px rgba(var(--pulse),.55);}
  .head{display:flex; align-items:center; gap:.55rem; margin-bottom:.55rem;}
  .tile{width:1.7rem; height:1.7rem; border-radius:.55rem; background:var(--accent-soft);
    display:flex; align-items:center; justify-content:center; flex:none; color:var(--accent);}
  .tile svg{width:1rem; height:1rem;}
  .name{font-size:.82rem; font-weight:600; line-height:1.25; min-width:0;
    white-space:nowrap; overflow:hidden; text-overflow:ellipsis;}
  .sub{font-size:.66rem; color:var(--muted); margin-top:.05rem;
    white-space:nowrap; overflow:hidden; text-overflow:ellipsis;}
  .xbtn{margin-left:auto; width:1.5rem; height:1.5rem; border:0; background:transparent; color:var(--muted);
    border-radius:.4rem; display:flex; align-items:center; justify-content:center; cursor:pointer; padding:0;
    transition:all .12s; flex:none;}
  .xbtn:hover{background:var(--hover-soft); color:var(--text);}
  .xbtn svg{width:.7rem; height:.7rem;}
  .xbtn.pin{width:1.55rem; height:1.55rem; margin-left:.15rem; order:0;}
  .xbtn.pin svg{width:.85rem; height:.85rem;}
  .xbtn.pin.active{background:var(--accent); color:#fff;}
  .vol-row{display:flex; align-items:center; gap:.6rem;}
  input[type=range]{width:100%; -webkit-appearance:none; height:1.4rem; background:transparent; margin:0; cursor:pointer;}
  input[type=range]::-webkit-slider-runnable-track{height:.35rem; border-radius:99px;
    background:linear-gradient(to right, var(--accent) var(--p,100%), var(--track) var(--p,100%));}
  input[type=range]::-webkit-slider-thumb{-webkit-appearance:none; width:1.05rem; height:1.05rem; border-radius:50%;
    background:var(--accent); margin-top:-.36rem; box-shadow:0 1px 3px rgba(var(--pulse),.4); transition:transform .12s;}
  input[type=range]:active::-webkit-slider-thumb{transform:scale(1.2);}
  .vol-num{font-size:.95rem; font-weight:700; color:var(--text); text-align:center;
    width:2.5rem; flex:none; font-variant-numeric:tabular-nums;}
</style>
</head>
<body>
<div class="card">
  <div class="head pywebview-drag-region" style="cursor:default;">
    <div class="tile">
      <svg viewBox="0 0 24 24" fill="none"><rect x="8.2" y="3" width="7.6" height="18" rx="2.6" stroke="currentColor" stroke-width="1.7"/><circle cx="12" cy="8" r="1.6" stroke="currentColor" stroke-width="1.5"/><circle cx="12" cy="14.5" r="2.6" stroke="currentColor" stroke-width="1.5"/></svg>
    </div>
    <div style="min-width:0;">
      <div class="name" id="name">未选择音箱</div>
      <div class="sub" id="sub">声渡 · 快速调节音量</div>
    </div>
    <button class="xbtn pin" id="pin" title="常驻置顶：点击屏幕其他地方不会关闭">
      <svg viewBox="0 0 24 24" fill="none">
        <circle cx="12" cy="5" r="2.8" stroke="currentColor" stroke-width="2.6"/>
        <path d="M9.6 8h4.8l1.4 5.5H8.2L9.6 8z" stroke="currentColor" stroke-width="2.6" stroke-linejoin="round"/>
        <path d="M12 13.5V21" stroke="currentColor" stroke-width="2.6" stroke-linecap="round"/>
      </svg>
    </button>
    <button class="xbtn" id="close" title="收起">
      <svg viewBox="0 0 12 12" fill="none"><path d="M2.5 2.5l7 7M9.5 2.5l-7 7" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"/></svg>
    </button>
  </div>
  <div class="vol-row">
    <input type="range" id="volume" min="0" max="100" step="1" value="1">
    <span class="vol-num" id="volnum">1%</span>
  </div>
</div>

<script>
const $ = (id) => document.getElementById(id);
let volumeDirty = false, lastTouch = Date.now(), lastFitH = 0, pinned = false;

["pointerdown","wheel","keydown","touchstart"].forEach(ev =>
  window.addEventListener(ev, () => { lastTouch = Date.now(); }, {passive:true}));
window.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && !pinned) post("/api/win/hide-panel");
});
setInterval(() => {
  if (!pinned && Date.now() - lastTouch > 90000){ post("/api/win/hide-panel"); }
}, 2000);

$("close").onclick = () => post("/api/win/hide-panel");
$("pin").onclick = () => post("/api/panel/pin", {pinned: !pinned});

function fitWindow(){
  try{
    const h = Math.ceil(document.body.getBoundingClientRect().height) + 22;
    if (Math.abs(h - lastFitH) > 4){
      lastFitH = h;
      post("/api/win/fit", {kind:"panel", w: 320, h: h});
    }
  }catch(e){}
}

function paintVolume(v){
  $("volnum").textContent = v + "%";
  $("volume").style.setProperty("--p", Math.min(100, v) + "%");
}

function applyPinned(p){
  pinned = !!p;
  $("pin").classList.toggle("active", pinned);
  document.body.dataset.pinned = pinned ? "1" : "0";
  $("pin").title = pinned ? "已常驻置顶：点击取消，恢复「点外部自动关闭」" : "常驻置顶：点击屏幕其他地方不会关闭";
}

async function poll(){
  try{
    const r = await fetch("/api/state", {cache:"no-store"});
    const s = await r.json();
    if (s.theme) document.documentElement.dataset.theme = s.theme;
    applyPinned(!!s.panel_pinned);
    const dev = (s.devices || []).find(d => d.identifier === s.selected_device);
    if (dev){
      $("name").textContent = dev.name;
      $("sub").textContent = (pinned ? "常驻置顶 · " : "") + (s.running ? "推流中" : "未推流");
    } else {
      $("name").textContent = "未选择音箱";
      $("sub").textContent = "声渡 · 快速调节音量";
    }
    if (!volumeDirty){
      $("volume").value = s.volume;
      paintVolume(s.volume);
    }
  }catch(e){}
}

async function post(url, body){
  const r = await fetch(url, {
    method:"POST", headers:{"Content-Type":"application/json"},
    body: JSON.stringify(body || {})
  });
  return r.json().catch(() => ({}));
}

$("volume").addEventListener("input", (e) => {
  volumeDirty = true;
  lastTouch = Date.now();
  paintVolume(parseInt(e.target.value, 10));
});
$("volume").addEventListener("change", async (e) => {
  await post("/api/volume", {volume: parseInt(e.target.value, 10)});
  volumeDirty = false;
});

paintVolume(1);
poll();
setInterval(poll, 700);
setTimeout(fitWindow, 300);
</script>
</body>
</html>
"""