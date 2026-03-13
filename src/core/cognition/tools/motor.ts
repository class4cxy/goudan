import { tool } from 'ai'
import { z } from 'zod'
import { Spine } from '@/core/runtime/spine'

/**
 * navigateTo — 导航到目标位置
 *
 * Agent 唯一暴露的行走接口。将高层导航意图发布到 Spine，由 MotorEffector 执行。
 * 激光雷达装车 + 内建地图后，MotorEffector 将接入真实路径规划，当前记录意图。
 */
export const navigateTo = tool({
  description:
    '让机器车移动到指定位置或执行指定行走动作。' +
    '支持房间名（如"客厅"、"厨房"）或行走动作（如"向前"、"后退"、"左转"、"右转"、"停止"）。' +
    '导航功能在激光雷达模块安装后生效，当前记录意图并提示用户。',
  inputSchema: z.object({
    destination: z
      .string()
      .describe('目标位置或行走动作，如"客厅"、"向前"、"停止"'),
    reason: z
      .string()
      .optional()
      .describe('导航原因，如"巡检房间"、"跟随主人"，可不填'),
  }),
  execute: async ({ destination, reason }) => {
    try {
      Spine.publish({
        type: 'action.navigate',
        priority: 'MEDIUM',
        source: 'brain',
        payload: { destination, reason },
        summary: `导航指令：前往「${destination}」${reason ? `（${reason}）` : ''}`,
      })

      return {
        success: true,
        message: `导航指令已发出：前往「${destination}」。当前处于激光雷达安装前阶段，实体导航待地图模块生效后启用。`,
        destination,
      }
    } catch (err) {
      return { success: false, error: String(err) }
    }
  },
})
