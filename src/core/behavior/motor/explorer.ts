/**
 * Explorer — 自主建图探索引擎
 * ==============================
 * 算法（两档决策 + 果断转向）：
 *
 *   扇区定义：
 *     front      ±30°         正前（决定是否前进的主判断）
 *     frontLeft  -50° ~ -30°  前左对角（仅用于紧急检测）
 *     frontRight  30° ~  50°  前右对角（仅用于紧急检测）
 *     sideLeft  -140° ~ -60°  左侧（选择转向方向）
 *     sideRight   60° ~ 140°  右侧（选择转向方向）
 *
 *   决策（两档，无中间 nudge）：
 *     frontAll < STOP_MM   → 紧急后退 + 果断转向
 *     front    < CLEAR_MM  → 果断转向（朝更空旷一侧）
 *     否则                 → 脉冲前进
 *
 *   为什么去掉 nudge（轻推）：
 *     WARN_MM=700mm 的 nudge 会让机器人在两侧墙壁之间反复微调，
 *     形成左右振荡，永远走不开。新版只区分"能走"和"不能走"，
 *     遇到障碍就果断旋转半步，不做微调。
 */

import { PlatformConnector } from '../../runtime/platform-connector'

const PLATFORM_URL = process.env.ROBOROCK_BRIDGE_URL ?? 'http://localhost:8001'

// ── 距离阈值 ─────────────────────────────────────────────────────
/**
 * 正前扇区（±30°）前进阈值（mm）：超过此值就脉冲前进。
 * 600mm：比旧版 1000mm 更宽松，普通走廊里也能正常行驶。
 */
const CLEAR_MM           = 600
/**
 * 紧急阈值（mm）：任意前方扇区（含对角）低于此值才触发紧急。
 * 对角扇区障碍物只在 <350mm 时才紧急，不再触发轻推。
 */
const STOP_MM            = 350
/**
 * 转向完成阈值（mm）：_turnUntilClear 中正前 ±30° 超过此值即退出。
 * 只检查正前，不要求对角也清晰，避免在普通房间找不到"全清"方向。
 */
const TURN_EXIT_MM       = 500

// ── 运动参数 ─────────────────────────────────────────────────────
const FORWARD_SPEED      = 30
const TURN_SPEED         = 35
/** 单次脉冲前进时长（秒） */
const BURST_DURATION     = 0.4
/** 后退时长（秒） */
const REVERSE_DURATION   = 0.6
/** 单次转向时长（秒）：果断旋转，不 nudge */
const TURN_STEP_DURATION = 0.5
/** 最大连续转向次数（超过后换方向+后退脱困） */
const MAX_TURN_STEPS     = 8

// ── 扇区角度 ─────────────────────────────────────────────────────
/** 对角扇区半角（°）：捕获斜向障碍（如柜子腿），仅用于紧急检测 */
const FRONT_HALF_DEG     = 50
/** 侧方扇区范围（°）：评估左/右整体空旷度，用于选择转向方向 */
const SIDE_START_DEG     = 60
const SIDE_END_DEG       = 140

interface LidarPoint {
  angle: number
  distance: number
  confidence: number
}

// ── Explorer 单例 ─────────────────────────────────────────────────

class ExplorerClass {
  private _running  = false

  get isRunning() { return this._running }

  async start(): Promise<void> {
    if (this._running) return
    this._running = true

    try {
      await fetch(`${PLATFORM_URL}/slam/start`, { method: 'POST', signal: AbortSignal.timeout(5000) })
      console.log('[Explorer] SLAM 已启动')
    } catch (e) {
      console.warn('[Explorer] SLAM 启动失败（继续探索）：', e)
    }

    const connected = await this._waitForConnection(8000)
    if (!connected) {
      console.error('[Explorer] WebSocket 未连接，探索取消')
      this._running = false
      return
    }

    console.log('[Explorer] 自主探索已启动')
    this._runLoop().catch((e) => console.error('[Explorer] 循环异常：', e))
  }

  async stop(): Promise<void> {
    if (!this._running) return
    this._running = false
    this._motor('stop')

    try {
      await fetch(`${PLATFORM_URL}/slam/stop`, { method: 'POST', signal: AbortSignal.timeout(5000) })
      console.log('[Explorer] SLAM 已停止')
    } catch (e) {
      console.warn('[Explorer] SLAM 停止失败：', e)
    }

    console.log('[Explorer] 自主探索已停止')
  }

  // ── 主控制循环 ──────────────────────────────────────────────────

  private async _runLoop(): Promise<void> {
    while (this._running) {
      if (!PlatformConnector.isConnected) {
        this._motor('stop')
        await this._sleep(1000)
        continue
      }

      const points = await this._fetchLidar()
      if (!points) {
        await this._sleep(200)
        continue
      }

      // 四扇区
      const front      = this._minDist(points, -30,             30)
      const frontLeft  = this._minDist(points, -FRONT_HALF_DEG, -30)
      const frontRight = this._minDist(points,  30,  FRONT_HALF_DEG)
      const sideLeft   = this._minDist(points, -SIDE_END_DEG,  -SIDE_START_DEG)
      const sideRight  = this._minDist(points,  SIDE_START_DEG,  SIDE_END_DEG)

      // 综合前方危险（正前 + 对角扇区）
      const frontAll = Math.min(front, frontLeft, frontRight)

      if (frontAll < STOP_MM) {
        // ── 紧急后退 + 果断转向 ───────────────────────────────────
        console.log(
          `[Explorer] 紧急避障：正前=${front}mm 前左=${frontLeft}mm 前右=${frontRight}mm`
        )
        this._motor('stop')
        await this._sleep(150)
        this._motor('backward', FORWARD_SPEED, REVERSE_DURATION)
        await this._sleep(REVERSE_DURATION * 1000 + 150)

        const turnDir = this._chooseTurnDir(frontLeft, frontRight, sideLeft, sideRight)
        await this._turnUntilClear(turnDir)

      } else if (front < CLEAR_MM) {
        // ── 正前偏近：果断转向（不 nudge）────────────────────────
        // 选侧方更空旷的方向旋转一步，然后重新检测
        const turnDir = sideLeft >= sideRight ? 'turn_left' : 'turn_right'
        console.log(
          `[Explorer] 转向 ${turnDir}：正前=${front}mm 左侧=${sideLeft}mm 右侧=${sideRight}mm`
        )
        this._motor(turnDir, TURN_SPEED, TURN_STEP_DURATION)
        await this._sleep(TURN_STEP_DURATION * 1000 + 100)

      } else {
        // ── 正前开阔：脉冲前进 ────────────────────────────────────
        this._motor('forward', FORWARD_SPEED, BURST_DURATION)
        await this._sleep(BURST_DURATION * 1000 + 50)
      }
    }
  }

  /**
   * 选择紧急转向方向：
   * 优先远离对角障碍更近的一侧；差距不明显时参考侧方整体空间。
   */
  private _chooseTurnDir(
    frontLeft: number,
    frontRight: number,
    sideLeft: number,
    sideRight: number,
  ): 'turn_left' | 'turn_right' {
    if (Math.abs(frontLeft - frontRight) > 150) {
      // frontLeft 小 → 左前有障碍 → 转右
      return frontLeft <= frontRight ? 'turn_right' : 'turn_left'
    }
    return sideLeft >= sideRight ? 'turn_left' : 'turn_right'
  }

  /**
   * 持续转向直到正前扇区（±30°）超过 TURN_EXIT_MM。
   * 只检查正前，不要求对角也清晰，避免在普通房间永远找不到"全清"方向。
   */
  private async _turnUntilClear(dir: 'turn_left' | 'turn_right'): Promise<void> {
    let steps = 0
    let currentDir = dir

    while (this._running) {
      this._motor(currentDir, TURN_SPEED, TURN_STEP_DURATION)
      await this._sleep(TURN_STEP_DURATION * 1000 + 100)

      const points = await this._fetchLidar()
      if (!points) continue

      // 只用正前扇区判断是否可以前进
      const front = this._minDist(points, -30, 30)
      console.log(`[Explorer] 转向中（步 ${steps + 1}/${MAX_TURN_STEPS}）：正前=${front}mm`)

      if (front > TURN_EXIT_MM) {
        console.log('[Explorer] 正前已开阔，继续前进')
        return
      }

      steps++
      if (steps >= MAX_TURN_STEPS) {
        console.warn('[Explorer] 多步转向未找到出路，换方向')
        currentDir = currentDir === 'turn_left' ? 'turn_right' : 'turn_left'
        steps = 0
        this._motor('backward', FORWARD_SPEED, REVERSE_DURATION)
        await this._sleep(REVERSE_DURATION * 1000 + 150)
      }
    }
  }

  // ── 工具方法 ────────────────────────────────────────────────────

  private async _fetchLidar(): Promise<LidarPoint[] | null> {
    try {
      const res = await fetch(`${PLATFORM_URL}/lidar/scan/valid`, {
        signal: AbortSignal.timeout(600),
      })
      if (!res.ok) return null
      const data = (await res.json()) as { points: LidarPoint[] }
      return data.points.length > 0 ? data.points : null
    } catch {
      return null
    }
  }

  /**
   * 计算指定角度扇区内的最近障碍距离。
   * LD06：0°=正前，顺时针增加。负角度 = 逆时针（左侧）。
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

  private async _waitForConnection(timeoutMs: number): Promise<boolean> {
    const start = Date.now()
    while (Date.now() - start < timeoutMs) {
      if (PlatformConnector.isConnected) return true
      await this._sleep(300)
    }
    return false
  }
}

declare global {
  // eslint-disable-next-line no-var
  var __explorer: ExplorerClass | undefined
}

export const Explorer =
  globalThis.__explorer ??
  (globalThis.__explorer = new ExplorerClass())
