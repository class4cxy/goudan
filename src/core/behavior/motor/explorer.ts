/**
 * Explorer — 自主建图探索引擎
 * ==============================
 * 职责：
 *   - 定期轮询 Platform /lidar/scan/valid 获取实时障碍信息
 *   - 根据前方扫描数据决策：前进 / 转向 / 后退
 *   - 通过 PlatformConnector 发送底层电机指令
 *
 * 算法（反应式探索）：
 *   1. 检查前方扇区（±30°）最近障碍距离
 *   2. 距离 > CLEAR_MM：继续前进
 *   3. 距离 < STOP_MM：立即停止，后退一小段
 *   4. 距离在两者之间：停止，比较左右扇区空旷程度，转向更开阔一侧
 */

import { PlatformConnector } from '../../runtime/platform-connector'

const PLATFORM_URL = process.env.ROBOROCK_BRIDGE_URL ?? 'http://localhost:8001'

// ── 配置参数 ─────────────────────────────────────────────────────
const POLL_INTERVAL_MS   = 400   // 障碍检测频率
const CLEAR_MM           = 800   // 前方此距离内无障碍 → 继续前进
const STOP_MM            = 350   // 前方此距离内有障碍 → 紧急停止+后退
const FORWARD_SPEED      = 35    // 前进速度（保守，方便转向）
const TURN_SPEED         = 40    // 转向速度
const REVERSE_DURATION   = 0.6  // 后退时间（秒）
const TURN_DURATION      = 0.9  // 单次转向时间（秒）

// 扇区角度范围（LD06 0°=正前，顺时针增加）
const FRONT_HALF_DEG     = 30   // 前方扇区 ±30°
const SIDE_START_DEG     = 40   // 侧方扇区起始角
const SIDE_END_DEG       = 120  // 侧方扇区结束角

interface LidarPoint {
  angle: number      // 度
  distance: number   // mm
  confidence: number
}

interface LidarScanResponse {
  valid_count: number
  points: LidarPoint[]
}

// ── Explorer 单例 ─────────────────────────────────────────────────

class ExplorerClass {
  private _running  = false
  private _timer: ReturnType<typeof setInterval> | null = null
  private _turning  = false   // 正在转向中，跳过本轮决策

  get isRunning() { return this._running }

  async start(): Promise<void> {
    if (this._running) return
    this._running = true
    this._turning = false

    // 启动 SLAM
    try {
      await fetch(`${PLATFORM_URL}/slam/start`, { method: 'POST', signal: AbortSignal.timeout(5000) })
      console.log('[Explorer] SLAM 已启动')
    } catch (e) {
      console.warn('[Explorer] SLAM 启动失败（继续探索）：', e)
    }

    // 立即前进，然后开始轮询
    this._motor('forward', FORWARD_SPEED)
    this._timer = setInterval(() => this._tick(), POLL_INTERVAL_MS)
    console.log('[Explorer] 自主探索已启动')
  }

  async stop(): Promise<void> {
    if (!this._running) return
    this._running = false

    if (this._timer) {
      clearInterval(this._timer)
      this._timer = null
    }

    this._motor('stop')

    // 停止 SLAM
    try {
      await fetch(`${PLATFORM_URL}/slam/stop`, { method: 'POST', signal: AbortSignal.timeout(5000) })
      console.log('[Explorer] SLAM 已停止')
    } catch (e) {
      console.warn('[Explorer] SLAM 停止失败：', e)
    }

    console.log('[Explorer] 自主探索已停止')
  }

  // ── 内部方法 ────────────────────────────────────────────────────

  private async _tick(): Promise<void> {
    if (!this._running || this._turning) return

    let points: LidarPoint[]
    try {
      const res = await fetch(`${PLATFORM_URL}/lidar/scan/valid`, {
        signal: AbortSignal.timeout(500),
      })
      if (!res.ok) return
      const data = (await res.json()) as LidarScanResponse
      points = data.points
    } catch {
      return  // 网络抖动，跳过本轮
    }

    if (points.length === 0) return

    const frontMin  = this._minDist(points, -FRONT_HALF_DEG, FRONT_HALF_DEG)
    const leftMin   = this._minDist(points, -(SIDE_END_DEG), -(SIDE_START_DEG))
    const rightMin  = this._minDist(points, SIDE_START_DEG,  SIDE_END_DEG)

    if (frontMin > CLEAR_MM) {
      // 前方开阔，继续前进
      this._motor('forward', FORWARD_SPEED)
    } else if (frontMin < STOP_MM) {
      // 太近，后退后转向
      await this._avoid('backward')
    } else {
      // 中等距离，转向空旷一侧
      const dir = leftMin >= rightMin ? 'turn_left' : 'turn_right'
      await this._avoid(dir)
    }
  }

  private async _avoid(action: 'backward' | 'turn_left' | 'turn_right'): Promise<void> {
    this._turning = true
    this._motor('stop')
    await this._sleep(200)

    if (action === 'backward') {
      this._motor('backward', FORWARD_SPEED, REVERSE_DURATION)
      await this._sleep(REVERSE_DURATION * 1000 + 100)
      // 后退完毕，随机转向
      const turn = Math.random() > 0.5 ? 'turn_left' : 'turn_right'
      this._motor(turn, TURN_SPEED, TURN_DURATION)
      await this._sleep(TURN_DURATION * 1000 + 100)
    } else {
      this._motor(action, TURN_SPEED, TURN_DURATION)
      await this._sleep(TURN_DURATION * 1000 + 100)
    }

    if (this._running) {
      this._motor('forward', FORWARD_SPEED)
    }
    this._turning = false
  }

  /**
   * 计算指定角度扇区内的最近障碍距离。
   * LD06：0°=正前，顺时针增加，360° 回到正前。
   * 负角度 = 逆时针（左侧）。
   */
  private _minDist(points: LidarPoint[], fromDeg: number, toDeg: number): number {
    const normalize = (a: number) => ((a % 360) + 360) % 360
    const lo = normalize(fromDeg)
    const hi = normalize(toDeg)

    const inSector = (a: number) => {
      const na = normalize(a)
      return lo <= hi ? na >= lo && na <= hi : na >= lo || na <= hi
    }

    let min = Infinity
    for (const p of points) {
      if (inSector(p.angle)) min = Math.min(min, p.distance)
    }
    return min === Infinity ? 99999 : min
  }

  private _motor(
    command: 'forward' | 'backward' | 'turn_left' | 'turn_right' | 'stop',
    speed?: number,
    duration?: number,
  ): void {
    PlatformConnector.send({
      type: 'action.motor',
      payload: { command, speed, duration },
    })
  }

  private _sleep(ms: number): Promise<void> {
    return new Promise((r) => setTimeout(r, ms))
  }
}

export const Explorer = new ExplorerClass()
