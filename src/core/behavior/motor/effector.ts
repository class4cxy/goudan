/**
 * MotorEffector — 效应器层（运动）
 * ==================================
 * 职责：
 *   - 订阅 Spine 的 action.navigate：记录高层导航意图，待激光雷达+地图模块后实现路径规划
 *   - 订阅 Spine 的 action.motor：将底层电机指令通过 PlatformConnector 转发给 Bridge
 *
 * 设计约定：
 *   - action.navigate 是 Agent 唯一暴露的行走接口（目标导向，不含时长）
 *   - action.motor 是内部执行接口，由本模块或未来的 NavigationThalamus 发布
 *   - 激光雷达安装后，在此模块内接入 NavigationThalamus：
 *       action.navigate → 地图查询路径 → 发布 action.motor 序列
 */

import { Spine } from '../../runtime/spine'
import { PlatformConnector } from '../../runtime/platform-connector'
import type { SpineEvent, ActionNavigatePayload, ActionMotorPayload } from '../../runtime/spine'

export function startMotorEffector(): void {
  // ─── 高层导航意图（当前为存根，激光雷达到来后在此实现路径规划）──────────────

  Spine.subscribe<ActionNavigatePayload>(
    ['action.navigate'],
    (event: SpineEvent<ActionNavigatePayload>) => {
      const { destination, reason } = event.payload
      console.log(
        `[MotorEffector] 导航意图已收到：前往「${destination}」${reason ? `（${reason}）` : ''}`
      )
      // TODO: 激光雷达模块装车后，在此接入 NavigationThalamus：
      //   1. 查询内建地图，计算当前位置 → destination 的路径
      //   2. 将路径分解为 action.motor 事件序列逐步执行
      //   3. 订阅 sense.system.obstacle 事件实现动态避障重规划
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
