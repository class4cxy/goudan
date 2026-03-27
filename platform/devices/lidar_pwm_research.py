"""
LD06 PWM 引脚有效性研究脚本（临时，验证完成后可删除）
=======================================================
目标：验证 LD06 PWM 引脚是否能被外部信号控制电机转速/停止。

背景：
  实测发现 GPIO12 输出 25kHz PWM（sysfs 硬件 PWM，万用表确认信号到达引脚），
  但 LD06 电机转速始终 ~597 RPM，不随占空比变化。

  当前假设（按可能性排序）：
    A. 3.3V 逻辑电平不足（LD06 VCC=5V，VIH 可能 = 3.5V，GPIO 输出 3.3V 被识别为 LOW）
    B. PWM 需要在 LD06 上电瞬间就存在（启动时序问题）
    C. 该批次 LD06 固件关闭了外部 PWM 接受功能

用法：
  python lidar_pwm_research.py              # 交互式菜单
  python lidar_pwm_research.py --test 1    # RPM 实时监控（基线）
  python lidar_pwm_research.py --test 2    # sysfs PWM 占空比扫描
  python lidar_pwm_research.py --test 3    # 引导式 5V 电平测试

前提：
  sudo chmod -R a+rw /sys/class/pwm/pwmchip0/
  echo 0 | sudo tee /sys/class/pwm/pwmchip0/export
"""

import argparse
import struct
import sys
import time
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent.parent))

SERIAL_PORT  = "/dev/ttyUSB0"
BAUD_RATE    = 230400
PWM_PATH     = "/sys/class/pwm/pwmchip0/pwm0"
PWM_PERIOD   = 40000   # ns → 25kHz
DIVIDER      = "─" * 60


# ── 串口 RPM 读取器（后台线程）──────────────────────────────────────

class RpmReader:
    def __init__(self, port: str = SERIAL_PORT):
        self._port   = port
        self._rpm    = 0.0
        self._count  = 0
        self._lock   = threading.Lock()
        self._stop   = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self):
        import serial
        self._ser = serial.Serial(self._port, BAUD_RATE, timeout=1)
        self._thread.start()
        return self

    def stop(self):
        self._stop.set()
        self._thread.join(timeout=2)
        try:
            self._ser.close()
        except Exception:
            pass

    def rpm(self) -> float:
        with self._lock:
            return self._rpm

    def _run(self):
        buf = b""
        while not self._stop.is_set():
            try:
                buf += self._ser.read(100)
            except Exception:
                break
            while len(buf) >= 47:
                if buf[0] == 0x54 and buf[1] == 0x2C:
                    val = struct.unpack_from("<H", buf, 2)[0] * 60.0 / 360.0
                    with self._lock:
                        self._rpm   = val
                        self._count += 1
                    buf = buf[47:]
                else:
                    buf = buf[1:]


# ── sysfs PWM 工具 ──────────────────────────────────────────────────

def pwm_setup():
    """初始化 sysfs PWM 通道（需已 export）。"""
    import os
    if not os.path.exists(PWM_PATH):
        raise RuntimeError(
            f"{PWM_PATH} 不存在，请先运行：\n"
            f"  echo 0 | sudo tee /sys/class/pwm/pwmchip0/export\n"
            f"  sudo chmod -R a+rw /sys/class/pwm/pwmchip0/pwm0/"
        )


def pwm_write(attr: str, val: int):
    with open(f"{PWM_PATH}/{attr}", "w") as f:
        f.write(str(val))


def pwm_start(duty_pct: float):
    """启动 sysfs PWM，duty_pct 为 0.0~100.0。"""
    duty_ns = int(PWM_PERIOD * duty_pct / 100.0)
    pwm_write("period",     PWM_PERIOD)
    pwm_write("duty_cycle", duty_ns)
    pwm_write("enable",     1)


def pwm_stop():
    """停止 sysfs PWM（disable，引脚回到 pull-down LOW）。"""
    try:
        pwm_write("duty_cycle", 0)
        pwm_write("enable",     0)
    except Exception:
        pass


# ── 测试 1：RPM 实时监控（基线，不操作 PWM）────────────────────────

def test_monitor(duration: int = 30):
    """持续打印 RPM，用于手动拔插线观察变化。"""
    print(f"\n{DIVIDER}")
    print("  测试 1 — RPM 实时监控（手动拔插 PWM 线时观察变化）")
    print(f"  串口：{SERIAL_PORT}  持续 {duration}s，Ctrl+C 提前停止")
    print(DIVIDER)
    print()
    print("  操作指引：")
    print("    当前状态：PWM 线接哪里都行（悬空 / GPIO12 / 5V / GND）")
    print("    改变接线后观察 RPM 是否变化")
    print("    悬空 ≈ 597 RPM（内部调速基线）")
    print()

    try:
        reader = RpmReader().start()
    except Exception as e:
        print(f"  ❌ 串口打开失败：{e}")
        return

    t0 = time.time()
    try:
        while time.time() - t0 < duration:
            time.sleep(1)
            print(f"  [{int(time.time()-t0):3d}s]  RPM: {reader.rpm():6.1f}")
    except KeyboardInterrupt:
        pass
    finally:
        reader.stop()
    print()


# ── 测试 2：sysfs PWM 占空比扫描 ────────────────────────────────────

def test_pwm_sweep():
    """
    依次输出不同占空比，每档停留 5s，观察 RPM 是否跟随变化。
    PWM 线需接在 GPIO12（Pin 32）。
    """
    print(f"\n{DIVIDER}")
    print("  测试 2 — sysfs 25kHz PWM 占空比扫描")
    print(f"  要求：PWM 线接 GPIO12（Pin 32），并已用 pinctrl set 12 a0 恢复 PWM 模式")
    print(DIVIDER)

    try:
        pwm_setup()
    except RuntimeError as e:
        print(f"  ❌ {e}")
        return

    try:
        reader = RpmReader().start()
    except Exception as e:
        print(f"  ❌ 串口打开失败：{e}")
        return

    duties = [0, 10, 30, 50, 60, 80, 100]
    print(f"\n  {'占空比':>8}  {'目标Hz':>8}  {'实测RPM':>10}  {'变化?':>8}")
    print(f"  {'─'*8}  {'─'*8}  {'─'*10}  {'─'*8}")

    baseline = None
    try:
        for duty in duties:
            pwm_start(duty)
            time.sleep(5)
            rpm = reader.rpm()
            if baseline is None:
                baseline = rpm
            delta = rpm - baseline
            mark = "✅ 有变化！" if abs(delta) > 20 else "— 无变化"
            print(f"  {duty:>7}%  {'25kHz':>8}  {rpm:>10.1f}  {mark}  (Δ{delta:+.1f})")
    except KeyboardInterrupt:
        pass
    finally:
        pwm_stop()
        reader.stop()

    print()
    print("  结论提示：")
    print("    若所有档位 RPM 均约 597 → LD06 不响应 3.3V PWM（电平不足或功能禁用）")
    print("    若高占空比 RPM 更高 → PWM 有效，但之前软件实现有问题")


# ── 测试 3：引导式 5V 电平测试 ──────────────────────────────────────

def test_5v_level():
    """
    验证假设 A：3.3V 逻辑电平不足（GPIO HIGH=3.3V < LD06 VIH=3.5V）。
    引导用户手动将 PWM 线接到 5V / GND，观察 RPM 变化。
    """
    print(f"\n{DIVIDER}")
    print("  测试 3 — 5V 电平验证（验证假设 A：3.3V 电平不足）")
    print(f"  原理：LD06 VCC=5V，VIH 可能 = 3.5V；GPIO 输出 3.3V 可能被识别为 LOW")
    print(DIVIDER)
    print()

    try:
        reader = RpmReader().start()
    except Exception as e:
        print(f"  ❌ 串口打开失败：{e}")
        return

    steps = [
        ("悬空（基线）",         "将 PWM 线从任何引脚拔下，悬空"),
        ("接 GND（Pin 6）",      "将 PWM 线插到树莓派 GND 引脚（Pin 6）"),
        ("接 3.3V（Pin 1）",     "将 PWM 线插到树莓派 3.3V 引脚（Pin 1）"),
        ("接 5V（Pin 2）",       "将 PWM 线插到树莓派 5V 引脚（Pin 2）⚠️ 注意别短路"),
        ("悬空（复位）",         "将 PWM 线从 5V 引脚拔下，悬空"),
    ]

    results = []
    try:
        for label, instruction in steps:
            print(f"  {'─'*50}")
            print(f"  [{label}]")
            print(f"  操作：{instruction}")
            print(f"  按回车继续...", end="")
            input()
            time.sleep(2)   # 等待稳定
            rpms = []
            for _ in range(5):
                time.sleep(0.5)
                rpms.append(reader.rpm())
            avg = sum(rpms) / len(rpms) if rpms else 0
            results.append((label, avg))
            print(f"  → RPM = {avg:.1f}")
            print()
    except KeyboardInterrupt:
        pass
    finally:
        reader.stop()

    if len(results) < 2:
        return

    print(f"\n  {'─'*50}")
    print("  ─── 结果汇总 ─────────────────────────────")
    for label, rpm in results:
        print(f"  {label:<20}  RPM = {rpm:.1f}")

    print()
    baseline = results[0][1] if results else 597
    for label, rpm in results:
        delta = rpm - baseline
        if abs(delta) > 20:
            print(f"  ✅ [{label}] RPM 变化 {delta:+.1f} → PWM 引脚有效！")
            if "5V" in label:
                print("     → 确认假设 A：需要 5V 逻辑电平（加电平转换器可解决）")
        else:
            print(f"  — [{label}] RPM 无明显变化（Δ{delta:+.1f}）")

    if all(abs(r - baseline) < 20 for _, r in results[1:]):
        print()
        print("  → 所有电平均无效，倾向假设 C：该 LD06 固件禁用了外部 PWM 控制")


# ── 测试 4：上电时序测试 ──────────────────────────────────────────

def test_startup_timing():
    """
    验证假设 B：PWM 需要在 LD06 上电瞬间就存在。
    流程：先输出 PWM，然后引导用户重新上电 LD06（拔插供电线）。
    """
    print(f"\n{DIVIDER}")
    print("  测试 4 — 上电时序测试（验证假设 B）")
    print(f"  原理：先输出 PWM 信号，再给 LD06 上电，观察 RPM 是否变化")
    print(DIVIDER)

    try:
        pwm_setup()
    except RuntimeError as e:
        print(f"  ❌ {e}")
        return

    print()
    print("  步骤 1：先确认 PWM 线接在 GPIO12（Pin 32）")
    print("  步骤 2：按回车，脚本先输出 25kHz @ 60% PWM...", end="")
    input()

    pwm_start(60)
    print("  ✅ PWM 已输出（GPIO12 正在发送 25kHz @ 60%）")
    print()
    print("  步骤 3：现在拔掉 LD06 的供电线（5V），等 3 秒，再插回去")
    print("  （让 LD06 在 PWM 信号已存在的情况下重新上电）")
    print("  操作完成后按回车...", end="")
    input()

    print()
    print("  步骤 4：监控 RPM 10 秒...")

    try:
        reader = RpmReader().start()
    except Exception as e:
        pwm_stop()
        print(f"  ❌ 串口打开失败：{e}")
        return

    try:
        for i in range(10):
            time.sleep(1)
            print(f"  [{i+1:2d}s]  RPM: {reader.rpm():6.1f}  (25kHz@60% PWM 输出中)")
    except KeyboardInterrupt:
        pass
    finally:
        reader.stop()
        pwm_stop()

    print()
    print("  结论：若 RPM 明显不同于 597 → 假设 B 成立（时序问题）")
    print("        若仍然 ~597 → 假设 C 成立（固件禁用）")


# ── 主入口 ─────────────────────────────────────────────────────────

def main():
    global SERIAL_PORT

    parser = argparse.ArgumentParser(description="LD06 PWM 有效性研究")
    parser.add_argument("--test", type=int, default=0, help="直接运行指定测试（1-4）")
    parser.add_argument("--port", default=SERIAL_PORT, help="串口设备")
    args = parser.parse_args()

    SERIAL_PORT = args.port

    print("\n╔══════════════════════════════════════════════════════════╗")
    print("║     LD06 PWM 引脚有效性研究（临时测试文件）              ║")
    print("║  验证完成后可直接删除本文件                              ║")
    print("╚══════════════════════════════════════════════════════════╝")

    if args.test:
        {1: test_monitor, 2: test_pwm_sweep,
         3: test_5v_level, 4: test_startup_timing}.get(args.test, lambda: print("无效编号"))()
        return

    while True:
        print(f"\n{DIVIDER}")
        print("  [1] RPM 实时监控（手动拔插线观察）")
        print("  [2] sysfs PWM 占空比扫描（GPIO12，验证 25kHz 是否有效）")
        print("  [3] 5V 电平测试（验证假设 A：3.3V 不足）")
        print("  [4] 上电时序测试（验证假设 B：PWM 需在上电时存在）")
        print("  [0] 退出")
        print(DIVIDER)
        print("  请选择：", end="")
        choice = input().strip()

        if choice == "0":
            break
        elif choice == "1":
            test_monitor()
        elif choice == "2":
            test_pwm_sweep()
        elif choice == "3":
            test_5v_level()
        elif choice == "4":
            test_startup_timing()
        else:
            print(f"  无效选项：{choice}")


if __name__ == "__main__":
    main()
