/**
 * IdleInitiator — 空闲发起器
 * ============================
 * 职责：
 *   - 追踪上次对话活动时间
 *   - agent 空闲超过阈值时，随机选择一种主动发起策略入队
 *   - 发布 sense.agent.idle 事件（供其他模块感知）
 *
 * 发起策略池（随机选，避免每次都说同一句）：
 *   - 询问型：「主人，有什么需要我做的吗？」
 *   - 信息推送型：让 LLM 根据情景自由发挥
 *   - 撒娇/牢骚型：「我有点无聊，能给我派个任务吗？」
 */

import { ConversationManager } from '../manager'
import { Spine } from '../../../runtime/spine'

const IDLE_THRESHOLD_MS = 20 * 60_000   // 20 分钟无对话则触发
const CHECK_INTERVAL_MS = 60_000         // 每分钟检查一次
const COOLDOWN_MS = 30 * 60_000         // 主动发起后冷却 30 分钟

// 主动发起的触发文案池（直接说的内容）
const IDLE_LINES = [
  '主人，我有点无聊，有什么事要我做吗？',
  '最近家里挺安静的，需要我帮你做什么吗？',
  '我在这儿待着，你有什么吩咐吗？',
  '好无聊啊，要不要我去巡检一下各个房间？',
  '主人，你在吗？有什么需要帮忙的吗？',
]

// 触发 LLM 自由发挥的情景描述（不直接说固定文案，而是给 LLM 一个场景让它自然表达）
const IDLE_TRIGGER_NOTES = [
  '你已经空闲了很长时间，家里没什么动静。主动找主人聊聊天，可以问问有没有事要做，也可以随口说点什么。',
  '家里很安静，你有些无聊。可以主动跟主人说话，表达一下你的状态或者询问有没有任务。',
  '距离上次对话已经过去了很长时间，主动联系一下主人，自然地开启对话。',
]

let lastActivityMs = Date.now()
let lastInitiateMs = 0
let checkTimer: NodeJS.Timeout | null = null

/** 每次对话活动时调用，重置空闲计时器 */
export function resetIdleTimer(): void {
  lastActivityMs = Date.now()
}

export function startIdleInitiator(): void {
  checkTimer = setInterval(() => {
    const now = Date.now()
    const idleMs = now - lastActivityMs
    const cooldownOk = now - lastInitiateMs > COOLDOWN_MS

    if (idleMs < IDLE_THRESHOLD_MS || !cooldownOk) return

    // 只在 agent 空闲状态下发起，不打扰正在进行的对话
    if (ConversationManager.getState() !== 'IDLE') return

    lastInitiateMs = now

    Spine.publish({
      type: 'sense.agent.idle',
      priority: 'LOW',
      source: 'idle.initiator',
      payload: { idle_since_ms: idleMs },
      summary: `agent 已空闲 ${Math.round(idleMs / 60_000)} 分钟，准备主动发起对话`,
    })

    // 随机策略：50% 概率直接说固定文案，50% 概率让 LLM 自由发挥
    const useLLM = Math.random() < 0.5

    if (useLLM) {
      const triggerNote = IDLE_TRIGGER_NOTES[Math.floor(Math.random() * IDLE_TRIGGER_NOTES.length)]
      // 无 content → manager 会让 LLM 根据 triggerNote 生成内容
      // 这里用一个空的 user turn + triggerNote 来触发 LLM
      ConversationManager.enqueue({
        mode: 'active',
        priority: 3,
        source: 'idle.initiator',
        label: 'idle.chat',
        content: undefined,
        triggerNote,
        expiresAt: now + 10 * 60_000,  // 10 分钟内未处理则过期
      })
    } else {
      const line = IDLE_LINES[Math.floor(Math.random() * IDLE_LINES.length)]
      ConversationManager.enqueue({
        mode: 'active',
        priority: 3,
        source: 'idle.initiator',
        label: 'idle.chat',
        content: line,
        expiresAt: now + 10 * 60_000,
      })
    }
  }, CHECK_INTERVAL_MS)

  console.log(`[IdleInitiator] 已启动，空闲阈值 ${IDLE_THRESHOLD_MS / 60_000} 分钟`)
}

export function stopIdleInitiator(): void {
  if (checkTimer) { clearInterval(checkTimer); checkTimer = null }
}
