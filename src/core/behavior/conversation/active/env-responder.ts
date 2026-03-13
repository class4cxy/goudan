/**
 * EnvResponder — 环境声音响应器
 * ================================
 * 职责：
 *   - 订阅 sense.audio.environment（Bridge 侧 YAMNet 分类结果）
 *   - 根据声音类别和置信度决定是否主动发起对话或通报
 *   - alert 类（烟雾/哭声/报警）→ priority 1（HIGH），可打断当前 SPEAKING
 *   - activity 类（门铃/敲门）→ priority 2（MEDIUM）
 *   - ambient 类（背景音） → 记录到 Spine，不主动说话
 *
 * 注意：YAMNet 在 Bridge Python 侧尚未集成，本模块是前置接收端，
 *       待 sense.audio.environment 事件上线后自动生效。
 */

import { ConversationManager } from '../manager'
import { Spine } from '../../../runtime/spine'
import type { SpineEvent, AudioEnvironmentPayload } from '../../../runtime/spine'

const CONFIDENCE_THRESHOLD = 0.7   // 低于此置信度不响应
const COOLDOWN_MS = 3 * 60_000     // 同类声音 3 分钟内只响应一次

const lastTriggered = new Map<string, number>()  // label → timestamp

// 响应规则表
const ENV_RULES: Array<{
  labels: string[]
  priority: 1 | 2
  response: string
  triggerNote: string
}> = [
  {
    labels: ['smoke_detector', 'fire_alarm', 'alarm'],
    priority: 1,
    response: '警告！我检测到疑似烟雾或火警声，请立即确认安全！',
    triggerNote: '检测到疑似烟雾/火警声，需要立即告警',
  },
  {
    labels: ['baby_crying', 'crying', 'infant_cry'],
    priority: 1,
    response: '我听到了哭声，需要我去看看吗？',
    triggerNote: '检测到婴儿/孩子哭声，询问主人是否需要处理',
  },
  {
    labels: ['doorbell', 'door_knock'],
    priority: 2,
    response: '有人按门铃了。',
    triggerNote: '检测到门铃声，通知主人',
  },
  {
    labels: ['dog_bark', 'cat'],
    priority: 2,
    response: '我听到宠物的声音，它们好像在叫。',
    triggerNote: '检测到宠物声音',
  },
  {
    labels: ['glass_breaking', 'shatter'],
    priority: 1,
    response: '我好像听到了玻璃破碎的声音，请确认一下家里是否安全。',
    triggerNote: '检测到疑似玻璃破碎声，可能存在安全风险',
  },
]

export function startEnvResponder(): void {
  Spine.subscribe<AudioEnvironmentPayload>(
    ['sense.audio.environment'],
    (event: SpineEvent<AudioEnvironmentPayload>) => {
      const { label, confidence, category } = event.payload

      if (confidence < CONFIDENCE_THRESHOLD) return
      if (category === 'ambient') return  // 背景音不主动响应

      const rule = ENV_RULES.find((r) => r.labels.includes(label))
      if (!rule) return

      // 冷却检查：同类声音不重复响应
      const now = Date.now()
      const cooldownKey = rule.labels[0]
      const last = lastTriggered.get(cooldownKey) ?? 0
      if (now - last < COOLDOWN_MS) return
      lastTriggered.set(cooldownKey, now)

      console.log(`[EnvResponder] 环境声音命中：${label}（confidence=${confidence.toFixed(2)}）`)

      ConversationManager.enqueue({
        mode: 'active',
        priority: rule.priority,
        source: `env.${label}`,
        label: `env.${cooldownKey}`,
        content: rule.response,
        triggerNote: rule.triggerNote,
        expiresAt: now + 5 * 60_000,
      })
    }
  )

  console.log('[EnvResponder] 已启动，订阅 sense.audio.environment')
}
