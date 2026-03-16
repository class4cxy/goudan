/**
 * slam.ts — SLAM 建图 + 自主探索工具
 * =====================================
 * startExploring   — 启动自主建图（SLAM + 自动避障行走）
 * stopExploring    — 停止探索，返回地图访问地址
 * getMapStatus     — 查询当前地图状态
 */

import { tool } from 'ai'
import { z } from 'zod'
import { Explorer } from '@/core/behavior/motor/explorer'

const PLATFORM_URL = process.env.PLATFORM_URL ?? 'http://localhost:8001'

// ── startExploring ────────────────────────────────────────────────

export const startExploring = tool({
  description:
    '启动自主建图模式：机器车会自动行走并用激光雷达建立房间地图，遇到障碍自动避开。' +
    '建图过程中可随时用 stopExploring 停止。' +
    '需要激光雷达已连接且 breezyslam 已安装。',
  inputSchema: z.object({}),
  execute: async () => {
    try {
      // 先检查激光雷达和 SLAM 是否可用
      const statusRes = await fetch(`${PLATFORM_URL}/slam/status`, {
        signal: AbortSignal.timeout(4000),
      })
      if (!statusRes.ok) throw new Error(`platform 不可达 (${statusRes.status})`)

      const status = (await statusRes.json()) as { available: boolean; is_mapping: boolean }

      if (!status.available) {
        return {
          success: false,
          error: 'breezyslam 未安装，无法建图。请在机器人上运行：pip install breezyslam --break-system-packages',
        }
      }

      if (Explorer.isRunning) {
        return { success: false, error: '探索已在进行中，请先调用 stopExploring 停止' }
      }

      await Explorer.start()
      if (!Explorer.isRunning) {
        return {
          success: false,
          error: '探索启动失败：请检查 /slam/start 与 /ultrasonic/status 是否正常（雷达/超声波/SLAM 状态）',
        }
      }

      return {
        success: true,
        message: '自主建图已启动！机器车正在自动行走建图，遇到障碍会自动避开。用 stopExploring 可随时停止并保存地图。',
      }
    } catch (err) {
      return { success: false, error: String(err) }
    }
  },
})

// ── stopExploring ─────────────────────────────────────────────────

export const stopExploring = tool({
  description: '停止自主建图探索，机器车原地停止，并将当前地图保存到服务器。返回地图信息。',
  inputSchema: z.object({
    save_name: z
      .string()
      .optional()
      .describe('地图文件名（不填则自动生成时间戳名称）'),
  }),
  execute: async ({ save_name }) => {
    try {
      if (!Explorer.isRunning) {
        return { success: false, error: '当前没有进行中的探索任务' }
      }

      await Explorer.stop()

      // 保存地图
      const url = new URL(`${PLATFORM_URL}/slam/save`)
      if (save_name) url.searchParams.set('name', save_name)

      const saveRes = await fetch(url.toString(), {
        method: 'POST',
        signal: AbortSignal.timeout(8000),
      })

      if (!saveRes.ok) {
        return { success: true, message: '探索已停止，但地图保存失败（地图可能为空）' }
      }

      const saved = (await saveRes.json()) as { ok: boolean; name: string; pgm_path: string }
      return {
        success: true,
        message: `探索已停止，地图已保存为「${saved.name}」`,
        map_name: saved.name,
      }
    } catch (err) {
      return { success: false, error: String(err) }
    }
  },
})

// ── getMapImage ───────────────────────────────────────────────────

export const getMapImage = tool({
  description:
    '获取当前 SLAM 建图的实时地图图片。建图进行中或已完成后调用，' +
    '前端组件会自动拉取并渲染地图图片。' +
    '用户问"地图怎么样了"、"让我看看地图"时调用。',
  inputSchema: z.object({}),
  execute: async () => {
    try {
      const res = await fetch(`${PLATFORM_URL}/slam/status`, {
        signal: AbortSignal.timeout(4000),
      })
      if (!res.ok) throw new Error(`platform 不可达 (${res.status})`)
      const status = (await res.json()) as {
        scan_count: number
        is_mapping: boolean
        pose: { x_mm: number; y_mm: number; theta_deg: number }
      }

      if (status.scan_count === 0) {
        return { success: false, error: '地图为空，请先启动建图（startExploring）' }
      }

      // 只返回元数据，图片由前端 Tool UI 自主拉取渲染（不占用对话上下文）
      return {
        success: true,
        scan_count: status.scan_count,
        pose: status.pose,
        exploring: Explorer.isRunning,
        /** 前端用此时间戳作为拉取图片的 cache-buster */
        fetch_ts: Date.now(),
      }
    } catch (err) {
      return { success: false, error: String(err) }
    }
  },
})

// ── getMapStatus ──────────────────────────────────────────────────

export const getMapStatus = tool({
  description: '查询当前 SLAM 建图状态：是否正在建图、已扫描圈数、机器人当前坐标。',
  inputSchema: z.object({}),
  execute: async () => {
    try {
      const res = await fetch(`${PLATFORM_URL}/slam/status`, {
        signal: AbortSignal.timeout(4000),
      })
      if (!res.ok) throw new Error(`HTTP ${res.status}`)
      const data = (await res.json()) as Record<string, unknown>
      return { success: true, ...data, exploring: Explorer.isRunning }
    } catch (err) {
      return { success: false, error: String(err) }
    }
  },
})
