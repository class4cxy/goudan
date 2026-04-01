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

/** 语音/手动控制默认速度（0–100） */
const MANUAL_DEFAULT_SPEED = Number(process.env.MANUAL_DEFAULT_SPEED ?? process.env.CHASSIS_DEFAULT_SPEED ?? '55')

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
}): Promise<{ ok: boolean; detail?: string }> {
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
    const data = await res.json() as { ok: boolean; timeout?: boolean }
    if (data.timeout) return { ok: false, detail: '运动超时，里程计/IMU 可能未正常工作' }
    return { ok: data.ok }
  } catch (err) {
    return { ok: false, detail: String(err) }
  }
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
      const motorCommand = MOTION_COMMANDS[dest]
      const effectiveSpeed = speed ?? MANUAL_DEFAULT_SPEED

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
            speed: effectiveSpeed,
            timeout_s: Math.ceil(targetAngle / 30) + 5, // 按 30°/s 估算超时
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
            speed: effectiveSpeed,
            message: `已完成${motorCommand === 'turn_left' ? '左转' : '右转'} ${targetAngle}°`,
          }
        }

        // ── 直行有距离：里程计闭环 ────────────────────────────────
        if (distance_mm != null) {
          const signedDistance = motorCommand === 'backward' ? -distance_mm : distance_mm
          const result = await driveClosedLoop({
            distance_mm: signedDistance,
            speed: effectiveSpeed,
            timeout_s: Math.ceil(distance_mm / 100) + 10, // 按 100mm/s 估算超时
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
            distance_mm,
            speed: effectiveSpeed,
            message: `已完成${motorCommand === 'forward' ? '前进' : '后退'} ${distance_mm}mm`,
          }
        }

        // ── 直行无距离：时间控制（持续运动）────────────────────────
        Spine.publish<ActionMotorPayload>({
          type: 'action.motor',
          priority: 'HIGH',
          source: 'brain',
          payload: { command: motorCommand, speed: effectiveSpeed, duration },
          summary: `电机指令：${motorCommand} 速度${effectiveSpeed}%${duration != null ? ` 持续${duration}s` : ''}`,
        })
        return {
          success: true,
          mode: 'motor',
          command: motorCommand,
          speed: effectiveSpeed,
          duration: duration ?? '持续到下一条指令',
          message: `已执行：${destination}${duration != null ? `，持续 ${duration} 秒` : ''}`,
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
