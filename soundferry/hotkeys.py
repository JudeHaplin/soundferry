"""全局快捷键：Windows RegisterHotKey + 独立消息循环线程。

设计要点：
- 注册必须在消息循环所在线程进行（RegisterHotKey 传 NULL hwnd 时绑定调用线程），
  所以所有注册/注销都通过命令队列交给热键线程自己执行。
- 组合键字符串格式与 JS KeyboardEvent.code 对齐，例如
  "Ctrl+Alt+ArrowUp"、"Ctrl+Shift+KeyM"、"Alt+F5"、"Ctrl+Alt+Numpad4"。
- 每个动作注册失败（被其他程序占用）时把错误写回 bindings_errors。
"""

from __future__ import annotations

import logging
import queue
import threading

LOGGER = logging.getLogger(__name__)

WM_HOTKEY = 0x0312
WM_APP_WAKE = 0x8000  # 自定义唤醒消息（PostThreadMessage）

MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004
MOD_WIN = 0x0008
MOD_NOREPEAT = 0x4000

MODMAP = {"CTRL": MOD_CONTROL, "ALT": MOD_ALT, "SHIFT": MOD_SHIFT, "WIN": MOD_WIN}

DEFAULT_HOTKEYS = {
    "vol_up": "Ctrl+Shift+Equal",
    "vol_down": "Ctrl+Shift+Minus",
    "mute": "Ctrl+Shift+KeyM",
    "panel": "Ctrl+Shift+KeyP",
}

HOTKEY_LABELS = {
    "vol_up": "增大音量",
    "vol_down": "减小音量",
    "mute": "静音 / 取消静音",
    "panel": "打开快速面板",
}


def _build_vkeys() -> dict[str, int]:
    vk: dict[str, int] = {}
    for i in range(26):
        vk[f"Key{chr(65 + i)}"] = 0x41 + i
    for i in range(10):
        vk[f"Digit{i}"] = 0x30 + i
        vk[f"Numpad{i}"] = 0x60 + i
    for n in range(1, 25):
        vk[f"F{n}"] = 0x70 + n - 1
    vk.update(
        {
            "ArrowUp": 0x26,
            "ArrowDown": 0x28,
            "ArrowLeft": 0x25,
            "ArrowRight": 0x27,
            "Space": 0x20,
            "Enter": 0x0D,
            "Tab": 0x09,
            "Backspace": 0x08,
            "Home": 0x24,
            "End": 0x23,
            "PageUp": 0x21,
            "PageDown": 0x22,
            "Insert": 0x2D,
            "Delete": 0x2E,
            "Semicolon": 0xBA,
            "Equal": 0xBB,
            "Comma": 0xBC,
            "Minus": 0xBD,
            "Period": 0xBE,
            "Slash": 0xBF,
            "Backquote": 0xC0,
            "BracketLeft": 0xDB,
            "Backslash": 0xDC,
            "BracketRight": 0xDD,
            "Quote": 0xDE,
            "NumpadAdd": 0x6B,
            "NumpadSubtract": 0x6D,
            "NumpadMultiply": 0x6A,
            "NumpadDivide": 0x6F,
            "NumpadDecimal": 0x6E,
        }
    )
    return vk


VKEYS = _build_vkeys()


def parse_combo(combo: str) -> tuple[int, int] | None:
    """把 "Ctrl+Alt+ArrowUp" 解析成 (modifiers, vk)；无效返回 None。"""
    if not combo or not isinstance(combo, str):
        return None
    parts = [p.strip() for p in combo.split("+") if p.strip()]
    if len(parts) < 2:
        return None  # 必须带修饰键，避免抢占普通按键
    key_name = parts[-1].upper()
    mods = 0
    for p in parts[:-1]:
        flag = MODMAP.get(p.upper())
        if flag is None:
            return None
        mods |= flag
    if mods == 0:
        return None
    if key_name.startswith("NUMPAD") and key_name[6:].isdigit():
        vk = VKEYS.get(key_name.title())  # Numpad4 形式
        if vk is None:
            return None
        return mods, vk
    vk = VKEYS.get(parts[-1]) or VKEYS.get(parts[-1].title())
    if vk is None:
        return None
    return mods, vk


class HotkeyManager:
    """管理一组命名热键（id -> 组合键字符串），触发时回调 on_action(id)。"""

    def __init__(self, on_action) -> None:
        self.on_action = on_action
        self._queue: queue.Queue = queue.Queue()
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()
        self._thread_id = 0
        self._registered: dict[int, tuple[int, int]] = {}  # slot -> (mods, vk)
        self._current: dict[str, str] = {}
        self.enabled = True
        self.errors: dict[str, str] = {}
        self._slot_names: dict[int, str] = {}
        self._stopping = False

    # ---- 对外接口 ---------------------------------------------------------
    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._loop, name="soundferry-hotkeys", daemon=True)
        self._thread.start()
        self._ready.wait(timeout=5)

    def update(self, bindings: dict[str, str], enabled: bool | None = None) -> dict[str, str]:
        """应用新绑定；返回 {id: 错误信息}（仅包含失败项）。"""
        if enabled is not None:
            self.enabled = bool(enabled)
        self._queue.put(("set", dict(bindings)))
        self._wake()
        # 等注册结果（简单起见轮询一小段时间）
        import time

        deadline = time.monotonic() + 2.0
        want = set(bindings)
        while time.monotonic() < deadline:
            if self._current.keys() >= want:
                break
            time.sleep(0.05)
        return dict(self.errors)

    def stop(self) -> None:
        if self._thread is None:
            return
        self._stopping = True
        try:
            self._queue.put(("quit", None))
            self._wake()
            self._thread.join(timeout=3)
        except Exception:  # noqa: BLE001
            LOGGER.debug("停止热键线程失败", exc_info=True)
        self._thread = None

    # ---- 内部 -------------------------------------------------------------
    def _wake(self) -> None:
        """唤醒热键线程处理命令队列。

        PostThreadMessage 在目标线程还没有消息队列时会失败，
        所以失败时短暂重试（线程通常马上就会建好队列）。
        """
        import ctypes
        import time

        if not self._thread_id:
            return
        user32 = ctypes.windll.user32
        for _ in range(40):
            if user32.PostThreadMessageW(self._thread_id, WM_APP_WAKE, 0, 0):
                return
            time.sleep(0.05)

    def _loop(self) -> None:
        import ctypes

        from ctypes import wintypes

        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
        self._thread_id = kernel32.GetCurrentThreadId()

        # 先强制创建本线程的消息队列（PeekMessage 一次），否则
        # 其他线程 PostThreadMessageW 会失败、命令永远送不进来
        probe = wintypes.MSG()
        user32.PeekMessageW(ctypes.byref(probe), None, 0, 0, 0)
        self._ready.set()  # 消息队列就绪（thread_id 可用于 PostThreadMessage）

        msg = wintypes.MSG()
        try:
            while True:
                try:
                    cmd, payload = self._queue.get_nowait()
                except queue.Empty:
                    # GetMessageW 阻塞等待消息（热键触发 / 唤醒）
                    r = user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
                    if r <= 0:
                        break
                    if msg.message == WM_HOTKEY:
                        name = self._slot_names.get(int(msg.wParam))
                        if name and self.enabled:
                            try:
                                self.on_action(name)
                            except Exception:  # noqa: BLE001
                                LOGGER.exception("快捷键动作执行失败: %s", name)
                        continue
                    # 其他消息（WM_APP_WAKE）→ 回到循环头处理队列
                    continue

                if cmd == "quit":
                    self._unregister_all(user32)
                    break
                if cmd == "set":
                    self._apply(user32, payload)
        except Exception:  # noqa: BLE001
            LOGGER.exception("热键消息循环崩溃")

        self._ready.set()

    def _apply(self, user32, bindings: dict[str, str]) -> None:
        self._unregister_all(user32)
        self._registered.clear()
        self._slot_names.clear()
        self.errors = {}
        self._current = {}
        if not self.enabled:
            return
        for slot, (name, combo) in enumerate(bindings.items()):
            parsed = parse_combo(combo)
            if parsed is None:
                self.errors[name] = f"「{combo}」不是有效的组合键（需要 Ctrl/Alt/Win 修饰键）"
                continue
            mods, vk = parsed
            # 静音/面板这类开关动作不响应按住重复；音量键允许重复
            flags = mods
            if name in ("mute", "panel"):
                flags |= MOD_NOREPEAT
            ok = user32.RegisterHotKey(None, slot, flags, vk)
            if ok:
                self._registered[slot] = (mods, vk)
                self._slot_names[slot] = name
                self._current[name] = combo
            else:
                self.errors[name] = f"「{self._pretty(combo)}」已被其他程序占用，换一个组合试试"

    def _unregister_all(self, user32) -> None:
        for slot in list(self._registered):
            try:
                user32.UnregisterHotKey(None, slot)
            except Exception:  # noqa: BLE001
                pass

    @staticmethod
    def _pretty(combo: str) -> str:
        return combo.replace("ArrowUp", "↑").replace("ArrowDown", "↓").replace(
            "ArrowLeft", "←"
        ).replace("ArrowRight", "→").replace("Key", "").replace("Digit", "").replace(
            "Numpad", "Num"
        )
