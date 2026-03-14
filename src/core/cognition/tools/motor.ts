import { tool } from 'ai'
import { z } from 'zod'
import { Spine } from '@/core/runtime/spine'
import type { ActionMotorPayload } from '@/core/runtime/spine'

// 简单动作词 → action.motor command 映射（直接执行，不需要地图）
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
  停止:       'stop',
  停:         'stop',
}

/**
 * navigateTo — 机器车移动控制
 *
 * - 简单动作（向前 / 后退 / 左转 / 右转 / 停止）：直接发 action.motor，Platform 立即执行
 * - 房间导航（客厅 / 厨房 等）：发 action.navigate，由 MotorEffector 在地图就绪后规划路径
 */
export const navigateTo = tool({
  description:
    '让机器车移动或导航。' +
    '简单动作（向前、后退、左转、右转、停止）会立即执行。' +
    '房间导航（如"去客厅"）需要地图模块支持，会记录意图待地图就绪后执行。' +
    '可以指定 speed（0–100）和 duration（秒数），不填则使用默认速度持续运动直到发出停止指令。',
  inputSchema: z.object({
    destination: z
      .string()
      .describe('目标位置或动作，如"向前"、"左转"、"停止"、"客厅"'),
    speed: z
      .number()
      .min(0)
      .max(100)
      .optional()
      .describe('速度 0–100，不填使用默认速度（60）'),
    duration: z
      .number()
      .positive()
      .optional()
      .describe('持续时间（秒），不填则持续运动直到下一条指令'),
    reason: z
      .string()
      .optional()
      .describe('导航原因，如"巡检房间"、"跟随主人"，可不填'),
  }),
  execute: async ({ destination, speed, duration, reason }) => {
    try {
      const motorCommand = MOTION_COMMANDS[destination.trim()]

      if (motorCommand) {
        // 简单动作：直接发 action.motor，MotorEffector 会立即转发给 Platform 执行
        Spine.publish<ActionMotorPayload>({
          type: 'action.motor',
          priority: 'HIGH',
          source: 'brain',
          payload: { command: motorCommand, speed, duration },
          summary: `电机指令：${motorCommand}${speed != null ? ` 速度${speed}%` : ''}${duration != null ? ` 持续${duration}s` : ''}`,
        })

        return {
          success: true,
          mode: 'motor',
          command: motorCommand,
          speed: speed ?? '默认(60)',
          duration: duration ?? '持续到下一条指令',
          message: `已执行：${destination}${duration ? `，持续 ${duration} 秒` : ''}`,
        }
      } else {
        // 房间导航：记录意图，等地图模块就绪后规划路径
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
