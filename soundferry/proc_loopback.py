"""按进程抓取声音（WASAPI Process Loopback，Windows 10 2004 及以上）。

系统混音回环（``pyaudiowpatch``）拿到的是「全部应用混在一起」的声音，
如果只想把某个应用（比如 QQ 音乐）的声音投到音箱，就绕不开这个接口：

    ActivateAudioInterfaceAsync("VAD\\Process_Loopback", IAudioClient, PROPVARIANT{Blob})

这是微软给「应用程序回环捕获」开的正式口子（Win32 桌面程序也能用），
不需要装虚拟声卡驱动，也不会影响其它应用的播放。

底层全部用 ctypes 手工调 COM（vtable 直调），原因：
1. 不依赖 comtypes 的代码生成，PyInstaller 打包更稳；
2. 进程回环的完成回调需要在任意线程被 COM 调用，手写 vtable 更好控制生命周期。
"""

from __future__ import annotations

import ctypes
import json
import logging
import os
import threading
import time
from pathlib import Path
from ctypes import (
    POINTER,
    Structure,
    byref,
    c_int64,
    c_long,
    c_ubyte,
    c_uint16,
    c_uint32,
    c_uint64,
    c_void_p,
    c_wchar_p,
    cast,
    sizeof,
)
from typing import Callable, Optional

LOGGER = logging.getLogger(__name__)

VAD_PROCESS_LOOPBACK = "VAD\\Process_Loopback"

# ---- 常量 -----------------------------------------------------------------
AUDCLNT_SHAREMODE_SHARED = 0
AUDCLNT_STREAMFLAGS_LOOPBACK = 0x00020000
AUDCLNT_STREAMFLAGS_EVENTCALLBACK = 0x00040000
AUDCLNT_STREAMFLAGS_AUTOCONVERTPCM = 0x80000000
AUDCLNT_STREAMFLAGS_SRC_DEFAULT_QUALITY = 0x08000000
AUDCLNT_BUFFERFLAGS_SILENT = 0x2
AUDIOCLIENT_ACTIVATION_TYPE_PROCESS_LOOPBACK = 1
PROCESS_LOOPBACK_MODE_INCLUDE_TARGET_PROCESS_TREE = 0
VT_BLOB = 65
S_OK = 0
E_NOINTERFACE = -2147467262  # 0x80004002
E_POINTER = -2147467261  # 0x80004003

WAVE_FORMAT_PCM = 1
WAVE_FORMAT_IEEE_FLOAT = 3


# ---- 基础结构 -------------------------------------------------------------
class GUID(Structure):
    _fields_ = [
        ("Data1", c_uint32),
        ("Data2", c_uint16),
        ("Data3", c_uint16),
        ("Data4", c_ubyte * 8),
    ]


def guid(text: str) -> GUID:
    raw = text.strip("{}").replace("-", "")
    return GUID(
        int(raw[0:8], 16),
        int(raw[8:12], 16),
        int(raw[12:16], 16),
        (c_ubyte * 8)(*bytes.fromhex(raw[16:32])),
    )


IID_IAudioClient = guid("{1CB9AD4C-DBFA-4C32-B178-C2F568A703B2}")
IID_IAudioCaptureClient = guid("{C8ADBD64-E71E-48A0-A4DE-185C395CD317}")
IID_IUnknown = guid("{00000000-0000-0000-C000-000000000046}")
IID_IActivateAudioInterfaceCompletionHandler = guid("{41D949AB-9862-444A-80F6-C261334DA5EB}")
# 空标记接口：完成回调必须声明自己是"跨套间安全"的，
# 否则 ActivateAudioInterfaceAsync 直接返回 E_ILLEGAL_METHOD_CALL（微软文档明确要求）。
IID_IAgileObject = guid("{94EA2B94-E9CC-49E0-C0FF-EE64CA8F5B90}")


class WAVEFORMATEX(Structure):
    _fields_ = [
        ("wFormatTag", c_uint16),
        ("nChannels", c_uint16),
        ("nSamplesPerSec", c_uint32),
        ("nAvgBytesPerSec", c_uint32),
        ("nBlockAlign", c_uint16),
        ("wBitsPerSample", c_uint16),
        ("cbSize", c_uint16),
    ]


class AUDIOCLIENT_PROCESS_LOOPBACK_PARAMS(Structure):
    _fields_ = [
        ("TargetProcessId", c_uint32),
        ("ProcessLoopbackMode", ctypes.c_int32),
    ]


class AUDIOCLIENT_ACTIVATION_PARAMS(Structure):
    _fields_ = [
        ("ActivationType", ctypes.c_int32),
        ("ProcessLoopbackParams", AUDIOCLIENT_PROCESS_LOOPBACK_PARAMS),
    ]


class _BLOB(Structure):
    _fields_ = [("cbSize", c_uint32), ("pBlobData", POINTER(c_ubyte))]


class PROPVARIANT(Structure):
    """只用到 VT_BLOB 这一种形态，足够传递激活参数。"""

    _fields_ = [
        ("vt", c_uint16),
        ("wReserved1", c_uint16),
        ("wReserved2", c_uint16),
        ("wReserved3", c_uint16),
        ("blob", _BLOB),
    ]


# ---- COM vtable 直调 ------------------------------------------------------
def _method(this: int, index: int, restype, *argtypes):
    """取出 COM 接口第 index 个方法的可调用包装。"""
    vtbl = cast(this, POINTER(POINTER(c_void_p))).contents
    proto = ctypes.WINFUNCTYPE(restype, c_void_p, *argtypes)
    return proto(vtbl[index])


def _release(this: Optional[int]) -> None:
    if not this:
        return
    try:
        _method(this, 2, c_uint32)(this)
    except Exception:  # noqa: BLE001 - 释放失败无需处理
        LOGGER.debug("COM 释放失败", exc_info=True)


class _CompletionHandler:
    """IActivateAudioInterfaceCompletionHandler 的最小实现。

    激活是异步的：COM 在内部线程回调 ActivateCompleted，我们只需把事件置位。
    """

    _REF = 1

    def __init__(self) -> None:
        self.done = threading.Event()
        proto = ctypes.WINFUNCTYPE

        self._qi_fn = proto(c_long, c_void_p, POINTER(GUID), POINTER(c_void_p))(self._query_interface)
        self._addref_fn = proto(c_uint32, c_void_p)(self._add_ref)
        self._release_fn = proto(c_uint32, c_void_p)(self._release_ref)
        self._completed_fn = proto(c_long, c_void_p, c_void_p)(self._activate_completed)

        self._vtable = (c_void_p * 4)(
            cast(self._qi_fn, c_void_p).value,
            cast(self._addref_fn, c_void_p).value,
            cast(self._release_fn, c_void_p).value,
            cast(self._completed_fn, c_void_p).value,
        )
        # COM 对象的首字段是 vtable 指针
        self._object = (c_void_p * 1)(cast(self._vtable, c_void_p).value)
        self.this = cast(self._object, c_void_p)

    # --- IUnknown ---
    def _query_interface(self, this, riid, ppv) -> int:
        if not ppv:
            return E_POINTER
        wanted = riid.contents if riid else None
        if wanted is None:
            return E_POINTER
        for known in (
            IID_IUnknown,
            IID_IAgileObject,
            IID_IActivateAudioInterfaceCompletionHandler,
        ):
            if ctypes.string_at(byref(wanted), sizeof(GUID)) == ctypes.string_at(byref(known), sizeof(GUID)):
                ppv[0] = self.this
                self._REF += 1
                return S_OK
        return E_NOINTERFACE

    def _add_ref(self, this) -> int:
        self._REF += 1
        return self._REF

    def _release_ref(self, this) -> int:
        self._REF -= 1
        # 保持对象存活，避免 COM 在我们还没取结果时把对象释放掉
        return max(self._REF, 1)

    # --- IActivateAudioInterfaceCompletionHandler ---
    def _activate_completed(self, this, operation) -> int:
        self.done.set()
        return S_OK


_ActivateAudioInterfaceAsync = ctypes.WinDLL("Mmdevapi.dll").ActivateAudioInterfaceAsync
_ActivateAudioInterfaceAsync.restype = c_long
_ActivateAudioInterfaceAsync.argtypes = [
    c_wchar_p,
    POINTER(GUID),
    POINTER(PROPVARIANT),
    c_void_p,
    POINTER(c_void_p),
]

_kernel32 = ctypes.WinDLL("kernel32.dll", use_last_error=True)
_kernel32.CreateEventW.restype = c_void_p
_kernel32.CreateEventW.argtypes = [c_void_p, ctypes.c_int, ctypes.c_int, c_wchar_p]
_kernel32.SetEvent.argtypes = [c_void_p]
_kernel32.WaitForSingleObject.restype = c_uint32
_kernel32.WaitForSingleObject.argtypes = [c_void_p, c_uint32]
_kernel32.CloseHandle.argtypes = [c_void_p]


def _co_initialize() -> None:
    try:
        ctypes.windll.ole32.CoInitializeEx(None, 0x0)  # COINIT_MULTITHREADED
    except Exception:  # noqa: BLE001
        pass


def _make_format(rate: int, channels: int, bits: int, is_float: bool = False) -> WAVEFORMATEX:
    block = channels * bits // 8
    return WAVEFORMATEX(
        wFormatTag=WAVE_FORMAT_IEEE_FLOAT if is_float else WAVE_FORMAT_PCM,
        nChannels=channels,
        nSamplesPerSec=rate,
        nAvgBytesPerSec=rate * block,
        nBlockAlign=block,
        wBitsPerSample=bits,
        cbSize=0,
    )


class ProcessLoopbackCapture:
    """抓取「指定进程（及其子进程）」输出的声音。

    回调格式固定为 int16 交织 PCM，采样率/声道数在构造时指定。
    """

    def __init__(
        self,
        pid: int,
        sample_rate: int = 44100,
        channels: int = 2,
        on_audio: Callable[[bytes], None] | None = None,
        on_level: Callable[[int], None] | None = None,
        on_closed: Callable[[], None] | None = None,
    ) -> None:
        self.pid = int(pid)
        self.sample_rate = int(sample_rate)
        self.channels = int(channels)
        self.sample_size = 2  # 每采样字节数；float32 时为 4
        self.is_float = False
        self.on_audio = on_audio or (lambda _b: None)
        self.on_level = on_level or (lambda _n: None)
        self.on_closed = on_closed or (lambda: None)

        self._thread: Optional[threading.Thread] = None
        self._stopping = threading.Event()
        self._ready = threading.Event()
        self._error: Optional[str] = None
        self._client = None
        self._capture = None
        self._event = None
        self._started = False
        self._silent_ct = 0

    # ---- 生命周期 ------------------------------------------------------
    def start(self) -> "ProcessLoopbackCapture":
        self._thread = threading.Thread(target=self._run, name="proc-loopback", daemon=True)
        self._thread.start()
        if not self._ready.wait(5.0):
            raise RuntimeError("启动应用音频捕获超时")
        if self._error:
            raise RuntimeError(self._error)
        return self

    def stop(self) -> None:
        self._stopping.set()
        if self._thread is not None:
            self._thread.join(timeout=3.0)
            self._thread = None

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    # ---- 内部 ----------------------------------------------------------
    def _run(self) -> None:
        _co_initialize()
        client = None
        capture = None
        event = None
        try:
            client, capture, event = self._open()
            self._client, self._capture, self._event = client, capture, event
            self._ready.set()
            self._loop(capture, event)
        except Exception as exc:  # noqa: BLE001
            self._error = str(exc)
            LOGGER.warning("应用音频捕获失败: %s", exc, exc_info=True)
            self._ready.set()
        finally:
            if client and self._started:
                try:
                    _method(client, 11, c_long)(client)  # Stop
                except Exception:  # noqa: BLE001
                    pass
            _release(capture)
            _release(client)
            if event:
                _kernel32.CloseHandle(event)
            self._capture = None
            self._client = None
            self.on_closed()

    def _open(self):
        params = AUDIOCLIENT_ACTIVATION_PARAMS()
        params.ActivationType = AUDIOCLIENT_ACTIVATION_TYPE_PROCESS_LOOPBACK
        params.ProcessLoopbackParams.TargetProcessId = self.pid
        params.ProcessLoopbackParams.ProcessLoopbackMode = (
            PROCESS_LOOPBACK_MODE_INCLUDE_TARGET_PROCESS_TREE
        )

        prop = PROPVARIANT()
        prop.vt = VT_BLOB
        prop.blob.cbSize = sizeof(AUDIOCLIENT_ACTIVATION_PARAMS)
        prop.blob.pBlobData = cast(byref(params), POINTER(c_ubyte))

        handler = _CompletionHandler()
        operation = c_void_p()
        hr = _ActivateAudioInterfaceAsync(
            VAD_PROCESS_LOOPBACK,
            byref(IID_IAudioClient),
            byref(prop),
            handler.this,
            byref(operation),
        )
        if hr < 0:
            raise RuntimeError(f"激活进程回环失败 (0x{hr & 0xFFFFFFFF:08X})，需要 Windows 10 2004 或更高版本")
        if not operation.value:
            raise RuntimeError("激活进程回环失败：没有返回操作对象")

        try:
            if not handler.done.wait(4.0):
                raise RuntimeError("激活进程回环超时")
            activate_result = c_long()
            iface = c_void_p()
            hr = _method(operation, 3, c_long, POINTER(c_long), POINTER(c_void_p))(
                operation, byref(activate_result), byref(iface)
            )
            if hr < 0:
                raise RuntimeError(f"取回音频接口失败 (0x{hr & 0xFFFFFFFF:08X})")
            if activate_result.value < 0 or not iface.value:
                raise RuntimeError(
                    f"无法捕获该应用的音频 (0x{activate_result.value & 0xFFFFFFFF:08X})，"
                    "请确认应用正在运行且系统为 Windows 10 2004+"
                )
        finally:
            _release(operation.value)

        client = iface.value
        last_error = None
        # 优先 float32：配合「压低会话音量 + 软件端补偿增益」的静音策略，
        # 浮点精度下信号缩小上千倍再还原几乎无损；int16 只作为兜底。
        candidates = [
            (self.sample_rate, self.channels, 32, True),
            (self.sample_rate, self.channels, 16, False),
            (48000, 2, 32, True),
            (48000, 2, 16, False),
            (44100, 2, 32, True),
            (44100, 2, 16, False),
        ]
        for convert in (True, False):
            for rate, ch, bits, is_float in candidates:
                fmt = _make_format(rate, ch, bits, is_float)
                flags = AUDCLNT_STREAMFLAGS_LOOPBACK | AUDCLNT_STREAMFLAGS_EVENTCALLBACK
                if convert:
                    flags |= AUDCLNT_STREAMFLAGS_AUTOCONVERTPCM | AUDCLNT_STREAMFLAGS_SRC_DEFAULT_QUALITY
                hr = _method(
                    client,
                    3,
                    c_long,
                    c_uint32,
                    c_uint32,
                    c_int64,
                    c_int64,
                    POINTER(WAVEFORMATEX),
                    c_void_p,
                )(
                    client,
                    AUDCLNT_SHAREMODE_SHARED,
                    flags,
                    200000,  # 20ms
                    0,
                    byref(fmt),
                    None,
                )
                if hr >= 0:
                    self.sample_rate = rate
                    self.channels = ch
                    self.sample_size = bits // 8
                    self.is_float = is_float
                    last_error = None
                    break
                last_error = hr
            if last_error is None:
                break
        if last_error is not None:
            _release(client)
            raise RuntimeError(f"初始化音频客户端失败 (0x{last_error & 0xFFFFFFFF:08X})")

        capture = c_void_p()
        hr = _method(client, 14, c_long, POINTER(GUID), POINTER(c_void_p))(
            client, byref(IID_IAudioCaptureClient), byref(capture)
        )
        if hr < 0 or not capture.value:
            _release(client)
            raise RuntimeError(f"取回采集接口失败 (0x{hr & 0xFFFFFFFF:08X})")

        event = _kernel32.CreateEventW(None, 0, 0, None)
        hr = _method(client, 13, c_long, c_void_p)(client, event)
        if hr < 0:
            _release(capture.value)
            _release(client)
            raise RuntimeError(f"绑定采集事件失败 (0x{hr & 0xFFFFFFFF:08X})")

        hr = _method(client, 10, c_long)(client)  # Start
        if hr < 0:
            _release(capture.value)
            _release(client)
            raise RuntimeError(f"启动采集失败 (0x{hr & 0xFFFFFFFF:08X})")
        self._started = True
        LOGGER.info(
            "应用音频捕获已启动: pid=%d, %d Hz, %d 声道, %s",
            self.pid,
            self.sample_rate,
            self.channels,
            "float32" if self.is_float else "int16",
        )
        return client, capture.value, event

    def _loop(self, capture: int, event: int) -> None:
        frame_size = self.channels * self.sample_size
        packets = c_uint32()
        data = POINTER(c_ubyte)()
        frames = c_uint32()
        flags = c_uint32()
        dev_pos = c_uint64()
        qpc_pos = c_uint64()

        while not self._stopping.is_set():
            _kernel32.WaitForSingleObject(event, 40)
            while not self._stopping.is_set():
                hr = _method(capture, 5, c_long, POINTER(c_uint32))(capture, byref(packets))
                if hr < 0 or packets.value == 0:
                    break
                hr = _method(
                    capture,
                    3,
                    c_long,
                    POINTER(POINTER(c_ubyte)),
                    POINTER(c_uint32),
                    POINTER(c_uint32),
                    POINTER(c_uint64),
                    POINTER(c_uint64),
                )(capture, byref(data), byref(frames), byref(flags), byref(dev_pos), byref(qpc_pos))
                if hr < 0:
                    return
                count = frames.value
                if count:
                    size = count * frame_size
                    if flags.value & AUDCLNT_BUFFERFLAGS_SILENT:
                        chunk = b"\x00" * size
                    else:
                        chunk = ctypes.string_at(data, size)
                    self.on_audio(chunk)
                _method(capture, 4, c_long, c_uint32)(capture, count)  # ReleaseBuffer


# ---- 进程与窗口枚举（给界面用）--------------------------------------------
def _process_name(pid: int) -> str:
    try:
        import psutil

        return psutil.Process(pid).name()
    except Exception:  # noqa: BLE001
        return ""


def _visible_window_pids() -> list[int]:
    user32 = ctypes.windll.user32
    pids: list[int] = []
    buf = ctypes.c_void_p()

    @ctypes.WINFUNCTYPE(ctypes.c_bool, c_void_p, c_void_p)
    def _cb(hwnd, _lparam):  # noqa: ANN001
        if not user32.IsWindowVisible(hwnd):
            return True
        length = user32.GetWindowTextLengthW(hwnd)
        if length <= 0:
            return True
        if user32.GetWindow(hwnd, 4):  # GW_OWNER：跳过工具窗口
            return True
        pid = c_uint32()
        user32.GetWindowThreadProcessId(hwnd, byref(pid))
        if pid.value:
            pids.append(pid.value)
        return True

    user32.EnumWindows(_cb, 0)
    return pids


_SKIP_EXES = {
    "explorer.exe",
    "searchhost.exe",
    "startmenuexperiencehost.exe",
    "shellexperiencehost.exe",
    "textinputhost.exe",
    "applicationframehost.exe",
    "systemsettings.exe",
    "python.exe",
    "pythonw.exe",
    "soundferry.exe",
}


def list_audio_apps() -> list[dict]:
    """列出「可能正在/可以播放声音」的应用，供界面选择。"""
    apps: dict[str, dict] = {}

    # 1) 有音频会话的进程（正在出声或曾经出声）
    try:
        from pycaw.pycaw import AudioUtilities

        for session in AudioUtilities.GetAllSessions():
            pid = getattr(session, "ProcessId", 0) or 0
            if pid <= 0:
                continue
            name = _process_name(pid)
            if not name or name.lower() in _SKIP_EXES:
                continue
            key = name.lower()
            entry = apps.setdefault(key, {"exe": name, "pids": [], "label": name, "playing": False})
            entry["pids"].append(pid)
            if int(getattr(session, "State", 0) or 0) == 1:
                entry["playing"] = True
    except Exception:  # noqa: BLE001
        LOGGER.debug("枚举音频会话失败", exc_info=True)

    # 2) 有可见窗口的进程（应用可能在暂停状态，没有会话）
    for pid in _visible_window_pids():
        name = _process_name(pid)
        if not name or name.lower() in _SKIP_EXES:
            continue
        key = name.lower()
        entry = apps.setdefault(key, {"exe": name, "pids": [], "label": name, "playing": False})
        if pid not in entry["pids"]:
            entry["pids"].append(pid)

    for entry in apps.values():
        entry["pid"] = entry["pids"][0]
        entry["label"] = entry["exe"]

    return sorted(apps.values(), key=lambda a: (not a["playing"], a["exe"].lower()))


def find_pids_by_exe(exe_name: str) -> list[int]:
    """按进程名找 pid（用于「记住上次的应用」）。"""
    target = exe_name.lower()
    pids: list[int] = []
    try:
        import psutil

        for proc in psutil.process_iter(["pid", "name"]):
            if (proc.info.get("name") or "").lower() == target:
                pids.append(int(proc.info["pid"]))
    except Exception:  # noqa: BLE001
        LOGGER.debug("按进程名查找失败", exc_info=True)
    return pids


# ---- 会话级音量控制 ---------------------------------------------------------
# 「指定应用」模式的本机静音策略：
#   进程回环抓到的是「会话音量处理之后」的信号（实测：会话静音 -> 采集变 0），
#   所以不能直接静音目标应用的会话，否则推流也没了。
#   改为把会话音量压到一个极小的值（默认 0.1%，约 -60dB，电脑上听不见），
#   采集端用 float32 格式抓取并乘回 1/quiet 精确还原 —— 其他应用完全不受影响。

QUIET_VOLUME = 0.001


def enumerate_render_sessions() -> list:
    """遍历所有活动渲染端点上的音频会话，返回 IAudioSessionControl2 列表。

    蓝牙等端点有时会卡住 COM 调用，所以整体放在带超时的线程里执行。
    """
    controls: list = []

    def work() -> None:
        try:
            import comtypes
            from comtypes import CLSCTX_ALL, CoInitialize

            CoInitialize()
            from pycaw.pycaw import AudioUtilities, IMMDeviceEnumerator, IAudioSessionControl2
            from pycaw.constants import EDataFlow, DEVICE_STATE

            clsid = comtypes.GUID("{BCDE0395-E52F-467C-8E3D-C4579291692E}")
            enum = comtypes.CoCreateInstance(clsid, IMMDeviceEnumerator, comtypes.CLSCTX_INPROC_SERVER)
            coll = enum.EnumAudioEndpoints(EDataFlow.eRender.value, DEVICE_STATE.ACTIVE.value)
            for i in range(coll.GetCount()):
                try:
                    wrapper = AudioUtilities.CreateDevice(coll.Item(i))
                    session_enum = wrapper.AudioSessionManager.GetSessionEnumerator()
                    for j in range(session_enum.GetCount()):
                        try:
                            controls.append(session_enum.GetSession(j).QueryInterface(IAudioSessionControl2))
                        except Exception:  # noqa: BLE001
                            pass
                except Exception:  # noqa: BLE001
                    pass
        except Exception:  # noqa: BLE001
            LOGGER.debug("枚举音频会话失败", exc_info=True)

    thread = threading.Thread(target=work, daemon=True)
    thread.start()
    thread.join(6.0)
    return controls


def find_app_sessions(exe_name: str) -> list:
    """找出某个应用（按进程名）的音频会话（跨所有输出端点，去重）。"""
    target = (exe_name or "").strip().lower()
    if not target:
        return []

    def pid_matches(pid: int) -> bool:
        if pid <= 0:
            return False
        try:
            import psutil

            return (psutil.Process(pid).name() or "").lower() == target
        except Exception:  # noqa: BLE001
            return False

    seen: set[int] = set()
    sessions: list = []
    for control in enumerate_render_sessions():
        try:
            pid = int(control.GetProcessId())
        except Exception:  # noqa: BLE001
            continue
        if pid in seen or not pid_matches(pid):
            continue
        seen.add(pid)
        sessions.append(control)
    return sessions


class SessionQuiet:
    """把目标应用在其所有输出端点上的会话音量压到极低，结束时恢复。

    用于「指定应用」模式：目标应用在电脑上几乎听不见（约 -60dB），
    其他应用的音量不受任何影响；采集端拿到的是压低后的 float32 信号，
    配合 1/quiet 的补偿增益可精确还原原始音量。
    """

    MARKER = (
        Path(os.environ.get("APPDATA", str(Path.home()))) / "SoundFerry" / "quiet_sessions.json"
    )

    def __init__(self, exe_name: str, quiet_level: float = QUIET_VOLUME) -> None:
        self.exe_name = exe_name
        self.quiet_level = float(quiet_level)
        self._saved: list[tuple] = []  # (ISimpleAudioVolume, 原音量, 原静音, pid)

    def apply(self, retries: int = 3, delay: float = 1.0) -> bool:
        """压低目标应用的会话音量。应用刚启动时会话可能还没注册，所以带重试。"""
        for attempt in range(retries):
            sessions = find_app_sessions(self.exe_name)
            if sessions:
                break
            if attempt < retries - 1:
                time.sleep(delay)
        else:
            return False

        for control in sessions:
            try:
                volume = control.QueryInterface(_get_ISimpleAudioVolume())
                prev_volume = float(volume.GetMasterVolume())
                prev_mute = bool(volume.GetMute())
                pid = int(control.GetProcessId())
                volume.SetMute(0, None)
                volume.SetMasterVolume(self.quiet_level, None)
                self._saved.append((volume, prev_volume, prev_mute, pid))
                LOGGER.info(
                    "已压低会话音量: pid=%d, %.1f%% -> %.2f%%",
                    pid,
                    prev_volume * 100,
                    self.quiet_level * 100,
                )
            except Exception:  # noqa: BLE001
                LOGGER.debug("压低会话音量失败", exc_info=True)
        if self._saved:
            self._write_marker()
        return bool(self._saved)

    def restore(self) -> None:
        for volume, prev_volume, prev_mute, _pid in self._saved:
            try:
                volume.SetMasterVolume(prev_volume, None)
                volume.SetMute(1 if prev_mute else 0, None)
            except Exception:  # noqa: BLE001
                LOGGER.debug("恢复会话音量失败", exc_info=True)
        self._saved.clear()
        try:
            self.MARKER.unlink(missing_ok=True)
        except OSError:
            pass

    # ---- 进程被强杀后的保险 --------------------------------------------
    def _write_marker(self) -> None:
        try:
            self.MARKER.parent.mkdir(parents=True, exist_ok=True)
            self.MARKER.write_text(
                json.dumps(
                    {
                        "quiet": self.quiet_level,
                        "sessions": [
                            {"pid": pid, "volume": v, "mute": 1 if m else 0}
                            for _vol, v, m, pid in self._saved
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
        except OSError:
            LOGGER.debug("写入会话音量标记失败", exc_info=True)


def recover_stale_quiet() -> None:
    """如果上次推流时程序被强杀，把忘记恢复的会话音量还原。"""
    marker = SessionQuiet.MARKER
    try:
        data = json.loads(marker.read_text(encoding="utf-8"))
        quiet = float(data.get("quiet") or QUIET_VOLUME)
        entries = data.get("sessions") or []
    except Exception:  # noqa: BLE001
        return
    if not entries:
        marker.unlink(missing_ok=True)
        return

    fixed = 0
    for entry in entries:
        try:
            pid = int(entry["pid"])
        except Exception:  # noqa: BLE001
            continue
        for control in enumerate_render_sessions():
            try:
                if int(control.GetProcessId()) != pid:
                    continue
                volume = control.QueryInterface(_get_ISimpleAudioVolume())
                if float(volume.GetMasterVolume()) <= quiet * 10:
                    volume.SetMasterVolume(float(entry.get("volume") or 1.0), None)
                    volume.SetMute(int(entry.get("mute") or 0), None)
                    fixed += 1
            except Exception:  # noqa: BLE001
                continue
    if fixed:
        LOGGER.info("已恢复上次未还原的 %d 个应用会话音量", fixed)
    try:
        marker.unlink(missing_ok=True)
    except OSError:
        pass


_ISIMPLE = None


def _get_ISimpleAudioVolume():
    global _ISIMPLE
    if _ISIMPLE is None:
        from pycaw.pycaw import ISimpleAudioVolume

        _ISIMPLE = ISimpleAudioVolume
    return _ISIMPLE
