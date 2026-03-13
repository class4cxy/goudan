# Platform — 硬件抽象层

Python FastAPI 服务，运行在树莓派 5 上（端口 8001）。负责硬件驱动与 Node.js 之间的双向通信，屏蔽所有底层 GPIO / 音频 / 摄像头细节。

---

## 启动

```bash
cd platform
pip install -r requirements.txt
uvicorn main:app --host 0.0.0.0 --port 8001 --reload
```

---

## 通信协议

### WebSocket（实时事件流）

端点：`ws://localhost:8001/ws`

Node.js 作为**客户端**主动连接，Bridge 作为服务端。

**Bridge → Node.js（Inbound，传感器事件）：**

```json
{ "type": "sense.audio.speech_start", "payload": {} }
{ "type": "sense.audio.speech_end",   "payload": { "audio_b64": "...", "duration_ms": 2300 } }
{ "type": "sense.video.frame",        "payload": { "frame_b64": "..." } }
```

**Node.js → Bridge（Outbound，行动指令）：**

```json
{ "type": "action.speak",   "payload": { "text": "好的，正在清扫客厅" } }
{ "type": "action.capture", "payload": {} }
{ "type": "action.patrol",  "payload": { "rooms": ["客厅"] } }
{ "type": "action.motor",   "payload": { "left": 0.5, "right": 0.5, "duration_ms": 1000 } }
```

### REST API（同步指令，Agent 工具直接调用）

**石头扫地机（通过 roborock-python）：**

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/status` | 机器人当前状态 |
| GET | `/rooms` | 房间列表 |
| POST | `/clean/full` | 全屋清扫 |
| POST | `/clean/rooms` | 指定房间清扫 |
| POST | `/clean/pause` | 暂停 |
| POST | `/clean/resume` | 继续 |
| POST | `/home` | 回充 |
| GET | `/history` | 清扫历史 |

**机器车电机：**

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/motor/command` | 发送运动指令（前进/后退/转向） |
| POST | `/motor/set` | 直接设置左右轮速度 |
| GET | `/motor/status` | 当前电机状态 |

**系统：**

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/health` | 健康检查 |
| GET | `/camera/snapshot` | 拍照（ffmpeg + RTSP） |

---

## 设备驱动（`devices/`）

| 文件 | 类 | 硬件 |
|------|-----|------|
| `motor.py` / `chassis.py` | `Chassis` | 4 轮差速底盘（L298N 驱动）|
| `servo.py` | `ServoController` | 双轴云台（摄像头舵机）|
| `microphone.py` | `Microphone` | USB 麦克风（sounddevice 回调）|
| `speaker.py` | `Speaker` | 扬声器（edge-tts + playsound）|
| `camera.py` | `Camera` | USB / RTSP 摄像头 |
| `gpio_adapter.py` | — | RPi.GPIO 适配器（含 Fake GPIO fallback）|

树莓派未连接时，`gpio_adapter.py` 自动切换为 Fake GPIO，方便本地开发调试。

---

## 音频处理流程

```
麦克风 PCM 流（16kHz / 16bit）
  │
  ▼
WebRTC VAD（webrtcvad）
  ├─ 静音帧 → 累积静音计数
  └─ 语音帧 → 加入音频缓冲
        │
        ▼ 静音超过阈值（说话结束）
  打包 PCM → base64 编码 → WebSocket 发送
```
