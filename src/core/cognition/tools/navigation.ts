import { tool } from 'ai'
import { z } from 'zod'

const PLATFORM_URL = process.env.PLATFORM_URL ?? 'http://localhost:8001'

/**
 * startLocalize — 启动 AMCL 室内定位
 *
 * 使用已保存的地图，通过激光雷达实时扫描确定机器车当前位置。
 * 可选传入已知起点坐标加速收敛；不传则全局定位（机器车需缓慢移动以帮助收敛）。
 */
export const startLocalize = tool({
  description:
    '启动室内定位（AMCL）。使用已保存的地图和激光雷达确定机器车当前位置。' +
    '可以指定已知起点坐标加速收敛，否则自动全局定位（机器车需缓慢移动）。' +
    '定位收敛后才能执行坐标导航。',
  inputSchema: z.object({
    map_name: z
      .string()
      .optional()
      .describe('地图名称（不含扩展名），不填则自动使用最新保存的地图'),
    x_mm: z.number().optional().describe('已知起点 X 坐标（mm），与 y_mm、theta_deg 同时填写'),
    y_mm: z.number().optional().describe('已知起点 Y 坐标（mm）'),
    theta_deg: z.number().optional().describe('已知起点朝向角度（°，-180~180）'),
  }),
  execute: async ({ map_name, x_mm, y_mm, theta_deg }) => {
    try {
      const body: Record<string, unknown> = {}
      if (map_name) body.map_name = map_name
      if (x_mm !== undefined && y_mm !== undefined && theta_deg !== undefined) {
        body.x_mm = x_mm
        body.y_mm = y_mm
        body.theta_deg = theta_deg
      }
      const res = await fetch(`${PLATFORM_URL}/localize/start`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
        signal: AbortSignal.timeout(15_000),
      })
      if (!res.ok) {
        const text = await res.text()
        return { success: false, error: `HTTP ${res.status}：${text}` }
      }
      const data = await res.json() as Record<string, unknown>
      return {
        success: true,
        map: data.map,
        mode: data.mode,
        message: `定位已启动（${data.mode}），请稍候 AMCL 收敛（confidence > 0.6）后再执行导航。`,
      }
    } catch (err) {
      return { success: false, error: String(err) }
    }
  },
})

/**
 * getLocalizePose — 查询当前 AMCL 定位位姿
 */
export const getLocalizePose = tool({
  description:
    '查询当前 AMCL 定位位姿和收敛置信度。confidence > 0.6 表示定位已收敛，可以执行导航。',
  inputSchema: z.object({}),
  execute: async () => {
    try {
      const res = await fetch(`${PLATFORM_URL}/localize/pose`, {
        signal: AbortSignal.timeout(5000),
      })
      if (!res.ok) {
        const text = await res.text()
        return { success: false, error: `HTTP ${res.status}：${text}` }
      }
      const data = await res.json() as {
        x_mm?: number; y_mm?: number; theta_deg?: number
        confidence?: number; converged?: boolean
      }
      return {
        success: true,
        x_mm: data.x_mm,
        y_mm: data.y_mm,
        theta_deg: data.theta_deg,
        confidence: data.confidence,
        converged: data.converged,
        message: data.converged
          ? `定位已收敛（confidence=${data.confidence?.toFixed(3)}），可以导航。`
          : `定位中（confidence=${data.confidence?.toFixed(3)}），请等待收敛后再导航。`,
      }
    } catch (err) {
      return { success: false, error: String(err) }
    }
  },
})

/**
 * navigateToCoordinates — 导航到地图坐标
 *
 * 需要先调用 startLocalize 并等待 AMCL 收敛。
 * 导航过程可通过 getNavigationStatus 查询进度。
 */
export const navigateToCoordinates = tool({
  description:
    '让机器车自主导航到地图上的指定坐标（mm）。' +
    '前提：已通过 startLocalize 启动定位且 confidence > 0.6。' +
    '导航过程中用 getNavigationStatus 查询进度，用 cancelNavigation 取消。',
  inputSchema: z.object({
    x_mm: z.number().describe('目标 X 坐标（mm，以建图起点为原点）'),
    y_mm: z.number().describe('目标 Y 坐标（mm）'),
    label: z.string().optional().describe('目标描述，如"客厅中心"，仅用于日志'),
  }),
  execute: async ({ x_mm, y_mm, label }) => {
    try {
      const res = await fetch(`${PLATFORM_URL}/navigate/to`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ x_mm, y_mm, label: label ?? '' }),
        signal: AbortSignal.timeout(10_000),
      })
      if (!res.ok) {
        const text = await res.text()
        return { success: false, error: `HTTP ${res.status}：${text}` }
      }
      return {
        success: true,
        x_mm,
        y_mm,
        label,
        message: `导航已启动，目标 (${x_mm}, ${y_mm})${label ? ` "${label}"` : ''}。` +
          '用 getNavigationStatus 查询进度。',
      }
    } catch (err) {
      return { success: false, error: String(err) }
    }
  },
})

/**
 * getNavigationStatus — 查询导航进度
 */
export const getNavigationStatus = tool({
  description: '查询当前导航任务状态：idle/localizing/navigating/arrived/failed/cancelled。',
  inputSchema: z.object({}),
  execute: async () => {
    try {
      const res = await fetch(`${PLATFORM_URL}/navigate/status`, {
        signal: AbortSignal.timeout(5000),
      })
      if (!res.ok) {
        const text = await res.text()
        return { success: false, error: `HTTP ${res.status}：${text}` }
      }
      const data = await res.json() as {
        status?: string; goal?: { x_mm: number; y_mm: number; label: string } | null
        confidence?: number; path_remaining?: number; dist_to_goal_mm?: number | null
      }
      const statusMsg: Record<string, string> = {
        idle:       '空闲',
        localizing: '定位中（等待 AMCL 收敛）',
        navigating: '导航中',
        arrived:    '已到达目标',
        failed:     '导航失败',
        cancelled:  '已取消',
      }
      return {
        success: true,
        ...data,
        status_label: statusMsg[data.status ?? ''] ?? data.status,
      }
    } catch (err) {
      return { success: false, error: String(err) }
    }
  },
})

/**
 * cancelNavigation — 取消当前导航
 */
export const cancelNavigation = tool({
  description: '取消当前正在执行的导航任务，机器车立即停止。',
  inputSchema: z.object({}),
  execute: async () => {
    try {
      const res = await fetch(`${PLATFORM_URL}/navigate/cancel`, {
        method: 'POST',
        signal: AbortSignal.timeout(5000),
      })
      if (!res.ok) {
        const text = await res.text()
        return { success: false, error: `HTTP ${res.status}：${text}` }
      }
      return { success: true, message: '导航已取消，机器车已停止。' }
    } catch (err) {
      return { success: false, error: String(err) }
    }
  },
})
