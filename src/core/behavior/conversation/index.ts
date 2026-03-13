/**
 * conversation — 交流能力模块入口
 * ==================================
 * 启动顺序：
 *   1. PlatformConnector（WebSocket 连接 Bridge）
 *   2. AudioThalamus（STT + 唤醒词检测 → Spine 事件）
 *   3. AudioEffector（action.speak → Bridge TTS）
 *   4. ConversationManager（状态机，订阅 keyword/transcript/emotion）
 *   5. IdleInitiator（空闲定时器，主动发起）
 *   6. EnvResponder（环境声音响应，主动发起）
 *   7. AmbientAnalyzer（旁听分析，主动插话）
 *
 * 对外暴露：
 *   - startConversationModule()  — 在 instrumentation.ts 中调用一次
 *   - narrate() / narrateIssue() — 供业务层主动让 agent 说话
 */

import { PlatformConnector } from '../../runtime/platform-connector'
import { startAudioThalamus } from '../../perception/audio/thalamus'
import { startAudioEffector } from '../../perception/audio/effector'
import { ConversationManager } from './manager'
import { startIdleInitiator, resetIdleTimer } from './active/idle-initiator'
import { startEnvResponder } from './active/env-responder'
import { startAmbientAnalyzer } from './ambient/analyzer'

export function startConversationModule(): void {
  PlatformConnector.start()
  startAudioThalamus()
  startAudioEffector()
  ConversationManager.start()
  startIdleInitiator()
  startEnvResponder()
  startAmbientAnalyzer()

  console.log('[Conversation] 交流能力模块已启动')
}

// 每次对话活动时重置空闲计时器（ConversationManager 调用）
export { resetIdleTimer }

// 业务层主动发言接口（转发自 task-narrator）
export { narrate, narrateIssue } from './active/task-narrator'

// 导出类型供外部使用
export type { ConvState, ConvRequest } from './manager'
