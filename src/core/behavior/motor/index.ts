/**
 * 运动模块入口
 * 按正确顺序启动各层：PlatformConnector → Effector
 *
 * 待激光雷达装车后扩展：PlatformConnector → NavigationThalamus → Effector
 */

import { PlatformConnector } from '../../runtime/platform-connector'
import { startMotorEffector } from './effector'
export { Explorer } from './explorer'

export function startMotorModule(): void {
  PlatformConnector.start()
  startMotorEffector()
  console.log('[Motor] 运动模块已启动（Effector + Explorer）')
}

export { startMotorEffector } from './effector'
