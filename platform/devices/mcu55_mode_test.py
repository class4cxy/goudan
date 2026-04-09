"""
MCU-55 / BNO055 模式诊断脚本
==============================

用途：
  一次运行同时检测 I2C 与 UART，快速判断 MCU-55 当前更可能处于哪种模式：
    - 标准 I2C（常见地址 0x29/0x28）
    - HID-I2C（常见地址 0x40）
    - UART（BNO055 UART 协议可读 CHIP_ID=0xA0）

说明：
  1) 本脚本不会改写传感器寄存器，只做探测。
  2) I2C 探测依赖系统命令 `i2cdetect`（i2c-tools）。
  3) UART 探测依赖 `pyserial`。

用法示例：
  python3 mcu55_mode_test.py
  python3 mcu55_mode_test.py --port /dev/ttyUSB1
  python3 mcu55_mode_test.py --i2c-bus 1
  python3 mcu55_mode_test.py --skip-i2c
  python3 mcu55_mode_test.py --skip-uart
"""

from __future__ import annotations

import argparse
import glob
import os
import re
import shutil
import subprocess
import time
from dataclasses import dataclass


DEF_PORT = os.environ.get("IMU_SERIAL_PORT", "/dev/ttyUSB1")
DEF_BAUD = 115200
DEF_I2C_BUS = 1

# BNO055 UART 协议
_START = 0xAA
_READ = 0x01
_RESP_READ = 0xBB
_REG_CHIP_ID = 0x00
_BNO055_CHIP_ID = 0xA0


@dataclass
class I2cProbeResult:
    ok: bool
    reason: str
    addresses: set[int]


@dataclass
class UartProbeResult:
    ok: bool
    reason: str
    chip_id: int | None


def _run_i2cdetect(bus: int) -> I2cProbeResult:
    if shutil.which("i2cdetect") is None:
        return I2cProbeResult(
            ok=False,
            reason="系统未安装 i2cdetect（i2c-tools）",
            addresses=set(),
        )

    cmd = ["i2cdetect", "-y", str(bus)]
    try:
        out = subprocess.check_output(cmd, text=True, stderr=subprocess.STDOUT, timeout=5.0)
    except subprocess.CalledProcessError as exc:
        return I2cProbeResult(
            ok=False,
            reason=f"i2cdetect 执行失败：{exc.output.strip()}",
            addresses=set(),
        )
    except Exception as exc:
        return I2cProbeResult(
            ok=False,
            reason=f"i2cdetect 异常：{exc}",
            addresses=set(),
        )

    addresses: set[int] = set()
    for token in re.findall(r"\b[0-9a-fA-F]{2}\b", out):
        if token.lower() in {"00", "01", "02", "03", "04", "05", "06", "07", "08", "09", "0a", "0b", "0c", "0d", "0e", "0f"}:
            # 跳过表头数字，不加入地址集合
            continue
        addresses.add(int(token, 16))

    return I2cProbeResult(ok=True, reason="i2cdetect 运行成功", addresses=addresses)


def _uart_read_chip_id(port: str, baud: int) -> UartProbeResult:
    try:
        import serial
    except ImportError:
        return UartProbeResult(ok=False, reason="pyserial 未安装（pip install pyserial）", chip_id=None)

    try:
        ser = serial.Serial(port, baudrate=baud, timeout=0.5)
    except Exception as exc:
        return UartProbeResult(ok=False, reason=f"串口打开失败：{exc}", chip_id=None)

    try:
        # 上电后给芯片一点稳定时间
        time.sleep(0.25)
        ser.reset_input_buffer()
        ser.write(bytes([_START, _READ, _REG_CHIP_ID, 0x01]))

        header = ser.read(2)
        if len(header) < 2:
            return UartProbeResult(ok=False, reason="UART 无响应（超时）", chip_id=None)
        if header[0] != _RESP_READ:
            return UartProbeResult(
                ok=False,
                reason=f"响应头异常：{header.hex()}（非 BNO055 UART 帧）",
                chip_id=None,
            )

        payload_len = header[1]
        payload = ser.read(payload_len)
        if len(payload) < payload_len:
            return UartProbeResult(
                ok=False,
                reason=f"数据不足（期望 {payload_len}，实际 {len(payload)}）",
                chip_id=None,
            )

        chip_id = payload[0] if payload_len > 0 else None
        if chip_id == _BNO055_CHIP_ID:
            return UartProbeResult(ok=True, reason="UART 读 CHIP_ID 成功", chip_id=chip_id)
        return UartProbeResult(
            ok=False,
            reason=f"读到 CHIP_ID=0x{chip_id:02X}（期望 0xA0）" if chip_id is not None else "无 CHIP_ID",
            chip_id=chip_id,
        )
    except Exception as exc:
        return UartProbeResult(ok=False, reason=f"UART 异常：{exc}", chip_id=None)
    finally:
        ser.close()


def _guess_mode(i2c_res: I2cProbeResult | None, uart_res: UartProbeResult | None) -> str:
    i2c_addrs = i2c_res.addresses if i2c_res else set()
    has_29 = 0x29 in i2c_addrs
    has_28 = 0x28 in i2c_addrs
    has_40 = 0x40 in i2c_addrs
    uart_ok = bool(uart_res and uart_res.ok)

    if uart_ok and not (has_29 or has_28 or has_40):
        return "最可能是 UART 模式（S0=1, S1=0）"
    if (has_29 or has_28) and not uart_ok:
        return "最可能是标准 I2C 模式（S0=0, S1=0）"
    if has_40 and not (has_29 or has_28) and not uart_ok:
        return "最可能是 HID-I2C 模式（S0=0, S1=1）"
    if uart_ok and (has_29 or has_28 or has_40):
        return "I2C 与 UART 都有响应，可能接线/模块逻辑与资料不一致，需复核"
    return "未能确定模式（可能供电/焊接/接线问题）"


def main() -> None:
    parser = argparse.ArgumentParser(description="MCU-55/BNO055 模式诊断脚本（I2C + UART）")
    parser.add_argument("--port", default=DEF_PORT, help=f"UART 串口设备（默认 {DEF_PORT}）")
    parser.add_argument("--baud", type=int, default=DEF_BAUD, help=f"UART 波特率（默认 {DEF_BAUD}）")
    parser.add_argument("--i2c-bus", type=int, default=DEF_I2C_BUS, help=f"I2C 总线号（默认 {DEF_I2C_BUS}）")
    parser.add_argument("--skip-i2c", action="store_true", help="跳过 I2C 探测")
    parser.add_argument("--skip-uart", action="store_true", help="跳过 UART 探测")
    args = parser.parse_args()

    print("\n" + "=" * 68)
    print(" MCU-55 / BNO055 模式诊断")
    print("=" * 68)

    ports = sorted(glob.glob("/dev/ttyUSB*") + glob.glob("/dev/ttyACM*"))
    print("\n[串口设备]")
    if ports:
        for p in ports:
            hint = "  <-- 当前目标端口" if p == args.port else ""
            print(f"  - {p}{hint}")
    else:
        print("  - 未发现 /dev/ttyUSB* 或 /dev/ttyACM*")

    i2c_res: I2cProbeResult | None = None
    uart_res: UartProbeResult | None = None

    if not args.skip_i2c:
        print(f"\n[I2C 探测] bus={args.i2c_bus}")
        i2c_res = _run_i2cdetect(args.i2c_bus)
        if not i2c_res.ok:
            print(f"  ❌ {i2c_res.reason}")
        else:
            print(f"  ✅ {i2c_res.reason}")
            key_addrs = [0x28, 0x29, 0x40]
            for a in key_addrs:
                state = "命中" if a in i2c_res.addresses else "--"
                print(f"  - 0x{a:02X}: {state}")
    else:
        print("\n[I2C 探测] 已跳过")

    if not args.skip_uart:
        print(f"\n[UART 探测] port={args.port} baud={args.baud}")
        uart_res = _uart_read_chip_id(args.port, args.baud)
        if uart_res.ok:
            print(f"  ✅ {uart_res.reason}，CHIP_ID=0x{uart_res.chip_id:02X}")
        else:
            print(f"  ❌ {uart_res.reason}")
    else:
        print("\n[UART 探测] 已跳过")

    print("\n[结论]")
    print(f"  { _guess_mode(i2c_res, uart_res) }")

    print("\n[建议]")
    print("  - 若要 UART：确保 ATX->CP2102 RXD、LRX->CP2102 TXD 且共地。")
    print("  - 若 UART 无响应但 I2C 有地址：说明当前仍在 I2C/HID-I2C 模式。")
    print("  - 若都无响应：优先检查供电、焊点、线序、以及模块是否上电稳定。")
    print()


if __name__ == "__main__":
    main()
