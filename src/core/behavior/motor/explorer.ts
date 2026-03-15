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
 *
 *   雷达装高时的盲区（重要）：
 *     LD06 为 2D 平面雷达，只扫描一个水平面。若雷达装在车身最高处，
 *     低于该平面的障碍物（桌沿、矮栏、台阶、矮墙）雷达完全“看不见”，
 *     车体仍会撞上。此时可设 EXPLORER_HIGH_LIDAR=1 启用保守模式（缩短
 *     步长、降速），减轻碰撞；根本解决需保险杠或底盘高度测距等硬件。
 */

import { PlatformConnector } from '../../runtime/platform-connector'

const PLATFORM_URL = process.env.PLATFORM_URL ?? 'http://localhost:8001'

// ── 距离阈值（按实际车体几何推算）──────────────────────────────────
//
//   车体：长 250mm / 宽 190mm / 高 170mm
//   雷达在车体中心顶部：前保险杠距雷达 ≈ 125mm，半车宽 95mm
//
//   正前（±30°）紧急阈值：
//     保险杠到雷达 125mm + 刹车余量 95mm = 220mm
//   对角（30°~50°）紧急阈值（横向分量 < 半车宽时才危险）：
//     d × sin(40°) < 95mm  →  d < 148mm，取 130mm
//     → 239mm 对角障碍横向 = 154mm > 95mm，不触发 ✓
//     →  93mm 对角障碍横向 =  60mm < 95mm，触发紧急 ✓
//
/** 正前 ±30° 停车阈值 */
const FRONT_STOP_MM      = 220
/** 对角 30°~50° 停车阈值（仅横向危险时触发，不会被远处斜向障碍误报） */
const DIAG_STOP_MM       = 130
/** 正前 ±30° 前进阈值：超过才脉冲前进（车短，500mm 足够） */
const CLEAR_MM           = 500
/** 转向完成阈值：_turnUntilClear 中正前 > 此值即退出 */
const TURN_EXIT_MM       = 300

/** 雷达装高时的保守阈值：更早停车、更短“开阔”距离，减少盲区冲撞 */
const FRONT_STOP_MM_HIGH_LIDAR  = 280
const CLEAR_MM_HIGH_LIDAR      = 350
const BURST_DURATION_HIGH_LIDAR = 0.22
const FORWARD_SPEED_HIGH_LIDAR = 22

// ── 运动参数 ─────────────────────────────────────────────────────
const FORWARD_SPEED      = 30
const TURN_SPEED         = 35
/**
 * 正常脉冲前进时长（秒）。
 * 脱困后使用更长的 ESCAPE_BURST_DURATION，把机器人真正带离障碍区域。
 */
const BURST_DURATION     = 0.4
/** 脱困后的前进时长（秒）：比普通脉冲长，确保离开障碍区 */
const ESCAPE_BURST_DURATION = 0.8
/** 普通后退时长（秒） */
const REVERSE_DURATION   = 0.6
/** 卡死时大后退时长（秒）：连续卡死后彻底换区域 */
const BIG_REVERSE_DURATION = 1.5
/** 单次转向时长（秒） */
const TURN_STEP_DURATION = 0.5
/** 单次转向最大步数（超过后换方向+后退） */
const MAX_TURN_STEPS     = 8

// ── 扇区角度 ─────────────────────────────────────────────────────
//
//   旧版用 ±30° 作为"正前"扇区，在走廊中侧壁（200mm × sin(30°) = 400mm 处）
//   会落入该扇区触发"前方被堵"，导致机器人无法直线穿越走廊。
//
//   新版：
//     DRIVE_HALF_DEG = 15°  → 窄扇区，只看"几乎正前"，走廊侧壁不影响前进判断
//     FRONT_HALF_DEG = 50°  → 宽扇区（15°~50°），捕获斜向障碍，触发 DIAG_STOP
//
//   几何验证（走廊宽 400mm = ±200mm 侧壁）：
//     侧壁进入 ±15° 扇区时：距离 = 200/sin(15°) ≈ 773mm，离得很远，不触发
//     侧壁进入 ±30° 扇区时：距离 = 200/sin(30°) = 400mm，会误触发（旧 bug）
//
/** 正前驾驶扇区半角（°）：决定"能否前进" */
const DRIVE_HALF_DEG     = 10
/** 对角扇区边界（°）：15°~50°，紧急侧碰检测 */
const FRONT_HALF_DEG     = 50
/** 侧方扇区范围（°）：评估左/右整体空旷度，用于选择转向方向 */
const SIDE_START_DEG     = 60
const SIDE_END_DEG       = 140

interface LidarPoint {
  angle: number
  distance: number
  confidence: number
}
interface SlamPose {
  x_mm: number
  y_mm: number
  theta_deg: number
}

// ── Explorer 单例 ─────────────────────────────────────────────────

/** 大角度逃脱旋转时长（秒）：约 120°，用于振荡时跳出障碍物簇 */
const ESCAPE_ROTATE_DURATION = 1.8

/** 雷达装在车身最高处时设为 1，启用保守前进（缩短步长、降速、更早停车） */
const HIGH_LIDAR = process.env.EXPLORER_HIGH_LIDAR === '1'
/** 前进后最小位移阈值（mm）：低于该值记为“无进展” */
const PROGRESS_MIN_MOVE_MM = Number(process.env.EXPLORER_PROGRESS_MIN_MOVE_MM ?? '25')
/** 连续无进展脉冲次数：达到后触发强制脱困 */
const STUCK_BURSTS_LIMIT = Number(process.env.EXPLORER_STUCK_BURSTS ?? (HIGH_LIDAR ? '2' : '3'))

class ExplorerClass {
  private _running            = false
  /** 连续卡死轮数（每次 _turnUntilClear 超过 MAX_TURN_STEPS 视为一次卡死） */
  private _stuckCount         = 0
  /** 上一次紧急避障的触发侧（left/right/null） */
  private _lastEmergencySide: 'left' | 'right' | null = null
  /** 交替触发计数：L→R→L 视为振荡 */
  private _oscillationCount   = 0
  /** 连续前进无位移计数（用于“走不动还走”的硬保护） */
  private _noProgressBursts   = 0

  get isRunning() { return this._running }

  /** 当前生效的避障/前进参数（雷达装高时用保守值） */
  private _frontStopMm()  { return HIGH_LIDAR ? FRONT_STOP_MM_HIGH_LIDAR : FRONT_STOP_MM }
  private _clearMm()     { return HIGH_LIDAR ? CLEAR_MM_HIGH_LIDAR : CLEAR_MM }
  private _burstDuration() { return HIGH_LIDAR ? BURST_DURATION_HIGH_LIDAR : BURST_DURATION }
  private _forwardSpeed() { return HIGH_LIDAR ? FORWARD_SPEED_HIGH_LIDAR : FORWARD_SPEED }

  async start(): Promise<void> {
    if (this._running) return
    this._running = true

    try {
      const res = await fetch(`${PLATFORM_URL}/slam/start`, {
        method: 'POST',
        signal: AbortSignal.timeout(5000),
      })
      if (!res.ok) {
        const detail = await res.text().catch(() => '')
        console.error(`[Explorer] SLAM 启动失败：HTTP ${res.status} ${detail}`)
        this._running = false
        return
      }
      console.log('[Explorer] SLAM 已启动')
    } catch (e) {
      console.error('[Explorer] SLAM 启动异常，探索取消：', e)
      this._running = false
      return
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

      // 五扇区
      // front：±15° 窄扇区，仅看正前，走廊侧壁不影响前进判断
      // frontLeft/Right：15°~50°，紧急侧碰检测
      const front      = this._minDist(points, -DRIVE_HALF_DEG,  DRIVE_HALF_DEG)
      const frontLeft  = this._minDist(points, -FRONT_HALF_DEG, -DRIVE_HALF_DEG)
      const frontRight = this._minDist(points,  DRIVE_HALF_DEG,  FRONT_HALF_DEG)
      const sideLeft   = this._minDist(points, -SIDE_END_DEG,   -SIDE_START_DEG)
      const sideRight  = this._minDist(points,  SIDE_START_DEG,   SIDE_END_DEG)

      // 正前和对角使用不同阈值（避免对角远处障碍误触发紧急）
      const diagMin    = Math.min(frontLeft, frontRight)
      const isEmergency = front < this._frontStopMm() || diagMin < DIAG_STOP_MM

      if (isEmergency) {
        // ── 紧急避障 ─────────────────────────────────────────────
        console.log(
          `[Explorer] 紧急避障：正前=${front}mm 前左=${frontLeft}mm 前右=${frontRight}mm`
        )

        // 振荡检测：判断本次是左侧还是右侧触发
        const side: 'left' | 'right' = frontLeft < frontRight ? 'left' : 'right'
        if (this._lastEmergencySide !== null && this._lastEmergencySide !== side) {
          // 和上次相反 → 可能在振荡
          this._oscillationCount++
        } else {
          this._oscillationCount = 0
        }
        this._lastEmergencySide = side

        if (this._oscillationCount >= 2) {
          // ── 振荡确认：大角度旋转 + 长距离后退跳出障碍物簇 ─────
          console.warn(`[Explorer] 振荡检测（${this._oscillationCount} 次交替），执行大角度逃脱`)
          this._oscillationCount = 0
          this._lastEmergencySide = null
          this._motor('stop')
          await this._sleep(150)
          // 先大角度旋转（约 120°）彻底改变方向
          const escapeDir = side === 'left' ? 'turn_right' : 'turn_left'
          this._motor(escapeDir, TURN_SPEED, ESCAPE_ROTATE_DURATION)
          await this._sleep(ESCAPE_ROTATE_DURATION * 1000 + 150)
          // 再长距离后退，离开障碍物簇
          this._motor('backward', this._forwardSpeed(), BIG_REVERSE_DURATION)
          await this._sleep(BIG_REVERSE_DURATION * 1000 + 150)
          this._noProgressBursts = 0
          continue
        }

        this._motor('stop')
        await this._sleep(150)
        this._motor('backward', this._forwardSpeed(), REVERSE_DURATION)
        await this._sleep(REVERSE_DURATION * 1000 + 150)
        this._noProgressBursts = 0

        const turnDir = this._chooseTurnDir(frontLeft, frontRight, sideLeft, sideRight)
        const escaped = await this._turnUntilClear(turnDir)

        if (escaped) {
          // 成功脱困：用更长脉冲把机器人真正带离障碍区，然后重置卡死计数
          this._stuckCount = 0
          this._motor('forward', this._forwardSpeed(), ESCAPE_BURST_DURATION)
          await this._sleep(ESCAPE_BURST_DURATION * 1000 + 50)
        } else {
          // 两个方向都没出路：卡死一次
          this._stuckCount++
          console.warn(`[Explorer] 卡死第 ${this._stuckCount} 次，执行大后退`)
          const reverseDur = this._stuckCount >= 2 ? BIG_REVERSE_DURATION : REVERSE_DURATION
          this._motor('backward', this._forwardSpeed(), reverseDur)
          await this._sleep(reverseDur * 1000 + 150)
          if (this._stuckCount >= 2) this._stuckCount = 0
          this._noProgressBursts = 0
        }

      } else if (front < this._clearMm()) {
        // ── 正前偏近：朝侧方更空旷一侧旋转一步 ──────────────────
        const turnDir = sideLeft >= sideRight ? 'turn_left' : 'turn_right'
        console.log(
          `[Explorer] 转向 ${turnDir}：正前=${front}mm 左侧=${sideLeft}mm 右侧=${sideRight}mm`
        )
        this._motor(turnDir, TURN_SPEED, TURN_STEP_DURATION)
        await this._sleep(TURN_STEP_DURATION * 1000 + 100)
        this._noProgressBursts = 0

      } else {
        // ── 正前开阔：脉冲前进，重置所有计数 ────────────────────
        this._stuckCount       = 0
        this._oscillationCount = 0
        this._lastEmergencySide = null
        const prePose = await this._fetchPose()
        this._motor('forward', this._forwardSpeed(), this._burstDuration())
        await this._sleep(this._burstDuration() * 1000 + 50)
        const postPose = await this._fetchPose()

        if (prePose && postPose) {
          const moved = Math.hypot(postPose.x_mm - prePose.x_mm, postPose.y_mm - prePose.y_mm)
          if (moved < PROGRESS_MIN_MOVE_MM) {
            this._noProgressBursts++
            console.warn(
              `[Explorer] 前进无进展：位移=${moved.toFixed(1)}mm，连续=${this._noProgressBursts}/${STUCK_BURSTS_LIMIT}`
            )
          } else {
            this._noProgressBursts = 0
          }
        }

        if (this._noProgressBursts >= STUCK_BURSTS_LIMIT) {
          // 位姿几乎不变却持续前进，判定“顶住了”：立刻停止并强制脱困
          console.warn('[Explorer] 检测到走不动还在走，执行强制脱困')
          this._motor('stop')
          await this._sleep(120)
          this._motor('backward', this._forwardSpeed(), BIG_REVERSE_DURATION)
          await this._sleep(BIG_REVERSE_DURATION * 1000 + 150)
          const hardTurnDir = sideLeft >= sideRight ? 'turn_left' : 'turn_right'
          this._motor(hardTurnDir, TURN_SPEED, ESCAPE_ROTATE_DURATION)
          await this._sleep(ESCAPE_ROTATE_DURATION * 1000 + 150)
          this._noProgressBursts = 0
        }
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
      return frontLeft <= frontRight ? 'turn_right' : 'turn_left'
    }
    return sideLeft >= sideRight ? 'turn_left' : 'turn_right'
  }

  /**
   * 持续转向直到正前扇区（±30°）> TURN_EXIT_MM。
   * 最多换方向一次（两个方向各转 MAX_TURN_STEPS 步）。
   * 返回 true = 找到出路；false = 两个方向都被堵死（卡死）。
   */
  private async _turnUntilClear(dir: 'turn_left' | 'turn_right'): Promise<boolean> {
    let steps      = 0
    let currentDir = dir
    let switches   = 0          // 已换方向次数

    while (this._running) {
      this._motor(currentDir, TURN_SPEED, TURN_STEP_DURATION)
      await this._sleep(TURN_STEP_DURATION * 1000 + 100)

      const points = await this._fetchLidar()
      if (!points) continue

      const front = this._minDist(points, -DRIVE_HALF_DEG, DRIVE_HALF_DEG)
      console.log(
        `[Explorer] 转向中（步 ${steps + 1}/${MAX_TURN_STEPS}，换向 ${switches} 次）：正前=${front}mm`
      )

      if (front > TURN_EXIT_MM) {
        console.log('[Explorer] 正前已开阔，脱困成功')
        return true
      }

      steps++
      if (steps >= MAX_TURN_STEPS) {
        switches++
        if (switches >= 2) {
          // 两个方向都找不到出路 → 告知调用方卡死
          console.warn('[Explorer] 两个方向均未找到出路，卡死')
          return false
        }
        console.warn('[Explorer] 该方向未找到出路，换反方向')
        currentDir = currentDir === 'turn_left' ? 'turn_right' : 'turn_left'
        steps = 0
        this._motor('backward', this._forwardSpeed(), REVERSE_DURATION)
        await this._sleep(REVERSE_DURATION * 1000 + 150)
      }
    }
    return false
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

  private async _fetchPose(): Promise<SlamPose | null> {
    try {
      const res = await fetch(`${PLATFORM_URL}/slam/pose`, {
        signal: AbortSignal.timeout(700),
      })
      if (!res.ok) return null
      return (await res.json()) as SlamPose
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
