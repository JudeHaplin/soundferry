"""声音采集：两种来源 + 统一的 PCM 处理管线。

来源 A：整个电脑 —— WASAPI 混音回环（pyaudiowpatch / PortAudio）
来源 B：指定应用 —— WASAPI Process Loopback（见 proc_loopback.py），只抓该进程树的声音

两条链路汇合到 PcmProcessor：声道整形 -> soxr 重采样到 44.1kHz -> 软件音量 -> 回调
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from typing import Callable, Optional

import numpy as np
import soxr

LOGGER = logging.getLogger(__name__)

FRAMES_PER_BUFFER = 512  # 512 帧 @48kHz ≈ 10.7ms，兼顾低延迟与稳定性


@dataclass(frozen=True)
class OutputDevice:
    """一个可以被采集的 Windows 输出设备（回环端点）。"""

    index: int
    name: str          # PyAudio 名称，形如 "扬声器 (Realtek(R) Audio) [Loopback]"
    friendly_name: str  # 去掉 [Loopback] 后的名字，用于和 pycaw 的端点匹配
    sample_rate: int
    channels: int
    is_default: bool = False


def _import_pyaudio():
    import pyaudiowpatch as pyaudio

    return pyaudio


def list_output_devices() -> list[OutputDevice]:
    """列出所有可采集的输出设备（含默认输出）。"""
    pyaudio = _import_pyaudio()
    devices: list[OutputDevice] = []

    with pyaudio.PyAudio() as pa:
        try:
            default_index = int(pa.get_default_wasapi_loopback()["index"])
        except Exception:  # noqa: BLE001 - 没有默认设备时不影响列举
            default_index = -1

        for info in pa.get_loopback_device_info_generator():
            raw = str(info["name"])
            friendly = raw.replace("[Loopback]", "").strip()
            devices.append(
                OutputDevice(
                    index=int(info["index"]),
                    name=raw,
                    friendly_name=friendly,
                    sample_rate=int(info["defaultSampleRate"]),
                    channels=int(info["maxInputChannels"]),
                    is_default=int(info["index"]) == default_index,
                )
            )

    return devices


def default_output_device() -> Optional[OutputDevice]:
    for device in list_output_devices():
        if device.is_default:
            return device
    return None


class PcmProcessor:
    """把任意输入格式的 PCM 变成 RAOP 需要的 int16 / 目标声道 / 目标采样率。

    内部统一在 float32 域处理：int16 输入先升精度，float32 输入直进；
    输出统一转回 int16 给 RAOP 通道。
    """

    def __init__(
        self,
        input_rate: int,
        input_channels: int,
        target_rate: int,
        target_channels: int = 2,
        on_audio: Callable[[bytes], None] | None = None,
        on_level: Callable[[int], None] | None = None,
        input_dtype: str = "int16",
    ) -> None:
        self.input_rate = int(input_rate)
        self.input_channels = max(1, int(input_channels))
        self.target_rate = int(target_rate)
        self.target_channels = max(1, min(int(target_channels), 2))
        self.input_dtype = "float32" if input_dtype == "float32" else "int16"
        self.on_audio = on_audio or (lambda _data: None)
        self.on_level = on_level or (lambda _level: None)

        self._gain = 1.0
        self._pre_gain = 1.0  # 会话音量补偿（不可超出正常范围，直接乘）
        self._frames_seen = 0
        if self.input_rate != self.target_rate:
            self._resampler: Optional[soxr.ResampleStream] = soxr.ResampleStream(
                self.input_rate, self.target_rate, self.target_channels, dtype="float32", quality="HQ"
            )
            LOGGER.info("重采样已启用: %d Hz -> %d Hz", self.input_rate, self.target_rate)
        else:
            self._resampler = None

    def set_gain(self, gain: float) -> None:
        self._gain = max(0.0, min(float(gain), 2.0))

    def set_pre_gain(self, gain: float) -> None:
        """补偿会话音量压低带来的衰减（如 1/0.001 = 1000）。"""
        self._pre_gain = max(1e-6, float(gain))

    def feed(self, raw: bytes) -> None:
        try:
            if self.input_dtype == "float32":
                pcm = np.frombuffer(raw, dtype=np.float32).astype(np.float32)
            else:
                pcm = np.frombuffer(raw, dtype=np.int16).astype(np.float32)
            if pcm.size == 0:
                return
            if self.input_channels > 1:
                pcm = pcm.reshape(-1, self.input_channels)
            else:
                pcm = pcm.reshape(-1, 1)

            # 声道整形到目标声道数
            if pcm.shape[1] != self.target_channels:
                if pcm.shape[1] == 1:
                    pcm = np.repeat(pcm, self.target_channels, axis=1)
                else:
                    pcm = pcm[:, : self.target_channels]

            # 先做会话音量补偿（重采样前乘，避免低电平信号在重采样中损失精度）
            if self._pre_gain != 1.0:
                pcm = pcm * self._pre_gain

            if self._resampler is not None:
                pcm = self._resampler.resample_chunk(np.ascontiguousarray(pcm), last=False)

            gain = self._gain
            if gain != 1.0:
                pcm = pcm * gain if gain > 0.0 else np.zeros_like(pcm)

            if pcm.size == 0:
                return

            # 归一化 float32 [-1, 1) -> int16
            np.clip(pcm, -1.0, 1.0, out=pcm)
            out = (pcm * 32767.0).astype(np.int16)
            out = np.ascontiguousarray(out)

            self.on_audio(out.tobytes())

            self._frames_seen += 1
            if self._frames_seen % 3 == 0:
                peak = float(np.max(np.abs(out))) / 32768.0
                self.on_level(int(min(100, peak * 118)))  # 近似 dB 视觉刻度
        except Exception:  # noqa: BLE001 - 音频线程里绝不能让异常冒出去
            LOGGER.exception("音频处理失败")


class LoopbackCapture:
    """实时抓取某个输出设备的混音，并转换成目标格式的 PCM。"""

    def __init__(
        self,
        device_index: int,
        target_rate: int,
        target_channels: int = 2,
        on_audio: Callable[[bytes], None] | None = None,
        on_level: Callable[[int], None] | None = None,
        frames_per_buffer: int = FRAMES_PER_BUFFER,
    ) -> None:
        self.device_index = device_index
        self.target_rate = target_rate
        self.target_channels = max(1, min(int(target_channels), 2))
        self.on_audio = on_audio or (lambda _data: None)
        self.on_level = on_level or (lambda _level: None)
        self.frames_per_buffer = frames_per_buffer

        self.device: Optional[OutputDevice] = None
        self.input_rate = 0
        self.input_channels = 0

        self._gain = 1.0
        self._processor: Optional[PcmProcessor] = None
        self._pa = None
        self._pyaudio = None
        self._stream = None
        self._closing = False

    # ---- 生命周期 -------------------------------------------------------
    def start(self) -> "LoopbackCapture":
        pyaudio = self._import_pyaudio()
        self._pyaudio = pyaudio
        self._pa = pyaudio.PyAudio()

        info = self._resolve_device(self._pa, self.device_index)
        raw_name = str(info["name"])
        self.input_rate = int(info["defaultSampleRate"])
        self.input_channels = int(info["maxInputChannels"])
        self.device = OutputDevice(
            index=int(info["index"]),
            name=raw_name,
            friendly_name=raw_name.replace("[Loopback]", "").strip(),
            sample_rate=self.input_rate,
            channels=self.input_channels,
        )
        self._processor = PcmProcessor(
            self.input_rate,
            self.input_channels,
            self.target_rate,
            self.target_channels,
            self.on_audio,
            self.on_level,
        )
        self._processor.set_gain(self._gain)

        self._stream = self._pa.open(
            format=self._pyaudio.paInt16,
            channels=self.input_channels,
            rate=self.input_rate,
            frames_per_buffer=self.frames_per_buffer,
            input=True,
            input_device_index=self.device.index,
            stream_callback=self._callback,
        )
        return self

    def stop(self) -> None:
        self._closing = True
        if self._stream is not None:
            try:
                if self._stream.is_active():
                    self._stream.stop_stream()
                self._stream.close()
            except Exception:  # noqa: BLE001 - 关闭失败不应影响退出
                LOGGER.debug("关闭采集流时出错", exc_info=True)
            self._stream = None
        if self._pa is not None:
            try:
                self._pa.terminate()
            except Exception:  # noqa: BLE001
                LOGGER.debug("终止 PyAudio 时出错", exc_info=True)
            self._pa = None

    # ---- 音量 -----------------------------------------------------------
    def set_gain(self, gain: float) -> None:
        """软件音量（1.0 = 原始音量）。设备端不支持 RAOP 调音量时用这个。"""
        self._gain = max(0.0, min(float(gain), 2.0))
        if self._processor is not None:
            self._processor.set_gain(self._gain)

    # ---- 内部 -----------------------------------------------------------
    def _callback(self, in_data, frame_count, time_info, status):
        pyaudio = self._pyaudio
        if self._closing:
            return (None, pyaudio.paComplete)
        if self._processor is not None:
            self._processor.feed(in_data)
        return (None, pyaudio.paContinue)

    def _import_pyaudio(self):
        return _import_pyaudio()

    @staticmethod
    def _resolve_device(pa, device_index: int) -> dict:
        info = pa.get_device_info_by_index(device_index)
        if info.get("isLoopbackDevice"):
            return info
        if hasattr(pa, "get_wasapi_loopback_analogue_by_index"):
            return pa.get_wasapi_loopback_analogue_by_index(device_index)
        for loopback in pa.get_loopback_device_info_generator():
            if str(info["name"]) in str(loopback["name"]):
                return loopback
        raise RuntimeError(f"设备 {device_index} 找不到对应的回环设备。")


class AppCapture:
    """只抓取指定应用（进程树）输出的声音。"""

    def __init__(
        self,
        pid: int,
        target_rate: int,
        target_channels: int = 2,
        on_audio: Callable[[bytes], None] | None = None,
        on_level: Callable[[int], None] | None = None,
        on_lost: Callable[[], None] | None = None,
        app_name: str = "",
    ) -> None:
        self.pid = int(pid)
        self.app_name = app_name or f"PID {pid}"
        self.target_rate = int(target_rate)
        self.target_channels = max(1, min(int(target_channels), 2))
        self.on_audio = on_audio or (lambda _data: None)
        self.on_level = on_level or (lambda _level: None)
        self.on_lost = on_lost or (lambda: None)

        self.input_rate = 0
        self.input_channels = 2
        self.device: Optional[OutputDevice] = None

        self._gain = 1.0
        self._pre_gain = 1.0
        self._processor: Optional[PcmProcessor] = None
        self._capture = None
        self._watchdog: Optional[threading.Thread] = None
        self._closing = threading.Event()

    def start(self) -> "AppCapture":
        from .proc_loopback import ProcessLoopbackCapture

        self._capture = ProcessLoopbackCapture(
            self.pid,
            sample_rate=self.target_rate,
            channels=self.target_channels,
            on_audio=self._on_chunk,
        )
        self._capture.start()
        self.input_rate = self._capture.sample_rate
        self.input_channels = self._capture.channels
        LOGGER.info("应用采集已启动: %s (pid=%d, %d Hz)", self.app_name, self.pid, self.input_rate)

        self._watchdog = threading.Thread(target=self._watch, name="app-watchdog", daemon=True)
        self._watchdog.start()
        return self

    def stop(self) -> None:
        self._closing.set()
        if self._capture is not None:
            try:
                self._capture.stop()
            except Exception:  # noqa: BLE001
                LOGGER.debug("停止应用采集失败", exc_info=True)
            self._capture = None

    def set_gain(self, gain: float) -> None:
        self._gain = max(0.0, min(float(gain), 2.0))
        if self._processor is not None:
            self._processor.set_gain(self._gain)

    def set_pre_gain(self, gain: float) -> None:
        """会话音量补偿（配合 SessionQuiet 使用）。"""
        self._pre_gain = max(1e-6, float(gain))
        if self._processor is not None:
            self._processor.set_pre_gain(self._pre_gain)

    # ---- 内部 -----------------------------------------------------------
    def _on_chunk(self, raw: bytes) -> None:
        if self._closing.is_set():
            return
        if self._processor is None:
            rate = self._capture.sample_rate if self._capture is not None else self.target_rate
            channels = self._capture.channels if self._capture is not None else self.target_channels
            dtype = "float32" if (self._capture is not None and self._capture.is_float) else "int16"
            self._processor = PcmProcessor(
                rate,
                channels,
                self.target_rate,
                self.target_channels,
                self.on_audio,
                self.on_level,
                input_dtype=dtype,
            )
            self._processor.set_gain(self._gain)
            self._processor.set_pre_gain(self._pre_gain)
        self._processor.feed(raw)

    def _watch(self) -> None:
        """目标应用退出后主动收尾，避免一直推静音。"""
        while not self._closing.wait(1.0):
            try:
                import psutil

                if not psutil.pid_exists(self.pid):
                    LOGGER.info("目标应用已退出: %s (pid=%d)", self.app_name, self.pid)
                    self.on_lost()
                    return
            except Exception:  # noqa: BLE001
                return
