import { tool } from 'ai'
import { z } from 'zod'
import { Spine } from '@/core/runtime/spine'
import type { ActionMotorPayload } from '@/core/runtime/spine'

const PLATFORM_URL = process.env.PLATFORM_URL ?? 'http://localhost:8001'

// 简单动作词 → 标准化动作
const MOTION_COMMANDS: Record<string, ActionMotorPayload['command']> = {
  forward:    'forward',
  backward:   'backward',
  turn_left:  'turn_left',
  turn_right: 'turn_right',
  stop:       'stop',
  向前:       'forward',
  前进:       'forward',
  往前:       'forward',
  后退:       'backward',
  后退走:     'backward',
  左转:       'turn_left',
  向左转:     'turn_left',
  右转:       'turn_right',
  向右转:     'turn_right',
  掉头:       'turn_left',
  调头:       'turn_left',
  停止:       'stop',
  停:         'stop',
}

// 掉头/调头 → 180°
const TURNAROUND_DESTINATIONS = new Set(['掉头', '调头'])

// ── 运动控制常量（直接修改此处，不依赖 env）─────────────────────────────────

/** 语音/手动点动默认速度（0–100） */
const MANUAL_DEFAULT_SPEED = 55

/** 闭环控制默认速度。校准条件：DEBOUNCE_US=3，该速度下编码器稳定，实测约 350mm/s */
const CLOSED_LOOP_DEFAULT_SPEED = 40

/** 闭环速度上限，与校准速度对齐；超出会导致 EMF 噪声加剧，编码器漏脉冲增多 */
const CLOSED_LOOP_MAX_SPEED = 40

/** 无距离直行时的安全点动时长（秒），避免持续运动失控 */
const OPEN_LOOP_DEFAULT_DURATION_S = 0.8

/** 闭环超时硬上限（秒）。偶发漏脉冲时机器人会跑超，此值防止卡死等待 */
const CLOSED_LOOP_TIMEOUT_MAX_S = 90

/**
 * 每 100mm 目标距离分配的超时时长（秒）。
 * 实测 40% 速约 350mm/s，100mm 约 0.3s，×2.5 余量 = 0.8s/100mm
 */
const CLOSED_LOOP_TIMEOUT_DISTANCE_PER_100MM_S = 0.8

/** 每 30° 转向分配的超时时长（秒） */
const CLOSED_LOOP_TIMEOUT_ANGLE_PER_30DEG_S = 3.0

function clampSpeed(v: number): number {
  if (Number.isNaN(v)) return 25
  return Math.max(0, Math.min(100, Math.round(v)))
}

function resolveClosedLoopSpeed(raw: number): number {
  const s = clampSpeed(raw)
  const maxS = clampSpeed(CLOSED_LOOP_MAX_SPEED)
  return Math.min(s, maxS)
}

function inferMotorCommand(destRaw: string): ActionMotorPayload['command'] | undefined {
  const dest = destRaw.trim()
  if (MOTION_COMMANDS[dest]) return MOTION_COMMANDS[dest]

  if (/停|停止|停下/.test(dest)) return 'stop'
  if (/掉头|调头|向左转身|向左掉头/.test(dest)) return 'turn_left'
  if (/左转|向左/.test(dest)) return 'turn_left'
  if (/右转|向右/.test(dest)) return 'turn_right'
  if (/后退|向后|往后|倒车/.test(dest)) return 'backward'
  if (/前进|向前|往前|直行/.test(dest)) return 'forward'
  return undefined
}

function extractDistanceMmFromText(text: string): number | undefined {
  const m = text.match(/(\d+(?:\.\d+)?)\s*(毫米|mm|厘米|cm|米|m)/i)
  if (!m) return undefined
  const n = Number(m[1])
  const unit = m[2].toLowerCase()
  if (!Number.isFinite(n) || n <= 0) return undefined
  if (unit === '毫米' || unit === 'mm') return n
  if (unit === '厘米' || unit === 'cm') return n * 10
  return n * 1000
}

function extractAngleDegFromText(text: string): number | undefined {
  const m = text.match(/(\d+(?:\.\d+)?)\s*(度|°)/i)
  if (!m) return undefined
  const n = Number(m[1])
  if (!Number.isFinite(n) || n <= 0) return undefined
  return n
}

/**
 * 调用 Platform 闭环运动接口 /motor/drive
 * - 转向：IMU 闭环，精确到目标角度后停止
 * - 直行：里程计闭环，精确到目标距离后停止
 */
async function driveClosedLoop(params: {
  distance_mm?: number
  angle_deg?: number
  speed: number
  timeout_s?: number
}): Promise<{ ok: boolean; detail?: string; raw?: Record<string, unknown> }> {
  const timeout_s = params.timeout_s ?? 30
  try {
    const res = await fetch(`${PLATFORM_URL}/motor/drive`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        distance_mm: params.distance_mm,
        angle_deg: params.angle_deg,
        speed: params.speed,
        timeout_s,
      }),
      // 闭环接口是阻塞的，等待时间 = timeout_s + 5s 余量
      signal: AbortSignal.timeout((timeout_s + 5) * 1000),
    })
    const data = await res.json() as { ok: boolean; timeout?: boolean; traveled_mm?: number; rotated_deg?: number }
    if (data.timeout) {
      const traveled = typeof data.traveled_mm === 'number' ? ` traveled=${data.traveled_mm.toFixed(1)}mm` : ''
      const rotated = typeof data.rotated_deg === 'number' ? ` rotated=${data.rotated_deg.toFixed(1)}deg` : ''
      return { ok: false, detail: `运动超时(timeout=true)。${traveled}${rotated}，请检查里程计/IMU。`, raw: data as Record<string, unknown> }
    }
    return { ok: data.ok, raw: data as Record<string, unknown> }
  } catch (err) {
    return { ok: false, detail: String(err) }
  }
}

function calcDistanceTimeoutSec(distanceMm: number): number {
  const estimated = Math.ceil(distanceMm / 100) * CLOSED_LOOP_TIMEOUT_DISTANCE_PER_100MM_S + 4
  const bounded = Math.max(4, Math.min(CLOSED_LOOP_TIMEOUT_MAX_S, estimated))
  return Math.ceil(bounded)
}

function calcAngleTimeoutSec(angleDeg: number): number {
  const estimated = Math.ceil(angleDeg / 30) * CLOSED_LOOP_TIMEOUT_ANGLE_PER_30DEG_S + 3
  const bounded = Math.max(4, Math.min(CLOSED_LOOP_TIMEOUT_MAX_S, estimated))
  return Math.ceil(bounded)
}

/**
 * navigateTo — 机器车移动控制
 *
 * - 转向（左转/右转/掉头）：IMU 闭环，精确角度
 * - 直行有距离：里程计闭环，精确距离
 * - 直行无距离：持续前进，直到下一条 stop 指令
 * - 房间导航（客厅/厨房 等）：发 action.navigate，地图就绪后规划路径
 */
export const navigateTo = tool({
  description:
    '让机器车移动或导航。' +
    '转向（左转/右转/掉头）使用 IMU 闭环精确控制角度，默认左转/右转 90°，掉头 180°。' +
    '直行可指定 distance_mm（毫米），使用里程计闭环；不指定则持续运动直到发出停止指令。' +
    '房间导航（如"去客厅"）需要地图模块支持。',
  inputSchema: z.object({
    destination: z
      .string()
      .describe('目标位置或动作，如"向前"、"左转"、"停止"、"客厅"'),
    speed: z
      .number()
      .min(0)
      .max(100)
      .optional()
      .describe('速度 0–100，不填使用默认速度'),
    distance_mm: z
      .number()
      .positive()
      .optional()
      .describe('直行距离（毫米），填写后使用里程计闭环精确控制距离'),
    angle_deg: z
      .number()
      .positive()
      .optional()
      .describe('转向角度（度），填写后覆盖默认角度（左转/右转默认 90°，掉头默认 180°）'),
    duration: z
      .number()
      .positive()
      .optional()
      .describe('持续时间（秒），仅用于无距离的持续直行，转向时忽略此参数'),
    reason: z
      .string()
      .optional()
      .describe('导航原因，可不填'),
  }),
  execute: async ({ destination, speed, distance_mm, angle_deg, duration, reason }) => {
    try {
      const dest = destination.trim()
      const motorCommand = inferMotorCommand(dest)
      const manualSpeed = clampSpeed(speed ?? MANUAL_DEFAULT_SPEED)
      const closedLoopSpeed = resolveClosedLoopSpeed(speed ?? CLOSED_LOOP_DEFAULT_SPEED)

      // 容错：当模型未显式填 distance_mm/angle_deg，但用户在 destination 里说了“1米/90度”时自动提取
      const inferredDistance = distance_mm ?? extractDistanceMmFromText(dest)
      const inferredAngle = angle_deg ?? extractAngleDegFromText(dest)

      if (motorCommand) {
        // ── 停止：直接发指令 ──────────────────────────────────────
        if (motorCommand === 'stop') {
          Spine.publish<ActionMotorPayload>({
            type: 'action.motor',
            priority: 'HIGH',
            source: 'brain',
            payload: { command: 'stop' },
            summary: '电机指令：stop',
          })
          return { success: true, mode: 'motor', command: 'stop', message: '已停止' }
        }

        // ── 转向：IMU 闭环 ────────────────────────────────────────
        if (motorCommand === 'turn_left' || motorCommand === 'turn_right') {
          const defaultAngle = TURNAROUND_DESTINATIONS.has(dest) ? 180 : 90
          const targetAngle = angle_deg ?? defaultAngle
          // 左转为正角度，右转为负角度
          const signedAngle = motorCommand === 'turn_left' ? targetAngle : -targetAngle

          const result = await driveClosedLoop({
            angle_deg: signedAngle,
            speed: closedLoopSpeed,
            timeout_s: calcAngleTimeoutSec(targetAngle),
          })

          if (!result.ok) {
            return {
              success: false,
              error: result.detail ?? '转向失败',
              fallback: '如需手动控制请发送停止指令',
            }
          }
          return {
            success: true,
            mode: 'closed_loop_angle',
            command: motorCommand,
            angle_deg: targetAngle,
            speed: closedLoopSpeed,
            message: `已完成${motorCommand === 'turn_left' ? '左转' : '右转'} ${targetAngle}°`,
          }
        }

        // ── 直行有距离：里程计闭环 ────────────────────────────────
        if (inferredDistance != null) {
          const signedDistance = motorCommand === 'backward' ? -inferredDistance : inferredDistance
          const result = await driveClosedLoop({
            distance_mm: signedDistance,
            speed: closedLoopSpeed,
            timeout_s: calcDistanceTimeoutSec(inferredDistance),
          })

          if (!result.ok) {
            return {
              success: false,
              error: result.detail ?? '直行失败',
            }
          }
          return {
            success: true,
            mode: 'closed_loop_distance',
            command: motorCommand,
            distance_mm: inferredDistance,
            speed: closedLoopSpeed,
            message: `已完成${motorCommand === 'forward' ? '前进' : '后退'} ${inferredDistance}mm`,
          }
        }

        // ── 直行无距离：时间控制（持续运动）────────────────────────
        const explicitContinuous = /(持续|一直|不停|保持|连续)/.test(dest)
        const safeDuration = duration ?? OPEN_LOOP_DEFAULT_DURATION_S
        Spine.publish<ActionMotorPayload>({
          type: 'action.motor',
          priority: 'HIGH',
          source: 'brain',
          payload: {
            command: motorCommand,
            speed: manualSpeed,
            // 未明确要求“持续运动”时，默认短时点动，避免长时间失控
            duration: explicitContinuous ? duration : safeDuration,
          },
          summary:
            `电机指令：${motorCommand} 速度${manualSpeed}%` +
            `${explicitContinuous ? (duration != null ? ` 持续${duration}s` : ' 持续到停止') : ` 点动${safeDuration}s`}`,
        })
        return {
          success: true,
          mode: 'motor',
          command: motorCommand,
          speed: manualSpeed,
          duration: explicitContinuous ? (duration ?? '持续到下一条指令') : safeDuration,
          message:
            explicitContinuous
              ? `已执行：${destination}${duration != null ? `，持续 ${duration} 秒` : ''}`
              : `未提供距离，已执行安全点动 ${safeDuration}s（如需持续请说“持续前进”）`,
        }

      } else {
        // ── 房间导航 ──────────────────────────────────────────────
        Spine.publish({
          type: 'action.navigate',
          priority: 'MEDIUM',
          source: 'brain',
          payload: { destination, reason },
          summary: `导航意图：前往「${destination}」${reason ? `（${reason}）` : ''}`,
        })
        return {
          success: true,
          mode: 'navigate',
          destination,
          message: `导航意图已记录：前往「${destination}」。地图模块就绪后自动规划路径执行。`,
        }
      }
    } catch (err) {
      return { success: false, error: String(err) }
    }
  },
})
