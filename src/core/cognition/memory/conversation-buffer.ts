import { generateText } from 'ai'
import { randomUUID } from 'crypto'
import type { UIMessage } from 'ai'
import { AGENT_MODEL } from '@/core/cognition/tools'
import { queries, type ConversationChunk } from '@/lib/db'

// ─── 配置 ──────────────────────────────────────────────────────────────────

/** rawTail 保留的最大消息数，超出部分触发压缩 */
export const RAW_TAIL_SIZE = 20

/** 每次从 rawTail 中刷出去的批量大小 */
const FLUSH_BATCH_SIZE = 10

/** 同一 level 累积多少个 chunk 时触发向上合并 */
const MERGE_THRESHOLD = 5

/** 最高压缩代数（超过后不再合并） */
const MAX_LEVEL = 3

/**
 * 历史摘要注入到 system prompt 的总 token 预算。
 * 线性权重分配：越新的 chunk 得到越多 token。
 * 粗估：1 token ≈ 1.5 个中文字符。
 */
const HISTORY_TOKEN_BUDGET = 1500

// ─── 压缩 Prompt ───────────────────────────────────────────────────────────

const COMPRESS_PROMPT = `你是对话历史压缩助手。请将输入的对话内容压缩为一段简洁的中文摘要。

重点保留：
- 用户明确表达的偏好和要求
- 已安排或取消的任务
- 重要的事实和决定
- 对话的情绪基调
- 未解决的问题或待完成的事项

忽略：闲聊寒暄、重复确认、工具调用的执行细节。

请直接输出摘要文本，不加任何前缀或解释。`

// ─── ConversationBuffer ────────────────────────────────────────────────────

/**
 * 统一的对话历史压缩与上下文组装器。
 *
 * 设计原则：
 * - 渠道无关：不感知来自 Web/Voice/微信，只操作 threadId
 * - 不修改 thread_messages 存储（前端展示仍用完整历史）
 * - chunks 表记录已压缩的"旧历史"，rawTail = 未被任何 chunk 覆盖的消息
 * - 上下文组装时对 chunks 应用线性时间权重衰减
 */
export class ConversationBuffer {
  private threadId: string

  constructor(threadId: string) {
    this.threadId = threadId
  }

  // ─── 公开 API ─────────────────────────────────────────────────────────────

  /**
   * 返回当前 thread 的所有 chunks（同步，按时间正序）。
   */
  getChunks(): ConversationChunk[] {
    return queries.getThreadChunks.all(this.threadId) as ConversationChunk[]
  }

  /**
   * 从完整消息列表中截取 rawTail（chunks 未覆盖的部分）。
   * 如果没有任何 chunk，rawTail = 全部消息。
   */
  getRawTail(messages: UIMessage[]): UIMessage[] {
    const covered = this.coveredCount()
    return messages.slice(covered)
  }

  /**
   * 将 chunks 历史组装为可注入 system prompt 的字符串。
   *
   * 线性权重分配：
   *   第 i 个 chunk（从旧到新，i 从 1 开始）权重 = i
   *   总权重 = n*(n+1)/2
   *   第 i 个 chunk 的 token 预算 = (i / 总权重) × HISTORY_TOKEN_BUDGET
   *
   * 越新的 chunk 得到越多 token，越旧的 chunk 被截断得越短。
   */
  assembleHistoryContext(): string {
    const chunks = this.getChunks()
    if (chunks.length === 0) return ''

    const n = chunks.length
    const totalWeight = (n * (n + 1)) / 2
    const lines: string[] = ['## 历史对话摘要（从旧到新）']

    chunks.forEach((chunk, idx) => {
      const weight = idx + 1
      const tokenBudget = Math.floor((weight / totalWeight) * HISTORY_TOKEN_BUDGET)
      const maxChars = Math.floor(tokenBudget * 1.5)

      const dateStr = new Date(chunk.covers_from).toLocaleDateString('zh-CN')
      const timeFrom = new Date(chunk.covers_from).toLocaleTimeString('zh-CN', {
        hour: '2-digit', minute: '2-digit', hour12: false,
      })
      const timeTo = new Date(chunk.covers_to).toLocaleTimeString('zh-CN', {
        hour: '2-digit', minute: '2-digit', hour12: false,
      })

      const trimmed = chunk.summary.length > maxChars
        ? chunk.summary.slice(0, maxChars) + '…'
        : chunk.summary

      lines.push(`[${dateStr} ${timeFrom}~${timeTo}，共${chunk.message_count}条]\n${trimmed}`)
    })

    return lines.join('\n\n')
  }

  /**
   * 检查是否需要压缩，并异步执行（fire-and-forget 友好，调用方自行 catch）。
   *
   * 触发条件：rawTail 超过 RAW_TAIL_SIZE，且超出部分能凑满一个 FLUSH_BATCH_SIZE。
   * 压缩完成后递归检查是否需要向上合并 chunks。
   */
  async maybeCompress(messages: UIMessage[]): Promise<void> {
    const covered = this.coveredCount()
    const rawTailCount = messages.length - covered

    if (rawTailCount <= RAW_TAIL_SIZE) return

    const overflow = rawTailCount - RAW_TAIL_SIZE
    const fullBatches = Math.floor(overflow / FLUSH_BATCH_SIZE)
    if (fullBatches === 0) return

    for (let i = 0; i < fullBatches; i++) {
      const batchStart = covered + i * FLUSH_BATCH_SIZE
      const batch = messages.slice(batchStart, batchStart + FLUSH_BATCH_SIZE)
      await this.flushToChunk(batch, 1)
    }

    await this.maybeMergeChunks(1)
  }

  // ─── 私有方法 ─────────────────────────────────────────────────────────────

  /** 已被 chunks 覆盖的消息总数（同步）。 */
  private coveredCount(): number {
    const row = queries.sumChunkMessages.get(this.threadId) as { total: number }
    return row.total
  }

  /**
   * 将一批 UIMessage 压缩为一个 level-N chunk，写入 DB。
   */
  private async flushToChunk(messages: UIMessage[], level: number): Promise<void> {
    const text = this.messagesToText(messages)
    if (!text.trim()) return

    const { text: summary } = await generateText({
      model: AGENT_MODEL,
      system: COMPRESS_PROMPT,
      prompt: text,
    })

    const timestamps = messages
      .map(m => {
        const raw = (m as unknown as Record<string, unknown>).createdAt
        if (!raw) return null
        const t = new Date(raw as string).getTime()
        return isNaN(t) ? null : t
      })
      .filter((t): t is number => t !== null)

    const now = Date.now()
    const coversFrom = timestamps.length > 0 ? Math.min(...timestamps) : now
    const coversTo   = timestamps.length > 0 ? Math.max(...timestamps) : now
    const tokenCount = Math.ceil(summary.length / 1.5)

    queries.insertChunk.run(
      randomUUID(),
      this.threadId,
      level,
      messages.length,
      coversFrom,
      coversTo,
      summary,
      tokenCount,
    )
  }

  /**
   * 如果同一 level 有 ≥ MERGE_THRESHOLD 个 chunks，把最旧的一批合并为上一级 chunk。
   * 递归处理更高层级。
   */
  private async maybeMergeChunks(level: number): Promise<void> {
    if (level >= MAX_LEVEL) return

    const chunks = queries.getChunksByLevel.all(this.threadId, level) as ConversationChunk[]
    if (chunks.length < MERGE_THRESHOLD) return

    const toMerge = chunks.slice(0, MERGE_THRESHOLD)
    const mergedInput = toMerge
      .map((c, i) => `【摘要 ${i + 1}】\n${c.summary}`)
      .join('\n\n')

    const { text: summary } = await generateText({
      model: AGENT_MODEL,
      system: COMPRESS_PROMPT,
      prompt: `请将以下 ${MERGE_THRESHOLD} 段历史摘要进一步合并压缩为一段简洁的摘要：\n\n${mergedInput}`,
    })

    const coversFrom    = Math.min(...toMerge.map(c => c.covers_from))
    const coversTo      = Math.max(...toMerge.map(c => c.covers_to))
    const messageCount  = toMerge.reduce((sum, c) => sum + c.message_count, 0)
    const tokenCount    = Math.ceil(summary.length / 1.5)

    for (const chunk of toMerge) {
      queries.deleteChunk.run(chunk.id)
    }

    queries.insertChunk.run(
      randomUUID(),
      this.threadId,
      level + 1,
      messageCount,
      coversFrom,
      coversTo,
      summary,
      tokenCount,
    )

    await this.maybeMergeChunks(level + 1)
  }

  /**
   * 将 UIMessage[] 转为纯文本，供压缩 LLM 消费。
   * 只保留 user / assistant 的文字和工具调用摘要，忽略 step-start 等元数据。
   */
  private messagesToText(messages: UIMessage[]): string {
    return messages
      .map(msg => {
        if (msg.role !== 'user' && msg.role !== 'assistant') return null

        const role = msg.role === 'user' ? '用户' : 'Aria'
        const parts = (msg.parts ?? []) as Array<Record<string, unknown>>
        const segments: string[] = []

        for (const part of parts) {
          if (part['type'] === 'text' && typeof part['text'] === 'string') {
            segments.push(part['text'])
          } else if (
            part['type'] === 'dynamic-tool' &&
            typeof part['toolName'] === 'string' &&
            part['state'] === 'output-available'
          ) {
            segments.push(`[调用了工具：${part['toolName']}]`)
          }
        }

        const text = segments.join('').trim()
        return text ? `${role}：${text}` : null
      })
      .filter((line): line is string => line !== null)
      .join('\n')
  }
}
