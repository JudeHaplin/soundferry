"""AirPlay 推流引擎：把实时 PCM 直接灌进 pyatv 的 RAOP 通道。

pyatv 原来的 ``stream_file`` 只接受文件/网络流，必须经过解码器。
这里在它内部 ``open_source`` 的位置注入一个「永不结束的音频源」，
于是 WASAPI 采集到的 PCM 可以在内存里直达 RTP 包，不落地、不编码、不依赖 ffmpeg。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Callable, Optional

import numpy as np

import pyatv
import pyatv.protocols.raop as _raop
from pyatv.const import Protocol
from pyatv.interface import MediaMetadata
from pyatv.protocols.raop.audio_source import AudioSource
from pyatv.protocols.raop.parsers import get_audio_properties
from pyatv.protocols.raop.protocols import StreamContext

from . import APP_DIR_NAME, APP_NAME_ZH
from .audio import AppCapture, LoopbackCapture
from .proc_loopback import SessionQuiet

LOGGER = logging.getLogger(__name__)

# AirPlay 缓冲深度（秒）。值越小延迟越低，但更容易被 Wi-Fi 抖动打断。
LATENCY_PRESETS = {"稳定（1.5 秒）": 1.5, "均衡（1.0 秒）": 1.0, "低延迟（0.6 秒）": 0.6}

_desired_latency_s: float = 1.0
_ORIGINAL_RESET = StreamContext.reset
_PATCHED = False


def _patched_reset(self: StreamContext) -> None:
    _ORIGINAL_RESET(self)
    if _desired_latency_s:
        minimum = 4410  # 0.1 秒，低于此值接收端基本无法工作
        self.latency = max(int(_desired_latency_s * self.sample_rate), minimum)


def install_patches() -> None:
    """把 pyatv 的两处关键行为改成支持实时音频流。"""
    global _PATCHED
    if _PATCHED:
        return

    StreamContext.reset = _patched_reset  # type: ignore[method-assign]

    original_open_source = _raop.open_source

    async def open_source(source, sample_rate, channels, sample_size):
        if isinstance(source, _LiveStreamMarker):
            return source.source
        return await original_open_source(source, sample_rate, channels, sample_size)

    _raop.open_source = open_source  # type: ignore[assignment]
    _PATCHED = True


def set_latency(seconds: float) -> None:
    global _desired_latency_s
    _desired_latency_s = max(0.1, float(seconds))


class _LiveStreamMarker:
    """占位对象，仅用于把自定义音频源偷渡进 pyatv。"""

    def __init__(self, source: AudioSource) -> None:
        self.source = source


class LivePcmSource(AudioSource):
    """从 asyncio 队列里不间断读取 PCM 的音频源。"""

    def __init__(
        self,
        queue: "asyncio.Queue[bytes]",
        *,
        sample_rate: int,
        channels: int,
        sample_size: int,
        stopping: threading.Event,
    ) -> None:
        self.queue = queue
        self._sample_rate = sample_rate
        self._channels = channels
        self._sample_size = sample_size
        self.stopping = stopping
        self._buffer = b""
        self._closed = False

    @property
    def frame_size(self) -> int:
        return self._channels * self._sample_size

    async def readframes(self, nframes: int) -> bytes:
        need = nframes * self.frame_size

        while len(self._buffer) < need:
            if self.stopping.is_set() and self.queue.empty():
                return AudioSource.NO_FRAMES
            try:
                chunk = await asyncio.wait_for(self.queue.get(), timeout=0.5)
            except asyncio.TimeoutError:
                # 采集端没数据（比如设备静默/断流）时补静音，保持时间轴连续，
                # 绝不能让读取返回空，否则 pyatv 会认为音频播完并结束会话。
                chunk = b"\x00" * need
            self._buffer += chunk

        data, self._buffer = self._buffer[:need], self._buffer[need:]
        # RAOP/ALAC 约定使用大端 16 位 PCM：pyatv 自带的音频源都会在这里
        # 做字节序交换（见 pyatv raop/audio_source.py::_to_audio_samples），
        # 我们注入的实时源必须保持一致，否则音箱只会播放出噪音。
        if len(data) >= 2:
            data = np.frombuffer(data, dtype="<i2").byteswap().tobytes()
        return data

    async def get_metadata(self) -> MediaMetadata:
        return MediaMetadata(title="电脑音频")

    @property
    def sample_rate(self) -> int:
        return self._sample_rate

    @property
    def channels(self) -> int:
        return self._channels

    @property
    def sample_size(self) -> int:
        return self._sample_size

    @property
    def duration(self) -> int:
        return 0

    async def close(self) -> None:
        self._closed = True


DEFAULT_ENDPOINT = "@default"


class EndpointMute:
    """临时静音某个输出端点，退出时恢复原状。

    WASAPI 回环采集取的是混音后的数据，不受端点静音影响（OBS 采集桌面声音同理），
    所以静音本机后 AirPlay 那边依然有声音。按进程捕获同理，实测静音后依然能抓到。
    ``friendly_name=None`` 表示操作系统默认输出设备（按应用捕获时用）。
    """

    MARKER = Path(os.environ.get("APPDATA", str(Path.home()))) / APP_DIR_NAME / "muted_endpoint.json"

    def __init__(self, friendly_name: Optional[str] = None) -> None:
        self.friendly_name = friendly_name
        self._device = None
        self._previous: Optional[int] = None
        self._key: str = friendly_name or DEFAULT_ENDPOINT

    def _resolve(self):
        from pycaw.pycaw import AudioUtilities

        if self.friendly_name is None:
            return AudioUtilities.GetSpeakers()
        for device in AudioUtilities.GetAllDevices():
            if getattr(device, "FriendlyName", None) != self.friendly_name:
                continue
            if not str(getattr(device, "state", "")).endswith("Active"):
                continue
            return device
        return None

    def mute(self) -> bool:
        try:
            device = self._resolve()
            if device is None:
                return False
            volume = device.EndpointVolume
            self._previous = int(volume.GetMute())
            volume.SetMute(1, None)
            self._device = device
            try:
                self.MARKER.parent.mkdir(parents=True, exist_ok=True)
                self.MARKER.write_text(
                    json.dumps({"endpoint": self._key}, ensure_ascii=False),
                    encoding="utf-8",
                )
            except OSError:
                LOGGER.debug("写入静音标记失败", exc_info=True)
            return True
        except Exception:  # noqa: BLE001 - 静音失败不算致命错误
            LOGGER.warning("静音本机输出失败", exc_info=True)
        return False

    def restore(self) -> None:
        if self._device is None or self._previous is None:
            return
        try:
            self._device.EndpointVolume.SetMute(self._previous, None)
        except Exception:  # noqa: BLE001
            LOGGER.debug("恢复静音状态失败", exc_info=True)
        finally:
            self._device = None
            self._previous = None
            try:
                self.MARKER.unlink(missing_ok=True)
            except OSError:
                pass


def recover_stale_mute() -> None:
    """如果上次推流时程序被强杀，把忘记恢复的端点静音撤掉。"""
    marker = EndpointMute.MARKER
    try:
        endpoint = json.loads(marker.read_text(encoding="utf-8")).get("endpoint")
    except Exception:  # noqa: BLE001
        return
    if not endpoint:
        return
    try:
        if endpoint == DEFAULT_ENDPOINT:
            from pycaw.pycaw import AudioUtilities

            AudioUtilities.GetSpeakers().EndpointVolume.SetMute(0, None)
            LOGGER.info("已恢复上次未撤销的本机静音（默认输出）")
            marker.unlink(missing_ok=True)
            return

        from pycaw.pycaw import AudioUtilities

        for device in AudioUtilities.GetAllDevices():
            if getattr(device, "FriendlyName", None) != endpoint:
                continue
            if not str(getattr(device, "state", "")).endswith("Active"):
                continue
            device.EndpointVolume.SetMute(0, None)
            LOGGER.info("已恢复上次未撤销的本机静音: %s", endpoint)
            break
        marker.unlink(missing_ok=True)
    except Exception:  # noqa: BLE001
        LOGGER.debug("恢复静音失败", exc_info=True)


class AirPlayDeviceInfo:
    """扫描结果里给界面用的精简信息。"""

    def __init__(self, config) -> None:
        self.config = config
        self.name = config.name
        self.address = str(config.address)
        self.identifier = config.identifier

    def __str__(self) -> str:
        return f"{self.name} ({self.address})"


def scan_devices(timeout: int = 4) -> list[AirPlayDeviceInfo]:
    """同步扫描局域网内的 AirPlay / RAOP 设备。"""
    async def _scan():
        loop = asyncio.get_running_loop()
        configs = await pyatv.scan(loop, timeout=timeout)
        return [AirPlayDeviceInfo(config) for config in configs]

    return asyncio.run(_scan())


class StreamEngine:
    """在后台线程里跑一条完整的推流会话。"""

    def __init__(
        self,
        on_status: Callable[[str], None] | None = None,
        on_level: Callable[[int], None] | None = None,
        on_error: Callable[[str], None] | None = None,
    ) -> None:
        install_patches()
        self.on_status = on_status or (lambda _text: None)
        self.on_level = on_level or (lambda _level: None)
        self.on_error = on_error or (lambda _text: None)

        self._thread: Optional[threading.Thread] = None
        self._stopping = threading.Event()
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._atv = None
        self._capture: Optional[LoopbackCapture] = None
        self._mute: Optional[EndpointMute] = None
        self._quiet: Optional[SessionQuiet] = None
        self._running = False
        self._lock = threading.Lock()
        self._remote_volume_ok = False
        self._desired_volume = 100.0

    # ---- 对外 -----------------------------------------------------------
    @property
    def is_running(self) -> bool:
        with self._lock:
            return self._running

    def start(
        self,
        device_identifier: str,
        capture_spec: dict,
        *,
        latency_s: float = 1.0,
        volume: float = 1.0,
        mute_local: bool = False,
    ) -> None:
        """volume: 音箱音量百分比（0-100），连接成功后立即应用到音箱。"""
        if self.is_running:
            return
        set_latency(latency_s)
        self._stopping.clear()
        with self._lock:
            self._running = True
        self._thread = threading.Thread(
            target=self._thread_main,
            args=(device_identifier, capture_spec, volume, mute_local),
            name="airplay-engine",
            daemon=True,
        )
        self._thread.start()

    def set_volume(self, volume: float) -> None:
        """调节音量（百分比 0-100）。优先控制音箱音量，失败时退回软件增益。"""
        volume = max(0.0, min(100.0, float(volume)))
        with self._lock:
            self._desired_volume = volume
            remote_ok = self._remote_volume_ok
        if remote_ok and self._loop is not None and self._atv is not None:
            try:
                asyncio.run_coroutine_threadsafe(self._remote_set_volume(volume), self._loop)
                return
            except RuntimeError:  # noqa: PERF203 - 事件循环已关闭时走软件音量
                pass
        if self._capture is not None:
            self._capture.set_gain(volume / 100.0)

    async def _remote_set_volume(self, volume: float) -> None:
        try:
            await self._atv.audio.set_volume(volume)
        except Exception:  # noqa: BLE001 - 远程音量失效就退回软件增益
            LOGGER.info("音箱音量设置失败，退回软件音量", exc_info=True)
            with self._lock:
                self._remote_volume_ok = False
            if self._capture is not None:
                self._capture.set_gain(volume / 100.0)
                self.on_status("音箱音量调节失败，改用电脑端音量")

    def stop(self) -> None:
        self._stopping.set()
        if self._capture is not None:
            try:
                self._capture.stop()
            except Exception:  # noqa: BLE001
                LOGGER.debug("停止采集失败", exc_info=True)
        if self._loop is not None and self._atv is not None:
            try:
                self._loop.call_soon_threadsafe(
                    lambda: asyncio.ensure_future(self._safe_remote_stop())
                )
            except RuntimeError:
                pass
        if self._mute is not None:
            self._mute.restore()
            self._mute = None
        if self._quiet is not None:
            self._quiet.restore()
            self._quiet = None

    def wait(self, timeout: float | None = None) -> None:
        if self._thread is not None:
            self._thread.join(timeout)

    # ---- 内部 -----------------------------------------------------------
    def _on_app_lost(self) -> None:
        """按应用捕获时，目标应用退出就主动结束会话。"""
        self.on_status("目标应用已退出，推流结束")
        self.stop()

    async def _safe_remote_stop(self) -> None:
        try:
            await self._atv.remote_control.stop()
        except Exception:  # noqa: BLE001
            LOGGER.debug("发送停止指令失败", exc_info=True)

    def _thread_main(self, device_identifier: str, capture_spec: dict, volume: float, mute_local: bool) -> None:
        try:
            asyncio.run(self._session(device_identifier, capture_spec, volume, mute_local))
        except Exception as exc:  # noqa: BLE001
            LOGGER.exception("推流会话异常结束")
            self.on_error(f"{exc}")
        finally:
            with self._lock:
                self._running = False
            self.on_level(0)

    async def _session(
        self, device_identifier: str, capture_spec: dict, volume: float, mute_local: bool
    ) -> None:
        loop = asyncio.get_running_loop()
        self._loop = loop

        self.on_status("正在查找音箱…")
        configs = await pyatv.scan(loop, timeout=5, identifier=device_identifier)
        if not configs:
            raise RuntimeError("没有找到这台 AirPlay 音箱，请确认电脑和音箱在同一个 Wi-Fi。")
        config = configs[0]

        service = config.get_service(Protocol.RAOP) or config.get_service(Protocol.AirPlay)
        if service is None:
            raise RuntimeError(f"{config.name} 没有提供 AirPlay 音频服务。")
        sample_rate, channels, bytes_per_channel = get_audio_properties(service.properties)
        channels = min(channels, 2)
        self.on_status(
            f"正在连接 {config.name}（{sample_rate} Hz / {channels} 声道）…"
        )

        queue: "asyncio.Queue[bytes]" = asyncio.Queue(maxsize=32)

        def push(data: bytes) -> None:
            def _put() -> None:
                if queue.full():
                    try:
                        queue.get_nowait()  # 丢弃最旧的块，避免延迟越积越高
                    except asyncio.QueueEmpty:
                        pass
                try:
                    queue.put_nowait(data)
                except asyncio.QueueFull:
                    pass

            try:
                loop.call_soon_threadsafe(_put)
            except RuntimeError:
                pass

        if capture_spec.get("mode") == "app":
            pid = int(capture_spec["pid"])
            app_name = str(capture_spec.get("name") or f"PID {pid}")
            pre_gain = 1.0

            # 「静音本机」在按应用模式下只静音该应用：把它的会话音量压到极低
            # （电脑上听不见），采集用 float32 抓并乘回补偿增益，其他应用不受影响。
            if mute_local:
                quiet = SessionQuiet(app_name)
                if quiet.apply():
                    self._quiet = quiet
                    pre_gain = 1.0 / quiet.quiet_level
                    LOGGER.info(
                        "已把「%s」在电脑上的音量压到 %.2f%%（其他应用不受影响）",
                        app_name,
                        quiet.quiet_level * 100,
                    )
                else:
                    LOGGER.info("没有找到「%s」的音频会话，本机静音跳过", app_name)
                    self.on_status("未能定位该应用的音频会话，电脑端仍会播放它的声音")

            capture = AppCapture(
                pid,
                target_rate=int(sample_rate),
                target_channels=channels,
                on_audio=push,
                on_level=self.on_level,
                on_lost=self._on_app_lost,
                app_name=app_name,
            )
            capture.set_pre_gain(pre_gain)
            capture.set_gain(1.0)
            capture.start()
            self._capture = capture
            LOGGER.info(
                "采集源: 应用 %s (pid=%d, %d Hz -> %d Hz)",
                app_name,
                pid,
                capture.input_rate,
                capture.target_rate,
            )
        else:
            capture = LoopbackCapture(
                device_index=int(capture_spec.get("index", -1)),
                target_rate=int(sample_rate),
                target_channels=channels,
                on_audio=push,
                on_level=self.on_level,
            )
            capture.set_gain(1.0)
            capture.start()
            self._capture = capture
            LOGGER.info(
                "采集源: [%d] %s (%d Hz -> %d Hz)",
                capture.device.index,
                capture.device.name,
                capture.input_rate,
                capture.target_rate,
            )

            if mute_local and capture.device is not None:
                mute = EndpointMute(capture.device.friendly_name)
                if mute.mute():
                    self._mute = mute
                    LOGGER.info("已静音本机端点: %s", capture.device.friendly_name)

        self.on_status(f"正在连接 {config.name}…")
        atv = await pyatv.connect(config, loop)
        self._atv = atv

        # 音量策略：连接成功后先把音箱音量压到起始值（默认 1%，避免突然炸响），
        # 之后界面上调节音量优先走 AirPlay 音量指令；如果音箱不接受，再退回软件增益。
        connect_volume = max(0.0, min(100.0, float(volume)))
        with self._lock:
            self._desired_volume = connect_volume
            self._remote_volume_ok = False
        try:
            await atv.audio.set_volume(connect_volume)
            with self._lock:
                self._remote_volume_ok = True
            LOGGER.info("音箱音量已设为 %.0f%%", connect_volume)
        except Exception as exc:  # noqa: BLE001 - 不支持远程音量时用软件增益兜底
            LOGGER.info("音箱不接受音量指令(%s)，改用软件音量", exc)
            capture.set_gain(connect_volume / 100.0)

        source = LivePcmSource(
            queue,
            sample_rate=int(sample_rate),
            channels=channels,
            sample_size=bytes_per_channel,
            stopping=self._stopping,
        )

        self.on_status(f"正在推流 → {config.name}")
        title = capture_spec.get("name") if capture_spec.get("mode") == "app" else "电脑声音"
        metadata = MediaMetadata(title=f"{APP_NAME_ZH} · {title}", artist=config.name)

        try:
            # 这里会阻塞到会话结束（流是无限的）
            await atv.stream.stream_file(_LiveStreamMarker(source), metadata=metadata)
        finally:
            self.on_status("正在断开…")
            try:
                pending = atv.close()
                if pending:
                    await asyncio.gather(*pending, return_exceptions=True)
            except TypeError:
                pass
            except Exception:  # noqa: BLE001
                LOGGER.debug("关闭连接失败", exc_info=True)
            self._atv = None
            capture.stop()
            self._capture = None
            if self._mute is not None:
                self._mute.restore()
                self._mute = None
            if self._quiet is not None:
                self._quiet.restore()
                self._quiet = None
            self.on_status("已停止")
