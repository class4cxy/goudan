/**
 * ConversationContext — 语音对话上下文
 * =======================================
 * 职责：
 *   - 维护一次语音对话会话的短期消息历史（滑动窗口，纯内存）
 *   - 同时将每一轮对话持久化到 DB conversations 表，供历史查看
 *   - 供 brain.ts 组装 LLM messages 数组
 */

import { randomUUID } from 'crypto'
import { queries } from '@/lib/db'

export interface VoiceTurn {
  role: 'user' | 'assistant'
  content: string
  timestamp: number
  emotion?: string  // 仅 user 侧有效
}

const MAX_TURNS = 20  // 滑动窗口：最多保留 20 轮（10 问 10 答）

export class ConversationContext {
  private turns: VoiceTurn[] = []
  private sessionStart: number = Date.now()
  /** 每次 reset() 时生成新 session_id，用于在 DB 中隔离不同通话 */
  private sessionId: string = randomUUID()

  // ─── 写入 ────────────────────────────────────────────────────────────────

  addUser(text: string, emotion?: string): void {
    this.turns.push({ role: 'user', content: text, timestamp: Date.now(), emotion })
    this._trim()
    try {
      queries.insertConversation.run(this.sessionId, 'user', text, null)
    } catch (e) {
      console.error('[ConversationContext] 持久化用户消息失败：', e)
    }
  }

  addAssistant(text: string): void {
    this.turns.push({ role: 'assistant', content: text, timestamp: Date.now() })
    this._trim()
    try {
      queries.insertConversation.run(this.sessionId, 'assistant', text, null)
    } catch (e) {
      console.error('[ConversationContext] 持久化助手消息失败：', e)
    }
  }

  // ─── 读取 ────────────────────────────────────────────────────────────────

  /** 返回 LLM messages 格式的历史（role + content） */
  getMessages(): Array<{ role: 'user' | 'assistant'; content: string }> {
    return this.turns.map((t) => ({ role: t.role, content: t.content }))
  }

  /** 返回最近一次用户发言的情绪，供 LLM system prompt 注入 */
  getLastEmotion(): string {
    const lastUser = [...this.turns].reverse().find((t) => t.role === 'user')
    return lastUser?.emotion ?? 'calm'
  }

  get length(): number {
    return this.turns.length
  }

  /** 本次会话持续时长（ms） */
  get sessionDurationMs(): number {
    return Date.now() - this.sessionStart
  }

  // ─── 会话重置 ─────────────────────────────────────────────────────────────

  /** 唤醒词触发新会话时调用，清空历史并生成新 session */
  reset(): void {
    this.turns = []
    this.sessionStart = Date.now()
    this.sessionId = randomUUID()
  }

  // ─── 私有 ─────────────────────────────────────────────────────────────────

  private _trim(): void {
    while (this.turns.length > MAX_TURNS) {
      this.turns.shift()
    }
  }
}
