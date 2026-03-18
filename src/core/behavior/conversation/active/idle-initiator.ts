/**
 * IdleInitiator — 空闲发起器
 * ============================
 * 职责：
 *   - 追踪上次对话活动时间
 *   - agent 空闲超过阈值时，由 LLM 根据情景自由发挥主动搭话
 *   - 发布 sense.agent.idle 事件（供其他模块感知）
 *
 * 静默时段：
 *   - 23:00 ~ 08:00 为休息时间，禁止主动搭话
 *   - 每天在 08:00 ~ 08:10 之间随机触发一次元气满满的早安问候
 */

import { ConversationManager } from '../manager'
import { Spine } from '../../../runtime/spine'

const IDLE_THRESHOLD_MS = 20 * 60_000   // 20 分钟无对话则触发
const CHECK_INTERVAL_MS = 60_000         // 每分钟检查一次
const COOLDOWN_MS = 30 * 60_000         // 主动发起后冷却 30 分钟

/** 休息时段：[起始小时, 结束小时)，前闭后开，跨午夜需分两段判断 */
const QUIET_HOUR_START = 23
const QUIET_HOUR_END = 8
/** 早安问候在 08:00 ~ 08:MORNING_WINDOW_MINS 之间随机触发 */
const MORNING_WINDOW_MINS = 10

// 触发 LLM 自由发挥的情景描述（空闲搭话）
const IDLE_TRIGGER_NOTES = [
  '你已经空闲了很长时间，家里没什么动静。主动找主人聊聊天，可以问问有没有事要做，也可以随口说点什么。',
  '家里很安静，你有些无聊。可以主动跟主人说话，表达一下你的状态或者询问有没有任务。',
  '距离上次对话已经过去了很长时间，主动联系一下主人，自然地开启对话。',
]

// 触发 LLM 自由发挥的情景描述（早安问候，语气要充满朝气）
const MORNING_TRIGGER_NOTES = [
  '现在是早上八点左右，新的一天刚刚开始。用元气满满、积极阳光的语气向主人道早安，可以顺带问问今天有什么计划或者需要帮什么忙。语气要充满朝气，让主人感受到你的活力。',
  '早上八点了，用充满活力和朝气的语气跟主人打招呼，表达你对新一天的期待，自然地询问有没有任务可以帮忙。',
  '新的一天开始啦，用元气十足的方式跟主人说早安，语气轻快活泼，顺便问问今天有什么安排。',
]

let lastActivityMs = Date.now()
let lastInitiateMs = 0
let checkTimer: NodeJS.Timeout | null = null
/** 记录已发送早安问候的日期字符串（如 "Mon Jan 01 2024"），避免当天重复 */
let lastMorningGreetingDate = ''
/** 当天早安问候的目标触发分钟（0 ~ MORNING_WINDOW_MINS-1 内随机），-1 表示未初始化 */
let morningTargetMinute = -1

function pickNote(pool: string[]): string {
  return pool[Math.floor(Math.random() * pool.length)]
}

/** 判断当前是否处于休息时段（23:00 ~ 08:00） */
function isQuietHour(hour: number): boolean {
  return hour >= QUIET_HOUR_START || hour < QUIET_HOUR_END
}

/** 每次对话活动时调用，重置空闲计时器 */
export function resetIdleTimer(): void {
  lastActivityMs = Date.now()
}

export function startIdleInitiator(): void {
  checkTimer = setInterval(() => {
    const now = Date.now()
    const date = new Date()
    const hour = date.getHours()
    const minute = date.getMinutes()
    const dateStr = date.toDateString()

    // ── 早安问候：每天在 08:00 ~ 08:MORNING_WINDOW_MINS 内随机触发一次 ──
    if (hour === QUIET_HOUR_END && lastMorningGreetingDate !== dateStr) {
      // 进入 8 点窗口后，为当天随机选一个触发分钟
      if (morningTargetMinute === -1) {
        morningTargetMinute = Math.floor(Math.random() * MORNING_WINDOW_MINS)
        console.log(`[IdleInitiator] 今日早安目标分钟：08:0${morningTargetMinute}`)
      }

      if (minute >= morningTargetMinute && ConversationManager.getState() === 'IDLE') {
        lastMorningGreetingDate = dateStr
        morningTargetMinute = -1   // 重置，供明天重新抽签
        lastInitiateMs = now       // 重置冷却，早安后不立刻触发空闲发言

        ConversationManager.enqueue({
          mode: 'active',
          priority: 2,
          source: 'idle.initiator.morning',
          label: 'morning.greeting',
          content: undefined,
          triggerNote: pickNote(MORNING_TRIGGER_NOTES),
          expiresAt: now + 60 * 60_000,   // 1 小时内未处理才过期
        })

        console.log('[IdleInitiator] 早安问候已入队')
      }
      return
    }

    // 离开 8 点窗口后重置目标分钟（防止跨天状态残留）
    if (morningTargetMinute !== -1 && hour !== QUIET_HOUR_END) {
      morningTargetMinute = -1
    }

    // ── 休息时段（23:00 ~ 08:00）：不主动搭话 ──
    if (isQuietHour(hour)) return

    // ── 常规空闲检测 ──
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

    ConversationManager.enqueue({
      mode: 'active',
      priority: 3,
      source: 'idle.initiator',
      label: 'idle.chat',
      content: undefined,
      triggerNote: pickNote(IDLE_TRIGGER_NOTES),
      expiresAt: now + 10 * 60_000,   // 10 分钟内未处理则过期
    })
  }, CHECK_INTERVAL_MS)

  console.log(`[IdleInitiator] 已启动，空闲阈值 ${IDLE_THRESHOLD_MS / 60_000} 分钟，休息时段 ${QUIET_HOUR_START}:00 ~ ${QUIET_HOUR_END}:00`)
}

export function stopIdleInitiator(): void {
  if (checkTimer) { clearInterval(checkTimer); checkTimer = null }
}
