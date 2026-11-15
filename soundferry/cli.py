"""命令行模式：不打开界面直接推流（也用于自检）。"""

from __future__ import annotations

import argparse
import logging
import sys
import time

from .audio import list_output_devices
from .engine import LATENCY_PRESETS, StreamEngine, scan_devices


def _setup_logging(debug: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if debug else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="soundferry", description="把 Windows 声音推给 AirPlay 音箱")
    parser.add_argument("--list", action="store_true", help="列出局域网里的 AirPlay 设备")
    parser.add_argument("--sources", action="store_true", help="列出可采集的输出设备")
    parser.add_argument("--apps", action="store_true", help="列出可以单独投送的应用")
    parser.add_argument("--device", help="AirPlay 设备标识（MAC）")
    parser.add_argument("--source", type=int, help="采集设备索引")
    parser.add_argument("--app", help="只投送指定应用（进程名，如 QQMusic.exe）")
    parser.add_argument("--seconds", type=float, default=0, help="运行多少秒后自动停止，0 表示一直运行")
    parser.add_argument("--volume", type=float, default=1.0, help="连接后把音箱音量设为多少（百分比，0-100）")
    parser.add_argument("--latency", choices=list(LATENCY_PRESETS), default="均衡（1.0 秒）")
    parser.add_argument("--mute-local", action="store_true", help="推流时静音本机输出")
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args(argv)

    _setup_logging(args.debug)

    if args.list:
        devices = scan_devices()
        if not devices:
            print("没有发现 AirPlay 设备。请确认电脑与音箱在同一 Wi-Fi。")
            return 1
        for device in devices:
            print(f"{device.name}\t{device.address}\t{device.identifier}")
        return 0

    if args.sources:
        for device in list_output_devices():
            mark = "*" if device.is_default else " "
            print(f"{mark} [{device.index}] {device.name}  {device.sample_rate}Hz {device.channels}ch")
        return 0

    if args.apps:
        from .proc_loopback import list_audio_apps

        for item in list_audio_apps():
            print(f"{item['exe']}\tpid={item['pid']}\t{'播放中' if item['playing'] else ''}")
        return 0

    if not args.device or (args.source is None and not args.app):
        parser.print_help()
        return 1

    capture: dict = {"mode": "app"}
    if args.app:
        from .proc_loopback import find_pids_by_exe

        pids = find_pids_by_exe(args.app)
        if not pids:
            print(f"没有找到正在运行的 {args.app}", file=sys.stderr)
            return 1
        capture.update({"pid": pids[0], "name": args.app})
    else:
        capture = {"mode": "device", "index": int(args.source)}

    engine = StreamEngine(
        on_status=lambda text: print(f"[状态] {text}", flush=True),
        on_level=lambda level: None,
        on_error=lambda text: print(f"[错误] {text}", file=sys.stderr, flush=True),
    )
    engine.start(
        args.device,
        capture,
        latency_s=LATENCY_PRESETS[args.latency],
        volume=args.volume,
        mute_local=args.mute_local,
    )

    started = time.monotonic()
    try:
        while engine.is_running:
            time.sleep(0.2)
            if args.seconds and time.monotonic() - started > args.seconds:
                print("[状态] 到达测试时长，正在停止…", flush=True)
                break
    except KeyboardInterrupt:
        print("\n[状态] 收到 Ctrl+C，正在停止…", flush=True)

    engine.stop()
    engine.wait(timeout=8)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
