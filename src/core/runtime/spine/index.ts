import { randomUUID } from 'crypto'
import {
  PRIORITY_RANK,
  type SpineEvent,
  type SpineEventType,
  type EventHandler,
  type Unsubscribe,
  type MemoryEntry,
  type EventPriority,
} from './types'

export * from './types'

// ─── 配置 ──────────────────────────────────────────────────────────────────

const WORKING_MEMORY_WINDOW_MS = 10 * 60 * 1000  // 保留最近 10 分钟
const WORKING_MEMORY_MAX_SIZE = 300               // 条目上限，防止无限增长

// ─── Spine 核心类 ─────────────────────────────────────────────────────────────

class SpineClass {
  private subscribers = new Map<SpineEventType, Set<EventHandler>>()
  private workingMemory: MemoryEntry[] = []

  /**
   * 发布事件到 Spine。
   * 发布方提供 summary 作为工作记忆的人类可读摘要，供 LLM 消费。
   */
  publish<T>(
    event: Omit<SpineEvent<T>, 'id' | 'timestamp'> & { summary: string }
  ): SpineEvent<T> {
    const { summary, ...rest } = event
    const fullEvent: SpineEvent<T> = {
      id: randomUUID(),
      timestamp: Date.now(),
      ...rest,
    }

    this.recordMemory(fullEvent, summary)
    this.dispatch(fullEvent)

    return fullEvent
  }

  /**
   * 订阅一个或多个事件类型。
   * 返回取消订阅函数。
   */
  subscribe<T>(types: SpineEventType[], handler: EventHandler<T>): Unsubscribe {
    for (const type of types) {
      if (!this.subscribers.has(type)) {
        this.subscribers.set(type, new Set())
      }
      this.subscribers.get(type)!.add(handler as EventHandler)
    }

    return () => {
      for (const type of types) {
        this.subscribers.get(type)?.delete(handler as EventHandler)
      }
    }
  }

  /**
   * 获取工作记忆（按时间窗口过滤）。
   * Brain 调用时传入，作为 LLM 上下文的一部分。
   */
  getWorkingMemory(windowMs = WORKING_MEMORY_WINDOW_MS): MemoryEntry[] {
    const cutoff = Date.now() - windowMs
    return this.workingMemory.filter((e) => e.timestamp >= cutoff)
  }

  /**
   * 将工作记忆格式化为 LLM 可直接阅读的文本块。
   */
  formatMemoryForLLM(windowMs = WORKING_MEMORY_WINDOW_MS): string {
    const entries = this.getWorkingMemory(windowMs)
    if (entries.length === 0) return '（近期无感知记录）'

    return entries
      .map((e) => {
        const time = new Date(e.timestamp).toLocaleTimeString('zh-CN', { hour12: false })
        const priorityTag = e.priority === 'CRITICAL' || e.priority === 'HIGH'
          ? ` [${e.priority}]`
          : ''
        return `${time}${priorityTag}  ${e.summary}`
      })
      .join('\n')
  }

  /**
   * 获取当前订阅者统计（用于调试/观测）。
   */
  getStats(): Record<string, number> {
    const stats: Record<string, number> = {}
    this.subscribers.forEach((handlers, type) => {
      if (handlers.size > 0) stats[type] = handlers.size
    })
    return stats
  }

  // ─── 私有方法 ──────────────────────────────────────────────────────────────

  private recordMemory(event: SpineEvent, summary: string): void {
    const entry: MemoryEntry = {
      timestamp: event.timestamp,
      type: event.type,
      priority: event.priority,
      source: event.source,
      summary,
    }

    this.workingMemory.push(entry)

    // 清理超时条目
    const cutoff = Date.now() - WORKING_MEMORY_WINDOW_MS
    while (this.workingMemory.length > 0 && this.workingMemory[0].timestamp < cutoff) {
      this.workingMemory.shift()
    }

    // 兜底上限
    if (this.workingMemory.length > WORKING_MEMORY_MAX_SIZE) {
      this.workingMemory.splice(0, this.workingMemory.length - WORKING_MEMORY_MAX_SIZE)
    }
  }

  private dispatch(event: SpineEvent): void {
    const handlers = this.subscribers.get(event.type)
    if (!handlers || handlers.size === 0) return

    // 按优先级决定是否同步执行（CRITICAL 同步，其余异步不阻塞发布方）
    const isCritical = PRIORITY_RANK[event.priority as EventPriority] === 0

    handlers.forEach((handler) => {
      if (isCritical) {
        Promise.resolve(handler(event)).catch((err) =>
          console.error(`[Spine] CRITICAL handler error for ${event.type}:`, err)
        )
      } else {
        setImmediate(() => {
          Promise.resolve(handler(event)).catch((err) =>
            console.error(`[Spine] Handler error for ${event.type}:`, err)
          )
        })
      }
    })
  }
}

// ─── 单例（Next.js 热重载安全）────────────────────────────────────────────────

declare global {
  // eslint-disable-next-line no-var
  var __spine: SpineClass | undefined
}

export const Spine = globalThis.__spine ?? (globalThis.__spine = new SpineClass())
