/**
 * MotorEffector — 效应器层（运动）
 * ==================================
 * 职责：
 *   - 订阅 Spine 的 action.navigate：启动 AMCL 定位，解析坐标后调用 Platform 导航接口
 *   - 订阅 Spine 的 action.motor：将底层电机指令通过 PlatformConnector 转发给 Bridge
 *
 * 设计约定：
 *   - action.navigate 是 Agent 唯一暴露的行走接口（目标导向，不含时长）
 *   - destination 可为 "x_mm,y_mm" 坐标字符串，或房间名（房间名需在地图中预存路标）
 *   - action.motor 是内部执行接口，由本模块发布
 */

import { Spine } from '../../runtime/spine'
import { PlatformConnector } from '../../runtime/platform-connector'
import type { SpineEvent, ActionNavigatePayload, ActionMotorPayload } from '../../runtime/spine'

const PLATFORM_URL = process.env.PLATFORM_URL ?? 'http://localhost:8001'
/** AMCL 收敛轮询间隔（ms） */
const LOCALIZE_POLL_MS = 2000
/** AMCL 收敛最大等待时长（ms） */
const LOCALIZE_TIMEOUT_MS = 60_000

/** 启动激光雷达（幂等）。 */
async function _lidarStart(): Promise<void> {
  try {
    await fetch(`${PLATFORM_URL}/lidar/start`, { method: 'POST', signal: AbortSignal.timeout(5000) })
    console.log('[MotorEffector] 激光雷达已启动')
  } catch (e) {
    console.warn('[MotorEffector] 激光雷达启动失败：', e)
  }
}

/**
 * 启动 AMCL 定位并等待收敛（confidence > 0.6）。
 * @returns true = 收敛成功；false = 超时或启动失败
 */
async function _startLocalize(): Promise<boolean> {
  try {
    const res = await fetch(`${PLATFORM_URL}/localize/start`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({}),
      signal: AbortSignal.timeout(10_000),
    })
    if (!res.ok) {
      const text = await res.text()
      console.warn(`[MotorEffector] /localize/start 失败：${res.status} ${text}`)
      return false
    }
    console.log('[MotorEffector] AMCL 定位已启动，等待收敛…')
  } catch (e) {
    console.warn('[MotorEffector] /localize/start 请求异常：', e)
    return false
  }

  // 轮询等待 AMCL 收敛
  const deadline = Date.now() + LOCALIZE_TIMEOUT_MS
  while (Date.now() < deadline) {
    await new Promise(r => setTimeout(r, LOCALIZE_POLL_MS))
    try {
      const res = await fetch(`${PLATFORM_URL}/localize/pose`, { signal: AbortSignal.timeout(3000) })
      if (res.ok) {
        const pose = await res.json() as { confidence?: number; converged?: boolean }
        console.log(`[MotorEffector] AMCL confidence=${pose.confidence?.toFixed(3)}`)
        if (pose.converged || (pose.confidence ?? 0) > 0.6) {
          console.log('[MotorEffector] AMCL 已收敛')
          return true
        }
      }
    } catch { /* 轮询失败继续等待 */ }
  }
  console.warn('[MotorEffector] AMCL 收敛超时')
  return false
}

/**
 * 解析 destination 字符串为坐标。
 * 支持格式：
 *   - "200,300"      → { x_mm: 200, y_mm: 300 }
 *   - "200.5, -300"  → { x_mm: 200.5, y_mm: -300 }
 * 返回 null 表示不是坐标格式（为房间名等语义目标）。
 */
function _parseCoordinates(dest: string): { x_mm: number; y_mm: number } | null {
  const parts = dest.split(',')
  if (parts.length !== 2) return null
  const x = parseFloat(parts[0].trim())
  const y = parseFloat(parts[1].trim())
  if (isNaN(x) || isNaN(y)) return null
  return { x_mm: x, y_mm: y }
}

export function startMotorEffector(): void {
  // ─── 高层导航意图 ─────────────────────────────────────────────────────────

  Spine.subscribe<ActionNavigatePayload>(
    ['action.navigate'],
    (event: SpineEvent<ActionNavigatePayload>) => {
      const { destination, reason } = event.payload
      console.log(
        `[MotorEffector] 导航意图：前往「${destination}」${reason ? `（${reason}）` : ''}`
      )

      void (async () => {
        // 1. 确保激光雷达已启动
        await _lidarStart()

        // 2. 解析目标坐标
        const coords = _parseCoordinates(destination)
        if (!coords) {
          // 房间名等语义目标：目前不支持，需要预先在地图中标注路标坐标
          console.warn(
            `[MotorEffector] 目标「${destination}」不是坐标格式（期望 "x_mm,y_mm"），` +
            '房间名导航需在地图中预存路标，当前无法执行'
          )
          return
        }

        // 3. 启动 AMCL 定位并等待收敛
        const converged = await _startLocalize()
        if (!converged) {
          console.error('[MotorEffector] 定位未收敛，取消导航')
          return
        }

        // 4. 发起导航
        try {
          const res = await fetch(`${PLATFORM_URL}/navigate/to`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ x_mm: coords.x_mm, y_mm: coords.y_mm, label: destination }),
            signal: AbortSignal.timeout(10_000),
          })
          if (res.ok) {
            console.log(`[MotorEffector] 导航已启动：(${coords.x_mm}, ${coords.y_mm})`)
          } else {
            const text = await res.text()
            console.error(`[MotorEffector] /navigate/to 失败：${res.status} ${text}`)
          }
        } catch (e) {
          console.error('[MotorEffector] /navigate/to 请求异常：', e)
        }
      })()
    }
  )

  // ─── 底层电机指令（直接转发给 Bridge 执行）──────────────────────────────────

  Spine.subscribe<ActionMotorPayload>(
    ['action.motor'],
    (event: SpineEvent<ActionMotorPayload>) => {
      const { command, speed, duration } = event.payload
      PlatformConnector.send({
        type: 'action.motor',
        payload: { command, speed, duration },
      })
    }
  )

  console.log('[MotorEffector] 已启动，订阅 action.navigate / action.motor')
}
