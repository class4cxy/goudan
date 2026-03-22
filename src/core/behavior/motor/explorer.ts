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
 *     frontAll < STOP_MM   → 紧急停车 + 果断转向
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
/** 正前 ±30° 前进阈值：超过才脉冲前进（过大容易在小空间持续原地转向） */
const CLEAR_MM           = 380
/** 转向完成阈值：_turnUntilClear 中正前 > 此值即退出 */
const TURN_EXIT_MM       = Number(process.env.EXPLORER_TURN_EXIT_MM ?? '240')
/** 斜向退出阈值：用于识别 30°~50° 方向可通行出口 */
const TURN_EXIT_DIAG_MM  = Number(process.env.EXPLORER_TURN_EXIT_DIAG_MM ?? '330')

/** 雷达装高时的保守阈值：更早停车、更短“开阔”距离，减少盲区冲撞 */
const FRONT_STOP_MM_HIGH_LIDAR  = 280
const CLEAR_MM_HIGH_LIDAR      = 300
const BURST_DURATION_HIGH_LIDAR = 0.22
const FORWARD_SPEED_HIGH_LIDAR = 22

// ── 运动参数 ─────────────────────────────────────────────────────
const FORWARD_SPEED      = Number(process.env.EXPLORER_FORWARD_SPEED ?? '28')
const TURN_SPEED         = Number(process.env.EXPLORER_TURN_SPEED ?? '30')
/**
 * 正常脉冲前进时长（秒）。
 * 脱困后使用更长的 ESCAPE_BURST_DURATION，把机器人真正带离障碍区域。
 */
const BURST_DURATION     = Number(process.env.EXPLORER_BURST_DURATION ?? '0.5')
/** 脱困后的前进时长（秒）：比普通脉冲长，确保离开障碍区 */
const ESCAPE_BURST_DURATION = 0.8
/** 普通后退时长（秒） */
const REVERSE_DURATION   = 0.6
/** 卡死时大后退时长（秒）：连续卡死后彻底换区域 */
const BIG_REVERSE_DURATION = 1.5
/** 单次转向时长（秒）：建图避障需足够转角，与语音左转/右转独立 */
const TURN_STEP_DURATION = Number(process.env.EXPLORER_TURN_STEP_DURATION ?? '0.55')
/** 单次转向最大步数（超过后换方向+后退） */
const MAX_TURN_STEPS     = Number(process.env.EXPLORER_MAX_TURN_STEPS ?? '6')

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
interface UltrasonicStatus {
  is_simulation: boolean
  latest: {
    timestamp_ms: number
    distance_cm: number
    is_too_close: boolean
  } | null
}

// ── Explorer 单例 ─────────────────────────────────────────────────

/** 大角度逃脱旋转时长（秒）：约 90°，用于振荡时跳出障碍物簇 */
const ESCAPE_ROTATE_DURATION = Number(process.env.EXPLORER_ESCAPE_ROTATE_DURATION ?? '1.75')

/** 雷达装在车身最高处时设为 1，启用保守前进（缩短步长、降速、更早停车） */
const HIGH_LIDAR = process.env.EXPLORER_HIGH_LIDAR === '1'
/** 前进后最小位移阈值（mm）：低于该值记为“无进展” */
const PROGRESS_MIN_MOVE_MM = Number(process.env.EXPLORER_PROGRESS_MIN_MOVE_MM ?? '25')
/** 连续无进展脉冲次数：达到后触发强制脱困 */
const STUCK_BURSTS_LIMIT = Number(process.env.EXPLORER_STUCK_BURSTS ?? (HIGH_LIDAR ? '2' : '3'))
/** 是否启用超声波前向安全门（默认启用，设 0 可关闭） */
const USE_ULTRASONIC_GUARD = process.env.EXPLORER_USE_ULTRASONIC !== '0'
/** 超声波硬急停阈值（cm）：低于该值一律不前进 */
const ULTRASONIC_STOP_CM = Number(
  process.env.EXPLORER_ULTRASONIC_STOP_CM ?? process.env.ULTRASONIC_TOO_CLOSE_CM ?? '25'
)
/** 硬急停阈值安全下限（cm）：避免阈值设得过小导致近距离来不及刹停 */
const ULTRASONIC_STOP_MIN_CM = Number(process.env.EXPLORER_ULTRASONIC_STOP_MIN_CM ?? '20')
/** 超声波谨慎阈值（cm）：低于该值优先转向，不执行前进脉冲 */
const ULTRASONIC_TURN_CM = Number(process.env.EXPLORER_ULTRASONIC_TURN_CM ?? '35')
/** 超声波数据新鲜度阈值（ms）：超过则视为不可用 */
const ULTRASONIC_TTL_MS = Number(process.env.EXPLORER_ULTRASONIC_TTL_MS ?? '350')
/** 前进分段时长（秒）：每段前都做一次超声波刹车检查 */
const FORWARD_GUARD_STEP_S = Number(process.env.EXPLORER_FORWARD_GUARD_STEP_S ?? '0.08')
/** 小空间连续原地转向上限：达到后尝试短前探，打破“打转” */
const MAX_INPLACE_TURNS = Number(process.env.EXPLORER_MAX_INPLACE_TURNS ?? '4')
/** 小空间短前探时长（秒） */
const INPLACE_POKE_DURATION = Number(process.env.EXPLORER_INPLACE_POKE_DURATION ?? '0.2')
/** 小空间短前探速度（0-100） */
const INPLACE_POKE_SPEED = Number(process.env.EXPLORER_INPLACE_POKE_SPEED ?? '22')
/** 侧向差值低于该阈值时维持上次转向，减少小空间左右抖动 */
const TURN_HYSTERESIS_MM = Number(process.env.EXPLORER_TURN_HYSTERESIS_MM ?? '120')
/** 斜向引导前探时长（秒）：识别到斜向出口后轻推进入该方向 */
const DIAG_GUIDE_POKE_DURATION = Number(process.env.EXPLORER_DIAG_GUIDE_POKE_DURATION ?? '0.22')
/** 斜向引导前探速度（0-100） */
const DIAG_GUIDE_POKE_SPEED = Number(process.env.EXPLORER_DIAG_GUIDE_POKE_SPEED ?? '24')
/** 是否允许后退动作（默认关闭：优先“停+转+前进”） */
const ALLOW_REVERSE = process.env.EXPLORER_ALLOW_REVERSE === '1'
const EFFECTIVE_ULTRASONIC_STOP_CM = Math.max(ULTRASONIC_STOP_CM, ULTRASONIC_STOP_MIN_CM)

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
  /** 连续“仅转向未前进”次数（用于小空间打转检测） */
  private _inplaceTurnStreak  = 0
  /** 上一次转向方向（用于小空间迟滞，避免左右来回抖动） */
  private _lastTurnDir: 'turn_left' | 'turn_right' | null = null

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
      const ultrasonicCm = await this._fetchUltrasonicDistanceCm()
      const ultraHardStop = ultrasonicCm !== null && ultrasonicCm <= EFFECTIVE_ULTRASONIC_STOP_CM
      const ultraCaution = ultrasonicCm !== null && ultrasonicCm <= ULTRASONIC_TURN_CM

      // 正前和对角使用不同阈值（避免对角远处障碍误触发紧急）
      const diagMin    = Math.min(frontLeft, frontRight)
      const isEmergency = front < this._frontStopMm() || diagMin < DIAG_STOP_MM || ultraHardStop

      if (isEmergency) {
        // ── 紧急避障 ─────────────────────────────────────────────
        console.log(
          `[Explorer] 紧急避障：正前=${front}mm 前左=${frontLeft}mm 前右=${frontRight}mm` +
          `${ultrasonicCm !== null ? ` 超声波=${ultrasonicCm.toFixed(1)}cm` : ''}`
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
          if (ALLOW_REVERSE) {
            // 可选：保留后退能力（默认关闭）
            this._motor('backward', this._forwardSpeed(), BIG_REVERSE_DURATION)
            await this._sleep(BIG_REVERSE_DURATION * 1000 + 150)
          } else {
            // 默认：仅停+转，不后退
            this._motor('stop')
            await this._sleep(120)
          }
          this._noProgressBursts = 0
          this._inplaceTurnStreak = 0
          continue
        }

        this._motor('stop')
        await this._sleep(150)
        if (ALLOW_REVERSE) {
          this._motor('backward', this._forwardSpeed(), REVERSE_DURATION)
          await this._sleep(REVERSE_DURATION * 1000 + 150)
        }
        this._noProgressBursts = 0
        this._inplaceTurnStreak = 0

        const turnDir = this._chooseTurnDir(frontLeft, frontRight, sideLeft, sideRight, false)
        const escaped = await this._turnUntilClear(turnDir)

        if (escaped) {
          // 成功脱困：用更长脉冲把机器人真正带离障碍区，然后重置卡死计数
          this._stuckCount = 0
          await this._forwardWithUltrasonicGuard(ESCAPE_BURST_DURATION, this._forwardSpeed())
        } else {
          // 两个方向都没出路：卡死一次
          this._stuckCount++
          console.warn(`[Explorer] 卡死第 ${this._stuckCount} 次，执行强制换向`)
          if (ALLOW_REVERSE) {
            const reverseDur = this._stuckCount >= 2 ? BIG_REVERSE_DURATION : REVERSE_DURATION
            this._motor('backward', this._forwardSpeed(), reverseDur)
            await this._sleep(reverseDur * 1000 + 150)
          } else {
            const forceDir = sideLeft >= sideRight ? 'turn_left' : 'turn_right'
            this._motor(forceDir, TURN_SPEED, ESCAPE_ROTATE_DURATION)
            await this._sleep(ESCAPE_ROTATE_DURATION * 1000 + 120)
          }
          if (this._stuckCount >= 2) this._stuckCount = 0
          this._noProgressBursts = 0
          this._inplaceTurnStreak = 0
        }

      } else if (front < this._clearMm() || ultraCaution) {
        // ── 正前偏近：朝侧方更空旷一侧旋转一步 ──────────────────
        const turnDir = this._chooseTurnDir(frontLeft, frontRight, sideLeft, sideRight, true)
        console.log(
          `[Explorer] 转向 ${turnDir}：正前=${front}mm 左侧=${sideLeft}mm 右侧=${sideRight}mm` +
          `${ultrasonicCm !== null ? ` 超声波=${ultrasonicCm.toFixed(1)}cm` : ''}`
        )
        this._motor(turnDir, TURN_SPEED, TURN_STEP_DURATION)
        await this._sleep(TURN_STEP_DURATION * 1000 + 100)
        this._noProgressBursts = 0
        this._inplaceTurnStreak++

        // 斜向出口增强：若 30°~50° 某一侧明显开阔，给一次短前探，避免一直原地旋转。
        const bestDiag = Math.max(frontLeft, frontRight)
        if (
          bestDiag > TURN_EXIT_DIAG_MM &&
          front > this._frontStopMm() + 40 &&
          !ultraHardStop
        ) {
          await this._forwardWithUltrasonicGuard(DIAG_GUIDE_POKE_DURATION, DIAG_GUIDE_POKE_SPEED)
          this._inplaceTurnStreak = 0
        }

        if (
          this._inplaceTurnStreak >= MAX_INPLACE_TURNS &&
          front > this._frontStopMm() + 60 &&
          !ultraHardStop
        ) {
          // 小空间“方向对但打转”时，给一个安全短前探打破原地旋转循环。
          console.warn('[Explorer] 小空间连续转向，执行短前探脱困')
          await this._forwardWithUltrasonicGuard(INPLACE_POKE_DURATION, INPLACE_POKE_SPEED)
          this._inplaceTurnStreak = 0
        }
        if (this._inplaceTurnStreak >= MAX_INPLACE_TURNS * 2) {
          // 仍在原地转：执行“后退+大角度换向”强脱困，避免 SLAM 在原地发散。
          console.warn('[Explorer] 连续原地转向未脱困，执行强制换向脱困')
          const escapeDir = turnDir === 'turn_left' ? 'turn_right' : 'turn_left'
          if (ALLOW_REVERSE) {
            this._motor('backward', this._forwardSpeed(), REVERSE_DURATION)
            await this._sleep(REVERSE_DURATION * 1000 + 120)
          }
          this._motor(escapeDir, TURN_SPEED, ESCAPE_ROTATE_DURATION)
          await this._sleep(ESCAPE_ROTATE_DURATION * 1000 + 120)
          this._inplaceTurnStreak = 0
          this._lastTurnDir = escapeDir
        }

      } else {
        // ── 正前开阔：脉冲前进，重置所有计数 ────────────────────
        this._stuckCount       = 0
        this._oscillationCount = 0
        this._lastEmergencySide = null
        this._inplaceTurnStreak = 0
        const prePose = await this._fetchPose()
        await this._forwardWithUltrasonicGuard(this._burstDuration(), this._forwardSpeed())
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
          if (ALLOW_REVERSE) {
            this._motor('backward', this._forwardSpeed(), BIG_REVERSE_DURATION)
            await this._sleep(BIG_REVERSE_DURATION * 1000 + 150)
          }
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
    preferStable: boolean,
  ): 'turn_left' | 'turn_right' {
    if (
      preferStable &&
      this._lastTurnDir &&
      Math.abs(frontLeft - frontRight) <= 150 &&
      Math.abs(sideLeft - sideRight) <= TURN_HYSTERESIS_MM
    ) {
      return this._lastTurnDir
    }
    if (Math.abs(frontLeft - frontRight) > 150) {
      const dir = frontLeft <= frontRight ? 'turn_right' : 'turn_left'
      this._lastTurnDir = dir
      return dir
    }
    const dir = sideLeft >= sideRight ? 'turn_left' : 'turn_right'
    this._lastTurnDir = dir
    return dir
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
      const frontLeft = this._minDist(points, -FRONT_HALF_DEG, -DRIVE_HALF_DEG)
      const frontRight = this._minDist(points, DRIVE_HALF_DEG, FRONT_HALF_DEG)
      const diagClear = Math.max(frontLeft, frontRight)
      console.log(
        `[Explorer] 转向中（步 ${steps + 1}/${MAX_TURN_STEPS}，换向 ${switches} 次）：` +
        `正前=${front}mm 斜向=${diagClear}mm`
      )

      if (front > TURN_EXIT_MM || (front > this._frontStopMm() && diagClear > TURN_EXIT_DIAG_MM)) {
        console.log('[Explorer] 前方/斜向已开阔，脱困成功')
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
        if (ALLOW_REVERSE) {
          this._motor('backward', this._forwardSpeed(), REVERSE_DURATION)
          await this._sleep(REVERSE_DURATION * 1000 + 150)
        } else {
          this._motor('stop')
          await this._sleep(120)
        }
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
   * 读取前向超声波距离（cm）。
   * 返回 null 表示未启用、传感器模拟模式、读数缺失或读数过期。
   */
  private async _fetchUltrasonicDistanceCm(): Promise<number | null> {
    if (!USE_ULTRASONIC_GUARD) return null
    try {
      const res = await fetch(`${PLATFORM_URL}/ultrasonic/status`, {
        signal: AbortSignal.timeout(600),
      })
      if (!res.ok) return null
      const status = (await res.json()) as UltrasonicStatus
      if (status.is_simulation || !status.latest) return null
      const ageMs = Date.now() - status.latest.timestamp_ms
      if (ageMs > ULTRASONIC_TTL_MS) return null
      return status.latest.distance_cm
    } catch {
      return null
    }
  }

  /**
   * 分段前进并在每段前做超声波急停检查，避免“脉冲期间顶撞”。
   * 返回 true 表示完整前进，false 表示中途被超声波打断。
   */
  private async _forwardWithUltrasonicGuard(durationS: number, speed: number): Promise<boolean> {
    if (durationS <= 0) return true
    let elapsed = 0
    while (elapsed < durationS && this._running) {
      const distCm = await this._fetchUltrasonicDistanceCm()
      if (distCm !== null && distCm <= EFFECTIVE_ULTRASONIC_STOP_CM) {
        this._motor('stop')
        await this._sleep(80)
        console.warn(
          `[Explorer] 超声波急停：${distCm.toFixed(1)}cm <= ${EFFECTIVE_ULTRASONIC_STOP_CM}cm`
        )
        return false
      }
      const step = Math.min(FORWARD_GUARD_STEP_S, durationS - elapsed)
      this._motor('forward', speed, step)
      await this._sleep(step * 1000 + 20)
      elapsed += step
    }
    return true
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

const g = globalThis as typeof globalThis & { __explorer?: ExplorerClass }
export const Explorer = g.__explorer ?? (g.__explorer = new ExplorerClass())
