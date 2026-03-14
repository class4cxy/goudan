import { tool } from 'ai'
import { z } from 'zod'

const PLATFORM_URL = process.env.ROBOROCK_BRIDGE_URL ?? 'http://localhost:8001'

/**
 * getPowerStatus — 查询机器车电源状态
 *
 * 通过 REST 调用 Platform /power/status，返回 INA219 实时采样数据。
 * Agent 可据此判断是否需要回充或向用户发出低电量提醒。
 */
export const getPowerStatus = tool({
  description:
    '查询机器车当前电源状态，包括剩余电量百分比、是否正在充电、当前电压和功率。' +
    '当用户问"还有多少电"、"需要充电吗"，或需要判断是否回充时调用此工具。',
  inputSchema: z.object({}),
  execute: async () => {
    try {
      const res = await fetch(`${PLATFORM_URL}/power/status`)
      if (!res.ok) {
        return { success: false, error: `Platform 返回 ${res.status}` }
      }
      const data = await res.json() as {
        is_simulation: boolean
        is_running: boolean
        is_low_battery: boolean
        low_battery_pct: number
        latest: {
          voltage_v: number
          current_ma: number
          power_mw: number
          battery_pct: number
          is_charging: boolean
        } | null
      }

      if (data.is_simulation || !data.latest) {
        return {
          success: false,
          error: 'INA219 电源传感器未连接，无法获取电量数据。',
        }
      }

      const { battery_pct, is_charging, voltage_v, current_ma, power_mw } = data.latest
      const status = is_charging ? '充电中' : '放电中（正常运行）'
      const batteryDesc =
        battery_pct >= 80 ? '充足' :
        battery_pct >= 50 ? '正常' :
        battery_pct >= 20 ? '偏低，建议适时回充' :
        '严重不足，请立即回充'

      return {
        success: true,
        battery_pct: Math.round(battery_pct),
        is_charging,
        is_low_battery: data.is_low_battery,
        voltage_v: Math.round(voltage_v * 1000) / 1000,
        current_ma: Math.round(current_ma),
        power_mw: Math.round(power_mw),
        status,
        summary: `当前电量 ${Math.round(battery_pct)}%（${voltage_v.toFixed(2)}V），${status}，状态：${batteryDesc}`,
      }
    } catch (err) {
      return { success: false, error: `请求失败：${String(err)}` }
    }
  },
})
