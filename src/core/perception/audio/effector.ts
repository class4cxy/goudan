/**
 * AudioEffector — 效应器层（音频）
 * ==================================
 * 职责：
 *   - 订阅 Spine 的 action.speak 事件
 *   - 将播放指令通过 PlatformConnector 转发给 Bridge
 *   - Bridge 负责 TTS + 扬声器播放（Python 侧，见 audio_effector.py）
 *
 * 本模块不直接操作硬件，只做 Spine → Bridge 的指令路由。
 */

import { Spine } from '../../runtime/spine'
import { PlatformConnector } from '../../runtime/platform-connector'
import type { SpineEvent, ActionSpeakPayload } from '../../runtime/spine'

export function startAudioEffector(): void {
  Spine.subscribe<ActionSpeakPayload>(
    ['action.speak'],
    (event: SpineEvent<ActionSpeakPayload>) => {
      const { text, interrupt_current } = event.payload

      if (!text?.trim()) return

      PlatformConnector.send({
        type: 'action.speak',
        payload: {
          text,
          interrupt_current: interrupt_current ?? false,
        },
      })
    }
  )

  console.log('[AudioEffector] 已启动，订阅 action.speak')
}
