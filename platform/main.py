"""
Roborock Bridge — Python FastAPI 服务（适配 python-roborock v4.x）
将 python-roborock 封装为本地 REST API，供 Node.js Agent 调用。

启动：uvicorn main:app --host 0.0.0.0 --port 8001 --reload

认证优先级：
  1. Token 文件（.roborock_token.json）— 运行 login_once.py 生成，推荐
  2. 环境变量中的密码（ROBOROCK_PASSWORD）— 次选，密码明文传到 Roborock 服务器
"""

import asyncio
import json
import logging
import os
import time
import urllib.parse
import urllib.request
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from dataclasses import replace
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from audio_effector import AudioEffector
from lidar_sensor import LidarSensor
from slam import SlamEngine, SlamConfig, config_from_env
from devices import (
    Chassis, DEFAULT_CONFIG,
    CameraMount, DEFAULT_CAMERA_CONFIG,
    Camera, CaptureConfig,
    BluetoothManager,
    PowerSensor, PowerSensorConfig, PowerReading,
    Ultrasonic, UltrasonicConfig, UltrasonicReading,
)

from roborock.data import UserData
from roborock.devices.device import RoborockDevice
from roborock.devices.device_manager import DeviceManager, UserParams, create_device_manager
from roborock.roborock_typing import RoborockCommand
from roborock.web_api import RoborockApiClient

load_dotenv(dotenv_path=Path(__file__).parent.parent / ".env")

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# 建图调试：EXPLORER_DEBUG_LOG=1 时，记录 lidar/slam/ultrasonic 请求的响应摘要
EXPLORER_DEBUG_LOG = os.environ.get("EXPLORER_DEBUG_LOG") == "1"

TOKEN_FILE = Path(__file__).parent / ".roborock_token.json"

REGION_URLS = {
    "cn": "https://cniot.roborock.com",
    "eu": "https://euiot.roborock.com",
    "us": "https://usiot.roborock.com",
    "ru": "https://ruiot.roborock.com",
}

# ── WebSocket 连接管理器 ───────────────────────────────────────────
class ConnectionManager:
    """管理所有 WebSocket 长连接（Node.js BridgeConnector 会连入）。"""

    def __init__(self):
        self.active: list[WebSocket] = []

    async def connect(self, ws: WebSocket) -> None:
        await ws.accept()
        self.active.append(ws)
        logger.info(f"[WS] 新连接，当前连接数：{len(self.active)}")

    def disconnect(self, ws: WebSocket) -> None:
        try:
            self.active.remove(ws)
        except ValueError:
            pass
        logger.info(f"[WS] 连接断开，当前连接数：{len(self.active)}")

    async def broadcast(self, message: dict) -> None:
        """向所有连接广播消息（感官事件 → Spine）。"""
        if not self.active:
            msg_type = message.get("type", "unknown")
            trace = message.get("payload", {}).get("trace_id", "")
            logger.warning("[WS] broadcast 无活跃连接，消息丢弃：type=%s%s",
                           msg_type, f"  trace={trace}" if trace else "")
            return
        dead: list[WebSocket] = []
        for ws in self.active:
            try:
                await ws.send_json(message)
            except Exception as e:
                msg_type = message.get("type", "unknown")
                trace = message.get("payload", {}).get("trace_id", "")
                logger.error("[WS] broadcast 发送失败：type=%s%s  error=%s",
                             msg_type, f"  trace={trace}" if trace else "", e)
                dead.append(ws)
        for ws in dead:
            self.active.remove(ws)
            try:
                await ws.close()
            except Exception:
                pass

    async def send(self, ws: WebSocket, message: dict) -> None:
        await ws.send_json(message)


ws_manager = ConnectionManager()
audio_effector = AudioEffector(ws_manager=ws_manager)
bluetooth_manager = BluetoothManager()
_chassis_speed = int(os.environ.get("CHASSIS_DEFAULT_SPEED", "35"))
chassis = Chassis(replace(DEFAULT_CONFIG, default_speed=_chassis_speed))
camera  = CameraMount(DEFAULT_CAMERA_CONFIG)

# 摄像头采集实例（source / snapshot_dir 从环境变量覆盖）
_cam_source_env = os.environ.get("CAMERA_SOURCE", "")
_cam_source: int | str = (
    int(_cam_source_env) if _cam_source_env.isdigit() else (_cam_source_env or 0)
)
cam = Camera(CaptureConfig(
    source=_cam_source,
    snapshot_dir=os.environ.get("CAMERA_SNAPSHOT_DIR", "/tmp/roborock_snapshots"),
))

# SLAM 引擎 + 激光雷达应用层（配置可从 SLAM_MAP_QUALITY 等环境变量覆盖）
slam_engine  = SlamEngine(config_from_env())
lidar_sensor = LidarSensor(ws_manager, slam_engine)

# 电源传感器（INA219，低电量时 WebSocket 广播报警）
# loop 在 _startup() 中赋值，回调在子线程里通过 run_coroutine_threadsafe 提交
_main_loop: asyncio.AbstractEventLoop | None = None

def _on_reading(reading: PowerReading) -> None:
    loop = _main_loop
    if loop and loop.is_running():
        asyncio.run_coroutine_threadsafe(
            ws_manager.broadcast({
                "type": "sense.power.reading",
                "payload": reading.to_dict(),
            }),
            loop,
        )

def _on_low_battery(reading: PowerReading) -> None:
    loop = _main_loop
    if loop and loop.is_running():
        asyncio.run_coroutine_threadsafe(
            ws_manager.broadcast({
                "type": "sense.power.low_battery",
                "payload": {
                    **reading.to_dict(),
                    "threshold_pct": float(os.environ.get("POWER_LOW_BATTERY_PCT", "20")),
                    "message": f"电量不足！当前电量 {reading.battery_pct:.0f}%（{reading.voltage_v:.2f}V）",
                },
            }),
            loop,
        )
power_sensor = PowerSensor(
    config=PowerSensorConfig(
        poll_interval_s=float(os.environ.get("POWER_POLL_INTERVAL", "5")),
        low_battery_pct=float(os.environ.get("POWER_LOW_BATTERY_PCT", "20")),
    ),
    on_reading=_on_reading,
    on_low_battery=_on_low_battery,
)

def _on_ultrasonic_too_close(reading: UltrasonicReading) -> None:
    loop = _main_loop
    if loop and loop.is_running():
        asyncio.run_coroutine_threadsafe(
            ws_manager.broadcast({
                "type": "sense.ultrasonic.too_close",
                "payload": {
                    **reading.to_dict(),
                    "threshold_cm": float(os.environ.get("ULTRASONIC_TOO_CLOSE_CM", "25")),
                    "message": f"前方障碍过近：{reading.distance_cm:.1f} cm",
                },
            }),
            loop,
        )

ultrasonic = Ultrasonic(
    config=UltrasonicConfig(
        trig_pin=int(os.environ.get("ULTRASONIC_TRIG_PIN", "20")),
        echo_pin=int(os.environ.get("ULTRASONIC_ECHO_PIN", "21")),
        poll_interval_s=float(os.environ.get("ULTRASONIC_POLL_INTERVAL", "0.2")),
        too_close_threshold_cm=float(os.environ.get("ULTRASONIC_TOO_CLOSE_CM", "25")),
    ),
    on_too_close=_on_ultrasonic_too_close,
)


# ── 全局状态 ──────────────────────────────────────────────────────
state: dict[str, Any] = {
    "device_manager": None,  # DeviceManager
    "device": None,          # RoborockDevice (第一台 T7)
    "rooms": {},             # {room_name: segment_id}
    "room_ids": {},          # {segment_id: room_name}
    "ready": False,
}


# ── Token 加载 ────────────────────────────────────────────────────
def _load_token_file() -> tuple[str, str | None, UserData] | None:
    """从 .roborock_token.json 加载缓存的登录 token。"""
    if not TOKEN_FILE.exists():
        return None
    try:
        payload = json.loads(TOKEN_FILE.read_text())
        username = payload["username"]
        base_url = payload.get("base_url")
        user_data = UserData.from_dict(payload["user_data"])
        logger.info("✅ 从 token 文件加载登录态（无需密码）")
        return username, base_url, user_data
    except Exception as e:
        logger.warning(f"token 文件读取失败（{e}），将回退到密码登录")
        return None


# ── 房间信息刷新 ──────────────────────────────────────────────────
async def _refresh_rooms():
    device: RoborockDevice | None = state.get("device")
    if not device or not device.v1_properties:
        return
    try:
        await device.v1_properties.rooms.refresh()
        room_map = device.v1_properties.rooms.room_map  # {segment_id: NamedRoomMapping}
        state["rooms"] = {r.name: sid for sid, r in room_map.items()}
        state["room_ids"] = {sid: r.name for sid, r in room_map.items()}
        logger.info(f"房间列表：{state['rooms']}")
    except Exception as e:
        logger.warning(f"获取房间信息失败（{e}）")


# ── 启动 ──────────────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    await _startup()
    yield
    await _shutdown()


def _task_done_callback(task: asyncio.Task) -> None:
    """捕获并打印 background task 的未处理异常（create_task 默认静默吞掉）。"""
    if task.cancelled():
        logger.warning(f"[Task] {task.get_name()} 被取消")
    elif task.exception():
        logger.error(f"[Task] {task.get_name()} 异常退出", exc_info=task.exception())

    if task.get_name() == "audio_effector":
        logger.warning("[Startup] audio_effector 已意外退出")


async def _startup():
    t_effector = asyncio.create_task(audio_effector.start(), name="audio_effector")
    t_effector.add_done_callback(_task_done_callback)
    logger.info("🔊 AudioEffector（TTS 播放队列）已启动")

    # 探测蓝牙环境（bluetoothctl 是否可用）
    await bluetooth_manager.probe()
    if bluetooth_manager.is_simulation:
        logger.warning("⚠️  蓝牙以模拟模式运行（bluetoothctl 不可用，开发机环境）")
    else:
        logger.info("🔵 蓝牙模块已就绪（RPi 5 内置 BT5.0）")
        # 检测实际蓝牙音频输出状态（查询 bluetoothctl + pactl，不依赖内存）
        audio_state = await bluetooth_manager.detect_audio_output()
        if audio_state["bt_connected"]:
            logger.info(
                "🎵 蓝牙设备已连接：%s (%s)",
                audio_state["bt_name"], audio_state["bt_mac"],
            )
        else:
            logger.warning("⚠️  未检测到已连接的蓝牙设备，TTS 将无法通过蓝牙外放")
            logger.warning("   → 请先运行：python3 connect_speaker.py <MAC>")

        if audio_state["sink_is_bt"]:
            logger.info("🔊 音频输出：%s（蓝牙 A2DP）", audio_state["default_sink"])
        elif audio_state["default_sink"]:
            logger.warning(
                "⚠️  默认音频 sink 不是蓝牙设备：%s", audio_state["default_sink"]
            )
            logger.warning("   → TTS 音频将输出到此设备而非蓝牙音箱")
            logger.warning("   → 手动修复：pactl set-default-sink <bluez_sink_name>")
        else:
            logger.warning("⚠️  无法获取 PulseAudio/PipeWire 默认 sink（pactl 是否已安装？）")

    # 必须在进入 to_thread 前捕获事件循环，子线程中无法调用 asyncio.get_event_loop()（Python 3.10+）
    _loop = asyncio.get_running_loop()

    # 提前注入 loop，供电源传感器回调及后续按需启动雷达的 REST 接口使用
    global _main_loop
    _main_loop = _loop

    # 激光雷达懒启动：服务启动时不自动开启雷达，仅在需要时由意图驱动：
    #   - 开始建图：POST /slam/start 内部自动启动
    #   - 开始导航：Node.js MotorEffector 收到 action.navigate 后调用 POST /lidar/start
    #   - 手动控制：POST /lidar/start / POST /lidar/stop
    logger.info("⏸  激光雷达未自动启动，将在建图或导航时按需开启")

    # 启动电源传感器（I2C 轮询线程，失败自动降级模拟模式）
    await asyncio.to_thread(power_sensor.start)
    if power_sensor.is_simulation:
        logger.warning("⚠️  INA219 未连接，电源监测以模拟模式运行")
    else:
        logger.info("🔋 电源传感器已启动（INA219 @ 0x40）")

    # 启动超声波测距（GPIO 轮询线程，开发机自动模拟模式）
    await asyncio.to_thread(ultrasonic.start)
    if ultrasonic.is_simulation:
        logger.warning("⚠️  超声波传感器运行在模拟模式（未检测到 RPi.GPIO）")
    else:
        logger.info(
            "📏 超声波传感器已启动（Trig=GPIO%s Echo=GPIO%s）",
            ultrasonic.status["trig_pin"],
            ultrasonic.status["echo_pin"],
        )

    username: str = ""
    user_data: UserData | None = None
    base_url: str | None = None
    region = os.environ.get("ROBOROCK_REGION", "cn").strip()
    base_url_from_region = REGION_URLS.get(region, REGION_URLS["cn"])

    # ── 认证：token 文件优先，密码次选 ───────────────────────────
    cached = _load_token_file()
    if cached:
        username, base_url, user_data = cached
        base_url = base_url or base_url_from_region
    else:
        env_username = os.environ.get("ROBOROCK_USERNAME", "").strip()
        env_password = os.environ.get("ROBOROCK_PASSWORD", "").strip()
        if env_username and env_password:
            logger.warning(
                "⚠️  正在使用密码登录（密码将以明文发送至 Roborock 服务器）。"
                "建议运行 `python login_once.py` 改用 token 文件方式。"
            )
            try:
                api_tmp = RoborockApiClient(username=env_username, base_url=base_url_from_region)
                user_data = await api_tmp.pass_login(env_password)
                username = env_username
                base_url = base_url_from_region
            except Exception as e:
                logger.error(f"密码登录失败：{e}")
        else:
            logger.warning(
                "未找到 token 文件，也未设置 ROBOROCK_USERNAME/PASSWORD。\n"
                "  → 推荐方式：运行 `python login_once.py` 完成首次登录\n"
                "  → 备用方式：在 .env 中设置 ROBOROCK_USERNAME 和 ROBOROCK_PASSWORD"
            )

    if not user_data or not username:
        logger.warning("Bridge 以无设备模式启动，API 调用将返回 503")
        return

    # ── 连接设备（v4 DeviceManager API）──────────────────────────
    try:
        logger.info("正在初始化 DeviceManager...")
        user_params = UserParams(username=username, user_data=user_data, base_url=base_url)
        device_manager: DeviceManager = await create_device_manager(user_params)
        state["device_manager"] = device_manager

        devices = await device_manager.get_devices()
        if not devices:
            logger.warning("未发现任何设备")
            return

        # 取第一台支持 v1 协议的设备（T7 是 v1）
        device = next((d for d in devices if d.v1_properties), None)
        if device is None:
            logger.warning("未找到支持 V1 协议的设备")
            return

        state["device"] = device
        logger.info(f"✅ 设备已就绪：{device.name}")

        # 刷新房间列表
        await _refresh_rooms()

        state["ready"] = True
        logger.info("🤖 Roborock Bridge 就绪")

    except Exception as e:
        err_str = str(e).lower()
        if any(kw in err_str for kw in ["invalid", "credentials", "2010", "unauthorized", "401"]):
            logger.error(
                f"❌ Token 已失效：{e}\n"
                "   → 解决方法：重新运行 `python login_once.py` 刷新 token 文件"
            )
            if TOKEN_FILE.exists():
                TOKEN_FILE.rename(TOKEN_FILE.with_suffix(".json.expired"))
        else:
            logger.error(f"连接设备失败：{e}")
        logger.warning("Bridge 以无设备模式运行，API 调用将返回 503")


async def _shutdown():
    dm: DeviceManager | None = state.get("device_manager")
    if dm:
        try:
            dm.close()
        except Exception:
            pass
    try:
        chassis.cleanup()
    except Exception:
        pass
    try:
        camera.cleanup()
    except Exception:
        pass
    try:
        cam.cleanup()
    except Exception:
        pass
    try:
        lidar_sensor.stop()
    except Exception:
        pass
    try:
        power_sensor.stop()
    except Exception:
        pass
    try:
        ultrasonic.stop()
    except Exception:
        pass


# ── FastAPI ───────────────────────────────────────────────────────
app = FastAPI(title="Roborock Bridge", version="2.0.0", lifespan=lifespan)

# 静态文件服务：将快照目录挂载到 /snapshots，供前端直接访问
_snapshot_dir = os.environ.get("CAMERA_SNAPSHOT_DIR", "/tmp/roborock_snapshots")
os.makedirs(_snapshot_dir, exist_ok=True)
app.mount("/snapshots", StaticFiles(directory=_snapshot_dir), name="snapshots")


# ── 请求模型 ──────────────────────────────────────────────────────
class CleanRoomsRequest(BaseModel):
    room_names: list[str] = []
    room_ids: list[int] = []
    repeat: int = 1

class ZoneCleanRequest(BaseModel):
    zones: list[list[int]]
    repeat: int = 1

class MotorCommandRequest(BaseModel):
    command: str          # forward / backward / turn_left / turn_right / stop
    speed: int | None = None    # 0–100，None 使用底盘默认速度
    duration: float | None = None  # 持续时间（秒），None 表示持续运动

class SetMotorRequest(BaseModel):
    position: str         # front_left / front_right / rear_left / rear_right
    direction: str        # forward / backward / stop
    speed: int | None = None

class CameraLookAtRequest(BaseModel):
    pan:  float | None = None   # 水平角度 0–180，None=不改变
    tilt: float | None = None   # 垂直角度（硬件限制 75–105），None=不改变

class CameraMoveRequest(BaseModel):
    axis:  str    # pan | tilt
    delta: float  # 相对偏移量（度），正=右/上，负=左/下

class BluetoothConnectRequest(BaseModel):
    mac: str      # 设备 MAC 地址，格式 XX:XX:XX:XX:XX:XX

class BluetoothScanRequest(BaseModel):
    timeout_s: int = 10  # 扫描超时（秒）


# ── 辅助函数 ──────────────────────────────────────────────────────
def require_device() -> RoborockDevice:
    device: RoborockDevice | None = state.get("device")
    if not device or not device.v1_properties:
        raise HTTPException(status_code=503, detail="设备未连接，请检查账号配置和网络")
    return device


async def send_command(cmd: RoborockCommand, params=None) -> dict:
    device = require_device()
    result = await device.v1_properties.command.send(cmd, params)
    return {"ok": True, "result": result}


# ── 健康检查 ──────────────────────────────────────────────────────
@app.get("/health")
async def health():
    return {
        "status": "ok",
        "ready": state["ready"],
        "has_device": state["device"] is not None,
        "rooms": state["rooms"],
        "auth": {
            "token_file": TOKEN_FILE.exists(),
            "token_expired": TOKEN_FILE.with_suffix(".json.expired").exists(),
        },
    }


# ── 音频模块接口 ───────────────────────────────────────────────────

# ── 蓝牙设备管理 ──────────────────────────────────────────────────

@app.get("/bluetooth/status", summary="蓝牙连接状态")
async def bluetooth_status():
    """
    返回当前蓝牙连接状态。

    返回字段：
      - connected:    是否已连接蓝牙设备
      - mac:          已连接设备 MAC 地址（未连接时为 null）
      - name:         已连接设备名称（未连接时为 null）
      - simulation:   是否为模拟模式（开发机 / bluetoothctl 不可用）
    """
    return bluetooth_manager.status()


@app.get("/bluetooth/devices", summary="已配对蓝牙设备列表")
async def bluetooth_devices():
    """返回已配对（曾连接过）的蓝牙设备列表 [{mac, name}]。"""
    devices = await bluetooth_manager.get_paired_devices()
    return {"devices": devices, "count": len(devices)}


@app.post("/bluetooth/scan", summary="扫描附近蓝牙设备")
async def bluetooth_scan(req: BluetoothScanRequest):
    """
    扫描附近蓝牙设备（默认 10 秒）。

    注意：扫描期间会阻塞请求，建议 timeout_s 不超过 15 秒。
    返回格式：[{mac, name}]
    """
    devices = await bluetooth_manager.scan(timeout_s=req.timeout_s)
    return {"devices": devices, "count": len(devices)}


@app.post("/bluetooth/connect", summary="连接蓝牙设备（配对→信任→连接→设为默认 sink）")
async def bluetooth_connect(req: BluetoothConnectRequest):
    """
    连接指定 MAC 的蓝牙设备（A2DP 音频输出模式）。

    流程：pair → trust → connect → pactl set-default-sink
    成功后 TTS 音频将通过该蓝牙设备播放（需同时设置 SPEAKER_BACKEND=pulseaudio）。

    返回字段：
      - ok:      是否连接成功
      - status:  连接后的蓝牙状态快照
    """
    ok = await bluetooth_manager.connect(req.mac)
    return {"ok": ok, "status": bluetooth_manager.status()}


@app.post("/bluetooth/disconnect", summary="断开蓝牙设备")
async def bluetooth_disconnect():
    """断开当前已连接的蓝牙设备。"""
    ok = await bluetooth_manager.disconnect()
    return {"ok": ok, "status": bluetooth_manager.status()}


@app.get("/audio/status", summary="音频输出状态（蓝牙 Chat 模式）")
async def audio_status():
    """
    查询扬声器与蓝牙连接状态（Chat 模式下麦克风已移除，录音由手机端负责）。

    返回字段：
      - speaker.backend:   播放后端（alsa / pulseaudio）
      - speaker.busy:      是否正在 TTS 播放
      - bluetooth:         蓝牙连接状态（见 /bluetooth/status）
    """
    from devices.speaker import SPEAKER_BACKEND, SPEAKER_TTS_ENGINE
    return {
        "speaker": {
            "backend":    SPEAKER_BACKEND,
            "busy":       audio_effector._speaker.is_busy(),
            "tts_engine": SPEAKER_TTS_ENGINE,
        },
        "bluetooth": bluetooth_manager.status(),
    }


@app.post("/audio/verify", summary="扬声器验证（播放测试 TTS）")
async def audio_verify():
    """
    通过扬声器播放一段测试语音，验证 TTS + 蓝牙/ALSA 链路是否正常。

    返回字段：
      - tts_queued:  测试音是否已成功入队播放
      - bluetooth:   当前蓝牙连接状态（若未连接蓝牙音箱声音输出到默认 ALSA 设备）
    """
    tts_queued = False
    try:
        verify_text = "语音输出验证通过，扬声器工作正常。"
        await audio_effector.enqueue(verify_text, interrupt=False)
        tts_queued = True
        logger.info("[audio/verify] 验证音已入队播放")
    except Exception as e:
        logger.error(f"[audio/verify] TTS 入队失败：{e}")

    return {
        "ok":         tts_queued,
        "tts_queued": tts_queued,
        "bluetooth":  bluetooth_manager.status(),
    }


# ── 树莓派机器人自身状态 ────────────────────────────────────────────
@app.get("/robot/status")
async def robot_status():
    """返回树莓派机器人本体状态（电源、各模块在线情况）。"""
    power = power_sensor.latest_reading
    return {
        "power": {
            "voltage_v":   round(power.voltage_v, 2)   if power else None,
            "current_ma":  round(power.current_ma, 1)  if power else None,
            "power_mw":    round(power.power_mw, 1)    if power else None,
            "battery_pct": round(power.battery_pct, 1) if power else None,
            "is_charging": power.is_charging            if power else None,
        },
        "modules": {
            "lidar":       not lidar_sensor.device.is_simulation,
            "chassis":     not chassis.is_simulation,
            "microphone":  True,
            "speaker":     True,
            "ultrasonic":  not ultrasonic.is_simulation,
        },
        "distance": ultrasonic.latest_reading.to_dict() if ultrasonic.latest_reading else None,
    }


# ── 获取机器人状态 ─────────────────────────────────────────────────
@app.get("/status")
async def get_status():
    device = require_device()
    try:
        await device.v1_properties.status.refresh()
        s = device.v1_properties.status
        return {
            "state": getattr(s, "state_name", None) or str(getattr(s, "state", "")),
            "state_code": getattr(s, "state", None),
            "battery": s.battery,
            "fan_power": s.fan_power,
            "clean_time": getattr(s, "clean_time", None),
            "clean_area": getattr(s, "square_meter_clean_area", None),
            "error_code": getattr(s, "error_code", None),
            "in_cleaning": getattr(s, "in_cleaning", None),
            "in_returning": getattr(s, "in_returning", None),
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ── 获取房间列表 ──────────────────────────────────────────────────
@app.get("/rooms")
async def get_rooms():
    require_device()
    await _refresh_rooms()
    return {"rooms": state["rooms"], "room_ids": state["room_ids"]}


# ── 全屋清扫 ──────────────────────────────────────────────────────
@app.post("/clean/start")
async def clean_start():
    try:
        await send_command(RoborockCommand.APP_START)
        return {"ok": True, "action": "全屋清扫已启动"}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ── 指定房间清扫 ──────────────────────────────────────────────────
@app.post("/clean/rooms")
async def clean_rooms(req: CleanRoomsRequest):
    require_device()

    ids = list(req.room_ids)
    if req.room_names:
        for name in req.room_names:
            sid = state["rooms"].get(name)
            if sid is None:
                raise HTTPException(
                    status_code=404,
                    detail=f"未找到房间：{name}，可用：{list(state['rooms'].keys())}"
                )
            ids.append(sid)

    if not ids:
        raise HTTPException(status_code=400, detail="请指定 room_names 或 room_ids")

    try:
        await send_command(
            RoborockCommand.APP_SEGMENT_CLEAN,
            [{"segments": ids, "repeat": req.repeat}],
        )
        names = [state["room_ids"].get(i, str(i)) for i in ids]
        return {"ok": True, "action": f"开始清扫：{names}，遍数：{req.repeat}"}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ── 区域清扫 ──────────────────────────────────────────────────────
@app.post("/clean/zone")
async def clean_zone(req: ZoneCleanRequest):
    try:
        await send_command(
            RoborockCommand.APP_ZONED_CLEAN,
            [{"zones": req.zones, "repeat": req.repeat}],
        )
        return {"ok": True, "action": f"区域清扫已启动，区域数：{len(req.zones)}"}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ── 暂停 ──────────────────────────────────────────────────────────
@app.post("/clean/pause")
async def clean_pause():
    try:
        await send_command(RoborockCommand.APP_PAUSE)
        return {"ok": True, "action": "已暂停"}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ── 继续 ──────────────────────────────────────────────────────────
@app.post("/clean/resume")
async def clean_resume():
    try:
        await send_command(RoborockCommand.APP_START)
        return {"ok": True, "action": "已继续"}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ── 停止并回充 ────────────────────────────────────────────────────
@app.post("/home")
async def return_home():
    try:
        await send_command(RoborockCommand.APP_CHARGE)
        return {"ok": True, "action": "正在回充"}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ── 停止清扫 ──────────────────────────────────────────────────────
@app.post("/clean/stop")
async def clean_stop():
    try:
        await send_command(RoborockCommand.APP_STOP)
        return {"ok": True, "action": "已停止"}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ── 清洁历史 ──────────────────────────────────────────────────────
@app.get("/history")
async def get_history(limit: int = 10):
    device = require_device()
    try:
        await device.v1_properties.clean_summary.refresh()
        data = device.v1_properties.clean_summary.as_dict()
        return {"records": data}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ── 底盘运动接口 ───────────────────────────────────────────────────
@app.post("/motor/command")
async def motor_command(req: MotorCommandRequest):
    """
    执行底盘运动指令。

    command 取值：forward / backward / turn_left / turn_right / stop
    speed 范围：0–100（整数），省略时使用底盘默认速度（CHASSIS_DEFAULT_SPEED，默认 35）
    duration：持续秒数，省略或为 null 时持续运动，直到发送 stop 指令
    """
    from devices.chassis import VALID_COMMANDS
    if req.command not in VALID_COMMANDS:
        raise HTTPException(
            status_code=400,
            detail=f"未知指令 {req.command!r}，有效值：{sorted(VALID_COMMANDS)}",
        )
    try:
        await chassis.execute_timed(req.command, req.speed, req.duration)
        return {
            "ok": True,
            "command": req.command,
            "speed": req.speed,
            "duration": req.duration,
        }
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/motor/set")
async def motor_set(req: SetMotorRequest):
    """精细控制单个电机，用于调试或特殊动作。"""
    try:
        chassis.set_motor(req.position, req.direction, req.speed)
        return {"ok": True, "position": req.position, "direction": req.direction}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/motor/status")
async def motor_status():
    """查询各电机当前速度（正=正转，负=反转，0=停止）。"""
    return {
        "simulation": chassis.is_simulation,
        "motors": chassis.status,
    }


# ── 摄像头云台接口 ─────────────────────────────────────────────────
@app.post("/camera/look_at")
async def camera_look_at(req: CameraLookAtRequest):
    """
    设置云台朝向。pan/tilt 均可省略（省略=保持当前值）。

    Pan  范围：0–180°（0=最左，90=正前，180=最右）
    Tilt 范围：75–105°（硬件限制，超出自动钳位）
    """
    result: dict = {}
    if req.pan is not None:
        result["pan"] = camera.pan_to(req.pan)
    if req.tilt is not None:
        result["tilt"] = camera.tilt_to(req.tilt)
    if not result:
        raise HTTPException(status_code=400, detail="请至少指定 pan 或 tilt")
    return {"ok": True, **result}


@app.post("/camera/move")
async def camera_move(req: CameraMoveRequest):
    """相对偏移云台（axis=pan|tilt，delta 为度数，正=右/上，负=左/下）。"""
    if req.axis == "pan":
        actual = camera.pan_by(req.delta)
    elif req.axis == "tilt":
        actual = camera.tilt_by(req.delta)
    else:
        raise HTTPException(status_code=400, detail="axis 须为 pan 或 tilt")
    return {"ok": True, "axis": req.axis, "angle": actual}


@app.post("/camera/center")
async def camera_center():
    """云台双轴归中（正视前方）。"""
    camera.center()
    return {"ok": True, "status": camera.status}


@app.get("/camera/status")
async def camera_status():
    """查询云台当前角度和硬件限制范围。"""
    return {
        "status": camera.status,
        "limits": camera.limits,
    }


# ── 摄像头拍照接口 ─────────────────────────────────────────────────

from fastapi.responses import Response as _FastAPIResponse  # noqa: E402


@app.get("/camera/capture", summary="拍照（返回 JPEG 图像）")
async def camera_capture():
    """
    使用机器车摄像头拍一张照片，直接返回 JPEG 二进制流。

    - Content-Type: image/jpeg
    - 失败时返回 503
    """
    data = await asyncio.to_thread(cam.capture)
    if data is None:
        raise HTTPException(503, "摄像头不可用或拍照失败，请检查摄像头连接")
    return _FastAPIResponse(content=data, media_type="image/jpeg")


@app.get("/camera/capture/base64", summary="拍照（返回 base64 JSON）")
async def camera_capture_base64():
    """
    使用机器车摄像头拍照，返回 base64 编码的 JPEG（适合嵌入 JSON 传给 AI 视觉接口）。

    返回格式：
      {"data": "<base64>", "timestamp": <ms>}
    """
    b64 = await asyncio.to_thread(cam.capture_base64)
    if b64 is None:
        raise HTTPException(503, "摄像头不可用或拍照失败，请检查摄像头连接")
    return {"data": b64, "timestamp": int(asyncio.get_running_loop().time() * 1000)}


@app.get("/camera/capture/save", summary="拍照并保存到文件")
async def camera_capture_save():
    """
    使用机器车摄像头拍照，保存到 snapshot_dir（由环境变量 CAMERA_SNAPSHOT_DIR 配置）。

    返回格式：
      {"path": "<绝对路径>"}
    """
    path = await asyncio.to_thread(cam.capture_to_file)
    if path is None:
        raise HTTPException(503, "摄像头不可用或保存失败")
    return {"path": path}


@app.get("/camera/capture/status", summary="查询摄像头采集状态")
async def camera_capture_status():
    return cam.status


# ── 激光雷达接口 ───────────────────────────────────────────────────

@app.post("/lidar/start", summary="启动激光雷达扫描")
async def lidar_start():
    """
    手动启动激光雷达串口读取线程。

    - 若已在运行，直接返回当前状态（幂等）
    - 若串口不可用（未接硬件），自动进入模拟模式并返回 is_simulation=true
    - 建议在开始建图或室内导航前调用；也可设置环境变量 LIDAR_AUTO_START=true 让其随服务自动启动
    """
    if lidar_sensor.device.is_running:
        return {"ok": True, "message": "激光雷达已在运行中", "status": lidar_sensor.device.status}
    await asyncio.to_thread(lidar_sensor.start, _main_loop)
    return {"ok": True, "status": lidar_sensor.device.status}


@app.post("/lidar/stop", summary="停止激光雷达扫描")
async def lidar_stop_endpoint():
    """
    停止激光雷达串口读取线程并关闭串口。

    - 停止后不再广播 sense.lidar.scan，SLAM 建图也将停止接收新帧
    - 可通过 POST /lidar/start 重新启动
    """
    lidar_sensor.stop()
    return {"ok": True, "message": "激光雷达已停止", "status": lidar_sensor.device.status}


@app.get("/lidar/status", summary="激光雷达连接状态")
async def lidar_status():
    """
    查询激光雷达（LD06）当前状态。

    返回字段：
      - port: 串口设备路径
      - is_simulation: 是否为模拟模式（True = 未连接）
      - is_running: 读取线程是否运行中
      - completed_scans: 已完成圈数
      - latest_scan: 最近一圈摘要（timestamp_ms, rpm, point_count, valid_count）
    """
    return lidar_sensor.device.status


@app.get("/lidar/scan", summary="获取最新一圈扫描数据")
async def lidar_scan():
    """
    返回最近一圈完整扫描数据（全部测距点）。

    每圈约 450 个点（@ 10Hz 扫描频率 × 4500Hz 测量频率）。
    若激光雷达未连接或尚未完成第一圈，返回 503。
    """
    scan = lidar_sensor.device.latest_scan
    if scan is None:
        raise HTTPException(
            status_code=503,
            detail="激光雷达尚未完成第一圈扫描，请检查连接后稍候重试"
        )
    return scan.to_dict()


@app.get("/lidar/scan/valid", summary="获取最新一圈有效测距点")
async def lidar_scan_valid():
    """
    仅返回置信度 > 10 且距离在 20mm–12000mm 范围内的有效测距点，
    剔除无效反射点（玻璃、过近过远目标等），适合直接用于地图构建。
    """
    scan = lidar_sensor.device.latest_scan
    if scan is None:
        if EXPLORER_DEBUG_LOG:
            logger.info("[ExplorerDebug] GET /lidar/scan/valid -> 503 激光雷达未就绪")
        raise HTTPException(status_code=503, detail="激光雷达未就绪")
    valid = scan.valid_points
    out = {
        "timestamp_ms": scan.timestamp_ms,
        "rpm": round(scan.rpm, 1),
        "valid_count": len(valid),
        "points": [
            {"angle": round(p.angle, 2), "distance": p.distance, "confidence": p.confidence}
            for p in valid
        ],
    }
    if EXPLORER_DEBUG_LOG:
        logger.info(
            "[ExplorerDebug] GET /lidar/scan/valid -> ok valid_count=%d ts=%d rpm=%.1f",
            len(valid),
            scan.timestamp_ms,
            scan.rpm,
        )
    return out


# ── SLAM 建图接口 ──────────────────────────────────────────────────

@app.post("/slam/start", summary="开始建图")
async def slam_start():
    """
    初始化 SLAM 引擎并开始接受激光雷达扫描帧建图。

    - 若激光雷达未启动，自动尝试启动（无需提前调用 POST /lidar/start）
    - 若已在建图中，重置后重新开始
    - 需要 breezyslam 已安装（pip install breezyslam）
    - LiDAR 必须已连接（非模拟模式）

    建图过程中，WebSocket 会持续广播：
      sense.slam.pose       — 机器人位姿（~1Hz）
      sense.slam.map_update — 地图 PNG（~0.2Hz）
    """
    if not slam_engine.is_available:
        raise HTTPException(status_code=503, detail="breezyslam 未安装，请运行：pip install breezyslam")
    # 若雷达未启动，自动启动（懒启动模式下建图前无需手动调用 /lidar/start）
    if not lidar_sensor.device.is_running:
        await asyncio.to_thread(lidar_sensor.start, _main_loop)
    if lidar_sensor.device.is_simulation:
        raise HTTPException(status_code=503, detail="激光雷达未连接，无法建图")
    ok = slam_engine.start_mapping()
    if not ok:
        raise HTTPException(status_code=500, detail="SLAM 引擎启动失败")
    return {"ok": True, "status": slam_engine.status}


@app.post("/slam/stop", summary="停止建图（冻结地图）并关闭激光雷达")
async def slam_stop():
    """
    停止接受新扫描帧，地图冻结在当前状态，同时关闭激光雷达串口。
    机器人位姿仍可查询，地图可继续保存。
    若需重新建图，调用 POST /slam/start 会自动重启雷达。
    """
    slam_engine.stop_mapping()
    lidar_sensor.stop()
    return {"ok": True, "status": slam_engine.status}


@app.post("/slam/reset", summary="重置 SLAM（清空地图和位姿）")
async def slam_reset():
    """彻底清空地图和位姿，回到初始状态。"""
    slam_engine.reset()
    return {"ok": True, "message": "SLAM 已重置"}


@app.get("/slam/status", summary="SLAM 引擎状态")
async def slam_status():
    """
    查询 SLAM 引擎当前状态。

    返回字段：
      - available: breezyslam 是否已安装
      - is_mapping: 是否正在建图
      - scan_count: 已处理圈数
      - elapsed_s: 建图持续时间（秒）
      - pose: 当前机器人位姿 {x_mm, y_mm, theta_deg}
      - map_size_pixels, map_size_meters, mm_per_pixel: 地图参数
    """
    return slam_engine.status


@app.get("/slam/pose", summary="当前机器人位姿")
async def slam_pose():
    """
    返回 SLAM 估算的机器人当前位姿。

    - x_mm, y_mm: 相对建图起点的坐标（毫米）
    - theta_deg: 朝向角度（度，逆时针为正）
    """
    if not slam_engine.is_mapping and slam_engine.scan_count == 0:
        if EXPLORER_DEBUG_LOG:
            logger.info("[ExplorerDebug] GET /slam/pose -> 503 SLAM 未启动")
        raise HTTPException(status_code=503, detail="SLAM 未启动，请先调用 /slam/start")
    x, y, theta = slam_engine.get_pose()
    out = {"x_mm": round(x, 1), "y_mm": round(y, 1), "theta_deg": round(theta, 2)}
    if EXPLORER_DEBUG_LOG:
        logger.info(
            "[ExplorerDebug] GET /slam/pose -> ok x_mm=%.1f y_mm=%.1f theta_deg=%.2f",
            out["x_mm"],
            out["y_mm"],
            out["theta_deg"],
        )
    return out


@app.get("/slam/map", summary="获取当前地图（PNG base64）")
async def slam_map():
    """
    返回当前地图的 PNG 图像（base64 编码）及相关元数据。

    颜色含义：
      灰色  = 未探索区域
      黑色  = 障碍物（墙壁、家具）
      白色  = 可通行区域
      蓝色点 = 机器人当前位置

    返回格式：
      {
        "image_b64": "<base64 PNG>",
        "width": 500, "height": 500,
        "mm_per_pixel": 20.0,
        "robot_pixel": {"x": 250, "y": 250},
        "pose": {"x_mm": 0, "y_mm": 0, "theta_deg": 0}
      }
    """
    if slam_engine.scan_count == 0:
        raise HTTPException(status_code=503, detail="地图尚未生成，请先启动建图")
    png_b64 = await asyncio.to_thread(slam_engine.get_map_png_b64)
    if png_b64 is None:
        raise HTTPException(status_code=503, detail="地图渲染失败（opencv 未安装？）")
    pose = slam_engine.get_pose()
    rx, ry = slam_engine.pose_to_pixel(pose[0], pose[1])
    return {
        "image_b64":    png_b64,
        "width":        slam_engine._cfg.map_size_pixels,
        "height":       slam_engine._cfg.map_size_pixels,
        "mm_per_pixel": round(slam_engine._cfg.mm_per_pixel, 1),
        "robot_pixel":  {"x": rx, "y": ry},
        "pose":         {"x_mm": round(pose[0], 1), "y_mm": round(pose[1], 1), "theta_deg": round(pose[2], 2)},
    }


@app.post("/slam/save", summary="保存当前地图到磁盘")
async def slam_save(name: str = ""):
    """
    将当前地图保存到 platform/maps/ 目录。

    保存文件：
      {name}.pgm  — 灰度地图图像（ROS 兼容格式）
      {name}.json — 元数据（分辨率、位姿、扫描次数等）

    name 可选，默认自动生成时间戳文件名（map_YYYYMMDD_HHMMSS）。
    """
    if slam_engine.scan_count == 0:
        raise HTTPException(status_code=503, detail="地图为空，请先建图")
    result = await asyncio.to_thread(slam_engine.save_map, name)
    if result is None:
        raise HTTPException(status_code=500, detail="地图保存失败（地图为空）")
    return {"ok": True, **result}


@app.get("/slam/maps", summary="列出所有已保存的地图")
async def slam_list_maps():
    """
    列出 platform/maps/ 目录下所有已保存地图的元数据。

    每条记录包含：name, created_at, map_size_pixels, scan_count, final_pose 等。
    """
    maps = await asyncio.to_thread(slam_engine.list_maps)
    return {"maps": maps, "count": len(maps)}


@app.post("/slam/load/{map_name}", summary="加载已保存的地图")
async def slam_load_map(map_name: str):
    """
    从 platform/maps/ 加载指定名称的地图（恢复 map_bytes，不恢复位姿）。

    加载后可用于定位（后续 AMCL 导航），或继续在此基础上建图。
    """
    ok = await asyncio.to_thread(slam_engine.load_map, map_name)
    if not ok:
        raise HTTPException(status_code=404, detail=f"地图 '{map_name}' 不存在或格式不匹配")
    return {"ok": True, "map_name": map_name}


# ── 电源状态接口 ───────────────────────────────────────────────────

@app.get("/power/status", summary="电源传感器实时状态")
async def power_status():
    """
    查询 INA219 电源传感器当前状态。

    返回字段：
      - is_simulation: 是否为模拟模式（True = INA219 未连接）
      - is_running:    后台轮询线程是否存活
      - latest:        最近一次采样数据，包含：
          - voltage_v:    总线电压（V）
          - current_ma:   电流（mA，正=放电，负=充电）
          - power_mw:     功率（mW）
          - battery_pct:  剩余电量（%，由电压线性估算）
          - is_charging:  是否正在充电
      - is_low_battery: 当前是否处于低电量状态（< 20%）
    """
    return power_sensor.status


# ── 超声波测距接口 ───────────────────────────────────────────────────

@app.get("/ultrasonic/status", summary="超声波传感器状态")
async def ultrasonic_status():
    """
    查询 HC-SR04 传感器当前状态。

    返回字段：
      - is_simulation: 是否为模拟模式（True = 非树莓派环境）
      - is_running:    后台轮询线程是否存活
      - trig_pin/echo_pin: GPIO 引脚配置（BCM）
      - latest:        最近一次测距读数（distance_cm / is_too_close）
    """
    status = ultrasonic.status
    if EXPLORER_DEBUG_LOG and status.get("latest"):
        latest = status["latest"]
        age_ms = int(time.time() * 1000) - latest.get("timestamp_ms", 0)
        logger.info(
            "[ExplorerDebug] GET /ultrasonic/status -> ok distance_cm=%.1f too_close=%s age_ms=%d",
            latest.get("distance_cm", 0),
            latest.get("is_too_close", False),
            age_ms,
        )
    elif EXPLORER_DEBUG_LOG and not status.get("latest"):
        logger.info("[ExplorerDebug] GET /ultrasonic/status -> ok latest=null")
    return status


@app.get("/ultrasonic/read", summary="执行一次超声波测距")
async def ultrasonic_read():
    """
    同步触发一次测距并返回结果。
    若读数超时或超量程，返回 503。
    """
    reading = await asyncio.to_thread(ultrasonic.read_once)
    if reading is None:
        raise HTTPException(
            status_code=503,
            detail="超声波测距失败（回波超时或目标超量程），请检查 Trig/Echo 接线与供电",
        )
    return reading.to_dict()


# ── 互联网搜索接口 ────────────────────────────────────────────────────

class FetchPageRequest(BaseModel):
    url: str
    max_chars: int = 3000


_SEARXNG_URL = os.environ.get("SEARXNG_URL", "http://localhost:8080").rstrip("/")


@app.get("/search", summary="互联网搜索（SearXNG，聚合 Bing/Google/DDG/百度）")
async def search_web(q: str, max_results: int = 5):
    """
    通过本地 SearXNG 服务搜索互联网，聚合多个搜索引擎结果，返回标题、URL 和摘要列表。

    - q:           搜索关键词（支持中英文）
    - max_results: 返回结果数（默认 5，最大 10）

    依赖：SearXNG Docker 容器在 SEARXNG_URL（默认 http://localhost:8080）上运行。
    启动命令：docker compose up -d

    返回格式：
      {
        "query": "...",
        "results": [
          {"title": "...", "url": "...", "snippet": "..."},
          ...
        ]
      }
    """
    import urllib.request

    max_results = min(max_results, 10)
    search_url = (
        f"{_SEARXNG_URL}/search"
        f"?q={urllib.parse.quote(q)}"
        f"&format=json"
        f"&language=zh-CN"
    )

    def _do_search():
        req = urllib.request.Request(
            search_url,
            headers={"User-Agent": "goudan-agent/1.0"},
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode())

    try:
        data = await asyncio.to_thread(_do_search)
    except Exception as e:
        raise HTTPException(
            status_code=502,
            detail=f"SearXNG 搜索失败（{_SEARXNG_URL}）：{e}。请确认已运行 docker compose up -d"
        )

    raw = data.get("results", [])[:max_results]
    results = [
        {
            "title":   r.get("title", ""),
            "url":     r.get("url", ""),
            "snippet": r.get("content", ""),
        }
        for r in raw
    ]
    return {"query": q, "results": results}


@app.post("/fetch", summary="抓取网页正文（trafilatura 提取纯文本）")
async def fetch_page(req: FetchPageRequest):
    """
    抓取指定 URL 的网页内容，提取正文纯文本（去除广告、导航、脚本等噪音）。

    - url:       目标网页 URL
    - max_chars: 返回正文最大字符数（默认 3000，防止 token 爆炸）

    返回格式：
      {
        "url": "...",
        "content": "...",   # 提取的正文，可能为 null（提取失败时）
        "ok": true
      }
    """
    try:
        import trafilatura
    except ImportError:
        raise HTTPException(status_code=503, detail="trafilatura 未安装，请运行：pip install trafilatura")

    def _do_fetch():
        html = trafilatura.fetch_url(req.url)
        if html is None:
            return None
        return trafilatura.extract(html, include_comments=False, include_tables=True)

    try:
        content = await asyncio.to_thread(_do_fetch)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"抓取失败：{e}")

    if content and len(content) > req.max_chars:
        content = content[: req.max_chars] + "…（已截断）"

    return {"url": req.url, "content": content, "ok": content is not None}


# ── WebSocket — Spine 双向通道 ─────────────────────────────────────
@app.websocket("/ws")
async def websocket_spine(ws: WebSocket):
    """
    Node.js BridgeConnector 连入此端点。
    - Bridge → Node.js：感官事件（sense.*）广播
    - Node.js → Bridge：行动指令（action.*）下发
    """
    await ws_manager.connect(ws)
    try:
        while True:
            data = await ws.receive_json()
            await _handle_action(data)
    except WebSocketDisconnect:
        ws_manager.disconnect(ws)
    except Exception as e:
        logger.warning("[WS] 连接异常退出：%s", e)
        ws_manager.disconnect(ws)
        try:
            await ws.close()
        except Exception:
            pass


async def _handle_action(message: dict) -> None:
    """处理来自 Node.js 的行动指令。"""
    msg_type = message.get("type", "")
    payload = message.get("payload", {})

    if msg_type == "action.speak":
        text = payload.get("text", "")
        interrupt = payload.get("interrupt_current", False)
        if text:
            ts = datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
            prev = (text[:48] + "…") if len(text) > 48 else text
            logger.info(
                "[WS] action.speak recv ts=%s chars=%d interrupt=%s preview=%s",
                ts,
                len(text),
                interrupt,
                prev,
            )
            await audio_effector.enqueue(text, interrupt=interrupt)

    elif msg_type == "action.motor":
        command = payload.get("command", "stop")
        speed = payload.get("speed")       # int | None
        duration = payload.get("duration") # float | None
        try:
            await chassis.execute_timed(command, speed, duration)
            logger.info("[WS] 底盘指令：%s speed=%s duration=%s", command, speed, duration)
        except ValueError as e:
            logger.warning("[WS] 底盘指令错误：%s", e)

    elif msg_type == "action.camera":
        # payload 示例：
        #   look_at:  {"command":"look_at","pan":90,"tilt":90}
        #   move:     {"command":"move","axis":"pan","delta":-10}
        #   center:   {"command":"center"}
        #   snapshot: {"command":"snapshot"}
        command = payload.get("command", "look_at")
        try:
            if command == "look_at":
                pan  = payload.get("pan")
                tilt = payload.get("tilt")
                if pan  is not None: camera.pan_to(float(pan))
                if tilt is not None: camera.tilt_to(float(tilt))
                logger.info("[WS] 云台 look_at pan=%s tilt=%s", pan, tilt)
            elif command == "move":
                axis  = payload.get("axis", "pan")
                delta = float(payload.get("delta", 0))
                if axis == "pan":
                    camera.pan_by(delta)
                else:
                    camera.tilt_by(delta)
                logger.info("[WS] 云台 move axis=%s delta=%s", axis, delta)
            elif command == "center":
                camera.center()
                logger.info("[WS] 云台归中")
            elif command == "snapshot":
                # 拍照后通过 WebSocket 广播给所有连接的客户端（含 Node.js Agent）
                b64 = await asyncio.to_thread(cam.capture_base64)
                import time as _time
                await ws_manager.broadcast({
                    "type": "sense.camera.snapshot",
                    "payload": {
                        "data":      b64,
                        "available": b64 is not None,
                        "timestamp": int(_time.time() * 1000),
                    },
                })
                logger.info("[WS] 摄像头拍照：%s", "成功" if b64 else "失败（摄像头不可用）")
            else:
                logger.warning("[WS] 未知云台指令：%s", command)
        except Exception as e:
            logger.warning("[WS] 云台指令错误：%s", e)

    else:
        logger.warning(f"[WS] 未知指令类型：{msg_type}")
