# 机器车硬件说明文档

> 本文档记录搭载于树莓派 5 机器车上的所有硬件模块的详细规格、接口定义与接线约定，供后续开发和调试参考。

---

## 目录

1. [主控板 - 树莓派 5 (8GB)](#1-主控板---树莓派-5-8gb)
2. [功能扩展板 - MakerRobo](#2-功能扩展板---makerrobo)
3. [激光雷达 - LD06](#3-激光雷达---ld06)
4. [激光雷达转换模块 - CP2102 USB转TTL](#4-激光雷达转换模块---cp2102-usb转ttl)
5. [电源管理模块 - INA219 DC电流传感器](#5-电源管理模块---ina219-dc电流传感器)
6. [音频模块 - USB免驱录音/外放](#6-音频模块---usb免驱录音外放)
7. [GPIO 使用总览与冲突分析](#7-gpio-使用总览与冲突分析)

---

## 1. 主控板 - 树莓派 5 (8GB)

| 参数 | 值 |
|------|-----|
| 型号 | Raspberry Pi 5 |
| 内存 | 8GB LPDDR4X |
| 操作系统 | Raspberry Pi OS（官方系统） |
| 主要对外接口 | USB-A × 4、USB-C（电源）、GPIO 40pin、CSI/DSI、HDMI × 2、千兆以太网、Wi-Fi 5 / BT 5.0 |

**在本项目中的角色：**
- 运行 Node.js/Next.js Agent 层（端口 3000）
- 运行 Python FastAPI Bridge 层（端口 8001）
- 通过 GPIO 与扩展板通信，通过 USB 接入激光雷达和音频模块

---

## 2. 功能扩展板 - MakerRobo

**品牌/型号：** MakerRobo 功能扩展板（插接于树莓派 40pin GPIO）

### 板载接口一览

| 接口名称 | 数量 | 说明 |
|----------|------|------|
| 超声波模块接口 | 1 | 标准 4pin HC-SR04 兼容 |
| 蜂鸣器 | 1 | 板载有源蜂鸣器 |
| IIC 总线接口（OLED 用） | 2 | 引出 GND / 5V / SCL / SDA，可接 OLED 屏 |
| GPIO 接口 | 1 | 引出 5V / GND / GP23 / GP13 / GP16 / GP18 / GP11 |
| 红外循迹接口 | 多路 | 底部循迹传感器接口 |
| PWM 舵机接口 | 2路 | 标准 3pin 舵机（PWM 信号控制） |
| 电机驱动接口 | 4路（M1-M4） | 连接 4 个直流电机，通过板载电机驱动 IC 控制 |
| 上层功能板 GPIO 口 | 1 | GP19 / GP10 / GP04 / 5V / GND / DC-SW / DC+ |
| 电源接口 | 1 | 6V-8.7V 直流电源输入（电池供电） |
| GP0 接口 | 1 | GND / +5V / GP1GP0 |

### 电源说明

- 电池电源由 **电源接口（6V-8.7V）** 输入，板载 DC-DC 降压为 5V 供树莓派及外设
- 树莓派 5V 与扩展板 5V 共地

### 在本项目中的角色

- 驱动 4 路直流电机（差速控制车轮）
- 预留超声波传感器、红外循迹传感器接口
- 通过 IIC 接口扩展 OLED 状态屏（可选）
- Platform 层通过 GPIO 引脚控制电机驱动 IC（`platform/devices/motor_test.py` 对应测试脚本）

---

## 3. 激光雷达 - LD06

**型号：** LD06  
**类型：** 360° 旋转 DTOF 激光扫描测距雷达  
**品牌：** 原件库存全新品牌  
**连接器：** ZH1.5T-4P 1.5mm（随机附赠 4P 端子线）

### 电气与机械参数

| 参数 | 最小值 | 典型值 | 最大值 | 备注 |
|------|--------|--------|--------|------|
| 输入电压 | 4.5V | 5V | 5.5V | |
| PWM 控制频率 | 20KHz | 30KHz | 50KHz | 方波信号 |
| PWM 高电平 | 3.0V | 3.3V | 5.0V | |
| PWM 低电平 | -0.3V | 0V | 0.5V | |
| PWM 占空比 | 0% | 40% | 100% | 40% 占空比扫描频率为 10Hz |
| 启动电流 | — | 300mA | — | |
| 工作电流 | — | 180mA | — | |
| 整机尺寸 | — | 38.59 × 38.59 × 33.50 mm | — | 长 × 宽 × 高 |
| 整机重量 | — | 42g | — | 不含连接线 |
| 通信接口 | — | UART @ 230400 | — | |
| UART 高电平 | 2.9V | 3.3V | 3.5V | |
| UART 低电平 | -0.3V | 0V | 0.4V | |
| 驱动电机 | — | BLDC | — | 无刷电机 |
| 工作温度 | -10℃ | 25℃ | 40℃ | |
| 存储温度 | -30℃ | 25℃ | 70℃ | |

### 测距性能参数

| 参数 | 最小值 | 典型值 | 最大值 | 备注 |
|------|--------|--------|--------|------|
| 测距范围 | 0.02m | — | 12m | 70% 目标反射率 |
| 扫描频率 | 5Hz | 10Hz | 13Hz | 外部提供 PWM 控速 |
| 测距频率 | — | 4500Hz | — | 固定频率 |
| 测距精度（近距） | — | — | — | 测距 < 300mm 时暂无精度要求 |
| 测距精度（中远） | — | — | ±45mm | 测距范围 300mm ~ 12000mm |
| 测距标准差 | — | 10mm | — | 测距范围 300mm ~ 12000mm |
| 测量分辨率 | — | 15mm | — | |
| 角度误差 | — | — | 2° | |
| 角度分辨率 | — | 1° | — | |
| 抗环境光 | — | — | 30KLux | |
| 整机寿命 | 10000h | — | — | |

### 数据接口（ZH1.5T-4P 引脚定义）

| 序号 | 信号名 | 类型 | 描述 | 电平范围 |
|------|--------|------|------|----------|
| 1 | Tx | 输出 | 雷达数据输出（UART） | 0V ~ 3.5V |
| 2 | PWM | 输入 | 电机控制信号（转速调节） | 0V ~ 3.3V |
| 3 | GND | 供电 | 电源负极 | — |
| 4 | P5V | 供电 | 电源正极 | 4.5V ~ 5.5V |

### UART 通信协议

| 波特率 | 数据长度 | 停止位 | 奇偶校验位 | 流控 |
|--------|----------|--------|------------|------|
| 230400 | 8 Bits | 1 | 无 | 无 |

- **单向通信**：LD06 单向发送，稳定旋转后即开始发送测量数据，不需要发送任何指令
- 每帧数据包为 **47 字节**（见 `platform/devices/lidar.py` 协议解析实现）
- 坐标系：左手坐标系，正前方为 X 轴（0 角度位置），旋转角度沿顺时针方向增大

### PWM 转速控制说明

- 支持内部控速（PWM 引脚不接或接高阻信号，默认内部调速，默认转速 10Hz）
- 外部控速：在 PWM 引脚输入方波信号，通过占空比控制电机启停和转速
- 由于每个产品电机的个体差异，占空比设置为典型值时实际转速可能有差异，精确控制需闭环

### 在本项目中的角色

- 提供 360° 环境点云数据，通过 `platform/lidar_sensor.py` 接入 SLAM 引擎（breezyslam）
- 通过 CP2102 USB-TTL 模块连接到树莓派 USB 口，在系统中注册为串口设备（如 `/dev/ttyUSB0`）

---

## 4. 激光雷达转换模块 - CP2102 USB转TTL

**用途：** 将 LD06 激光雷达的 UART（TTL电平）信号转换为 USB，连接树莓派

**主芯片：** Silicon Labs CP2102  
**USB 接口：** Type-C（免驱，即插即用）  
**主机侧：** 标准 USB-A 公头

### 尺寸

| 参数 | 值 |
|------|-----|
| 整体长度 | 40mm |
| USB 插头部分 | 25mm |
| 宽度 | 14mm |

### 电气参数

| 参数 | 值 |
|------|-----|
| 信号电平 | 3.3V TTL 正逻辑 |
| 支持波特率 | 300bps ~ 1Mbps（覆盖 LD06 的 230400） |
| USB 取电输出 | 3.3V（<40mA）、5V |
| 通信格式 | 5/6/7/8 位数据位；1/1.5/2 停止位；odd/even/mark/space/none 校验 |
| 支持系统 | Windows Vista/XP/Server、Mac OS-X/OS-9、Linux |

### 引脚定义

| 引脚 | 说明 |
|------|------|
| 3.3V | 电源正（<40mA） |
| 5V | 电源正 |
| TXD | 串口发送端（接雷达 RXD，但 LD06 无需接收，此脚空置） |
| RXD | 串口接收端（接雷达 Tx） |
| GND | 地 |

### 接线约定（LD06 → CP2102 → 树莓派）

```
LD06 Pin4 (P5V)  →  CP2102 5V 或外部 5V 供电
LD06 Pin3 (GND)  →  CP2102 GND
LD06 Pin1 (Tx)   →  CP2102 RXD
LD06 Pin2 (PWM)  →  悬空（使用内部默认转速 10Hz）或接树莓派 GPIO PWM
CP2102 USB-A     →  树莓派 USB-A 口
```

> **注意 TXD/RXD 交叉原则**：TXD 接另一设备的 RXD，RXD 接另一设备的 TXD。正常通信时自身的 TXD 永远接设备的 RXD。

### 在本项目中的角色

- 连接后在树莓派中识别为 `/dev/ttyUSB0`（或 `/dev/ttyACM0`）
- Bridge 层 `platform/devices/lidar.py` 打开该串口，波特率 230400，进行协议解析

---

## 5. 电源管理模块 - INA219 DC电流传感器

**型号：** INA219 DC Current Sensor  
**用途：** 实时监测机器车电池或供电回路的电压、电流、功率

### 主要参数

| 参数 | 值 |
|------|-----|
| 通信接口 | I2C |
| 供电电压 | 3.3V / 5V |
| 测量范围（电流） | 视分流电阻而定（板载 R100 = 0.1Ω，典型量程约 ±3.2A） |
| 测量范围（总线电压） | 0 ~ 26V |
| I2C 地址 | 可配置（板载 A0/A1 跳线，默认 0x40） |

### 引脚定义

| 引脚 | 说明 |
|------|------|
| Vin- | 分流电阻负端（接负载侧） |
| Vin+ | 分流电阻正端（接电源侧） |
| GND | 地 |
| SDA | I2C 数据 |
| SCL | I2C 时钟 |

### 接线约定

```
树莓派 3.3V  →  模块 Vcc（通过 IIC 总线接口）
树莓派 GND   →  模块 GND
树莓派 SDA   →  模块 SDA（GPIO2，物理引脚 3）
树莓派 SCL   →  模块 SCL（GPIO3，物理引脚 5）
电源正极     →  Vin+
负载正极     →  Vin-（串联接入供电回路）
```

> 可使用扩展板上的 IIC 总线接口（GND / 5V / SCL / SDA）连接，无需直接占用树莓派 GPIO。

### 在本项目中的角色

- 监测电池电量/电流，为 Agent 提供电源状态感知能力
- 通过 I2C 读取，可在 Python Bridge 层使用 `smbus2` 或 `pi-ina219` 库访问

---

## 6. 音频模块 - USB免驱录音/外放

**类型：** USB 免驱声卡（录音 + 外放）  
**接口：** Type-C USB（连接树莓派 USB-A 口）  
**驱动：** 免驱，即插即用（USB Audio Class 标准设备）

### 主要特性

- USB 免驱，支持 Linux（树莓派官方系统开箱即用）
- 同时支持录音（麦克风输入）和外放（扬声器输出）
- 接入树莓派后注册为 ALSA 音频设备

### 在本项目中的角色

- **录音（Sensory）**：Bridge 层 `platform/audio_sensor.py` 通过 PyAudio/sounddevice 采集 PCM 音频，经 WebRTC VAD 检测语音活动，将完整语音块作为 `sense.audio.speech_end` 事件发布到 Spine
- **外放（Action）**：Bridge 层 `platform/audio_effector.py` 接收 TTS 合成结果（edge-tts），通过 ALSA 播放至扬声器
- 设备名称查询：`aplay -l`（播放设备）、`arecord -l`（录音设备）

### 采样率策略（代码层）

`platform/devices/microphone.py` 会在启动时自动探测设备支持的采样率：

1. 优先尝试 **16000Hz**（webrtcvad 原生支持，零开销）
2. 若不支持，自动回退到 **48000Hz** + 3:1 均值抽取降采样至 16000Hz

```
arecord -l            # 查看 ALSA 已注册的录音设备
aplay -l              # 查看播放设备
python3 platform/devices/microphone_test.py --probe   # 探测设备支持的采样率
```

### 注意事项

- 树莓派 5 官方系统默认音频设备可能是 HDMI 或板载音频，使用前需确认 ALSA 默认设备指向 USB 声卡
- 可通过 `/etc/asound.conf` 或 `~/.asoundrc` 设置默认设备：

```
# ~/.asoundrc 示例（将 USB 声卡设为默认）
defaults.pcm.card 1
defaults.ctl.card 1
```

- 具体卡号（card 0/1/2）通过 `aplay -l` 确认
- `platform/devices/microphone.py` 中 `find_usb_audio_device()` 会扫描设备名含 `usb` 的输入设备自动匹配，免去手动配置

---

## 硬件连接总览

```
树莓派 5 (8GB)
├── GPIO 40pin  ──────────────────→  MakerRobo 功能扩展板
│                                      ├── 4路电机（M1-M4）
│                                      ├── 超声波模块
│                                      ├── 红外循迹模块
│                                      ├── 2路PWM舵机
│                                      └── IIC总线 → INA219 电流传感器
│
├── USB-A (port 1)  ──────────────→  CP2102 USB转TTL模块
│                                      └── LD06 激光雷达（UART 230400）
│
├── USB-A (port 2)  ──────────────→  USB免驱音频模块（录音/外放）
│
├── USB-C (电源) ─────────────────→  5V/5A 电源适配器（或由扩展板 DC-DC 供电）
│
└── MicroSD  ─────────────────────→  树莓派官方系统

电池（6V-8.7V）  ──────────────────→  MakerRobo 扩展板电源接口
                                        └── DC-DC 降压 → 5V → 树莓派 + 外设
```

---

---

## 7. GPIO 使用总览与冲突分析

> 所有引脚编号均使用 **BCM 编号**（代码中 `GPIO.setmode(GPIO.BCM)`），与物理引脚编号不同。

### 7.1 已占用引脚（当前代码实际使用）

| BCM 编号 | 物理引脚 | 功能描述 | 所属模块 | 代码文件 | 备注 |
|----------|---------|---------|---------|---------|------|
| GPIO 2 | Pin 3 | SDA1（I2C 数据） | INA219 电源传感器 | `platform/devices/power_sensor.py` | I2C 总线，硬件复用允许挂多设备 |
| GPIO 3 | Pin 5 | SCL1（I2C 时钟） | INA219 电源传感器 | `platform/devices/power_sensor.py` | I2C 总线，同上 |
| GPIO 5 | Pin 29 | M3 IN1（PWM 正转） | 左后轮电机 | `platform/devices/chassis.py` | SW-6008 驱动，直接 IN PWM 模式 |
| GPIO 6 | Pin 31 | M3 IN2（PWM 反转） | 左后轮电机 | `platform/devices/chassis.py` | 同上 |
| GPIO 9 | Pin 21 | M4 IN2（PWM 反转） | 右后轮电机 | `platform/devices/chassis.py` | ⚠️ 兼 SPI0 MISO，禁止启用 SPI0 |
| GPIO 12 | Pin 32 | PWM0 — 云台水平轴 Pan | 摄像头云台舵机 | `platform/devices/servo.py` | 硬件 PWM，50Hz |
| GPIO 13 | Pin 33 | PWM1 — 云台垂直轴 Tilt | 摄像头云台舵机 | `platform/devices/servo.py` | ⚠️ 硬件 PWM；扩展板 GPIO 接口也引出此脚，勿再外接 |
| GPIO 22 | Pin 15 | M4 IN1（PWM 正转） | 右后轮电机 | `platform/devices/chassis.py` | SW-6008 驱动 |
| GPIO 24 | Pin 18 | M1 IN1（PWM 正转） | 左前轮电机 | `platform/devices/chassis.py` | SW-6008 驱动 |
| GPIO 25 | Pin 22 | M1 IN2（PWM 反转） | 左前轮电机 | `platform/devices/chassis.py` | SW-6008 驱动 |
| GPIO 26 | Pin 37 | M2 IN2（PWM 反转） | 右前轮电机 | `platform/devices/chassis.py` | SW-6008 驱动 |
| GPIO 27 | Pin 13 | M2 IN1（PWM 正转） | 右前轮电机 | `platform/devices/chassis.py` | SW-6008 驱动 |

**USB 设备（不占用 GPIO 引脚）：**

| 设备节点 | 用途 | 模块 |
|---------|------|------|
| `/dev/ttyUSB0` | LD06 激光雷达数据输入 | `platform/devices/lidar.py` |
| USB Audio Card | 录音/外放 | `platform/audio_sensor.py` / `platform/audio_effector.py` |

---

### 7.2 扩展板对外引出的 GPIO 接口

扩展板将部分树莓派 GPIO 通过排针引出，供外接传感器或模块使用。

| 接口标识（扩展板丝印） | BCM 编号 | 物理引脚 | 当前占用状态 | 说明 |
|----------------------|---------|---------|------------|------|
| GP23 | GPIO 23 | Pin 16 | **空闲** | 可用 |
| GP13 | GPIO 13 | Pin 33 | **⚠️ 已被舵机 Tilt 占用** | 禁止外接任何设备 |
| GP16 | GPIO 16 | Pin 36 | **空闲** | 可用 |
| GP18 | GPIO 18 | Pin 12 | **空闲** | 可用（兼 PCM CLK / PWM） |
| GP11 | GPIO 11 | Pin 23 | **空闲** | 兼 SPI0 CLK；若启用 SPI0 则不可用 |
| GP19（上层板） | GPIO 19 | Pin 35 | **空闲** | 兼 SPI1 MISO / PWM1 |
| GP10（上层板） | GPIO 10 | Pin 19 | **空闲** | 兼 SPI0 MOSI；若启用 SPI0 则不可用 |
| GP04（上层板） | GPIO 4 | Pin 7 | **空闲** | 可用 |
| GP0 | GPIO 0 | Pin 27 | **空闲** | EEPROM SDA，通常不用于普通 GPIO |
| GP1 | GPIO 1 | Pin 28 | **空闲** | EEPROM SCL，通常不用于普通 GPIO |

---

### 7.3 冲突与风险提示

> ⚠️ = 存在冲突或潜在风险，开发前必须确认。

| 风险等级 | BCM 引脚 | 冲突描述 | 处理建议 |
|---------|---------|---------|---------|
| 🔴 高 | GPIO 13 | 舵机 Tilt 使用了此引脚，但扩展板 GPIO 接口也将其引出，如果外接传感器并同时运行舵机代码，会发生 PWM 信号被干扰或烧毁的风险 | 严禁在 GP13 排针上连接任何设备 |
| 🟡 中 | GPIO 9 | 电机 M4 IN2 占用 SPI0 MISO。若未来需要 SPI 设备（如 OLED SPI 版），SPI0 不可用 | 避免启用 SPI0；SPI 设备优先选 SPI1（GPIO 19/20/21）或 I2C 接口版本 |
| 🟡 中 | GPIO 10/11 | 扩展板 GP10/GP11 已引出，这两个引脚兼 SPI0 MOSI/CLK | 启用 SPI0 前需确认 GP10/GP11 接口上没有连接其他设备 |
| 🟢 低 | GPIO 2/3 | I2C 总线已被 INA219 使用，扩展板 IIC 接口也引出同一总线 | I2C 支持多设备挂载（不同 I2C 地址），注意 INA219 默认地址 0x40，新设备选其他地址即可 |

---

### 7.4 空闲 GPIO 汇总（可用于扩展）

以下引脚当前未被任何模块占用，可安全用于新传感器或外设：

| BCM 编号 | 物理引脚 | 兼复用功能 | 推荐用途 |
|---------|---------|----------|---------|
| GPIO 4 | Pin 7 | — | 通用数字输入输出（如 DHT 温湿度传感器） |
| GPIO 16 | Pin 36 | — | 通用数字输入输出 |
| GPIO 17 | Pin 11 | — | 通用数字输入输出 |
| GPIO 18 | Pin 12 | PWM / PCM CLK | 若需第三路 PWM 可用此脚 |
| GPIO 19 | Pin 35 | SPI1 MISO / PWM1 | 可用于 SPI1 设备 |
| GPIO 20 | Pin 38 | SPI1 MOSI | 可用于 SPI1 设备 |
| GPIO 21 | Pin 40 | SPI1 CLK | 可用于 SPI1 设备 |
| GPIO 23 | Pin 16 | — | 通用数字输入输出 |

> **UART 提示**：GPIO 14（Pin 8, TX）和 GPIO 15（Pin 10, RX）为 UART0，树莓派官方系统默认用于串口控制台，如需接串口设备须先在 `raspi-config` 中禁用控制台。

---

### 7.5 树莓派 5 GPIO 引脚全图（快速参考）

```
         3V3  (1) (2)  5V
   SDA1 GPIO2  (3) (4)  5V          ← INA219 SDA
   SCL1 GPIO3  (5) (6)  GND         ← INA219 SCL
        GPIO4  (7) (8)  GPIO14 TXD  ← 空闲 | UART0 TX（系统控制台）
          GND  (9) (10) GPIO15 RXD  ← 空闲 | UART0 RX（系统控制台）
       GPIO17 (11) (12) GPIO18      ← 空闲 | 空闲(PWM)
       GPIO27 (13) (14) GND         ← M2 IN1（右前轮）
       GPIO22 (15) (16) GPIO23      ← M4 IN1（右后轮） | 空闲
         3V3 (17) (18) GPIO24       ← M1 IN1（左前轮）
GPIO10/MOSI (19) (20) GND          ← 空闲(SPI0 MOSI)
 GPIO9/MISO (21) (22) GPIO25       ← M4 IN2（右后轮，⚠️SPI0 MISO） | M1 IN2（左前轮）
GPIO11/SCLK (23) (24) GPIO8/CE0   ← 空闲(SPI0 CLK) | 空闲
          GND (25) (26) GPIO7/CE1  ← 空闲
    GPIO0/ID_SD (27) (28) GPIO1/ID_SC  ← EEPROM（勿用于普通GPIO）
        GPIO5 (29) (30) GND         ← M3 IN1（左后轮）
        GPIO6 (31) (32) GPIO12/PWM0 ← M3 IN2（左后轮） | 舵机 Pan（云台水平）
  GPIO13/PWM1 (33) (34) GND         ← 舵机 Tilt（云台垂直，⚠️扩展板GP13已引出）
       GPIO19 (35) (36) GPIO16      ← 空闲(SPI1 MISO) | 空闲
       GPIO26 (37) (38) GPIO20      ← M2 IN2（右前轮） | 空闲(SPI1 MOSI)
          GND (39) (40) GPIO21      ← 空闲(SPI1 CLK)

图例：← 描述  |  ⚠️ 冲突风险
```

---

*文档最后更新：2026-03-13*
