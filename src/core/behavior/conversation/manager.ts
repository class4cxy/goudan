/**
 * ConversationManager — 对话状态机 + 优先队列
 * ================================================
 *
 * 状态机：
 *   IDLE → LISTENING → THINKING → SPEAKING → IDLE（循环）
 *
 * 队列规则：
 *   priority 0 (CRITICAL) — 唤醒词，立即中断一切，清空队列
 *   priority 1 (HIGH)     — 环境紧急事件（烟雾/哭声），中断 SPEAKING
 *   priority 2 (MEDIUM)   — 任务执行问题、旁听兴趣，IDLE 时处理
 *   priority 3 (LOW)      — 空闲发牢骚，IDLE 时处理，有过期时间
 *
 * 冲突解决：
 *   - 被动唤醒（priority 0）：无论当前任何状态，立即中断并清空队列
 *   - HIGH 主动：中断 SPEAKING，不中断 LISTENING/THINKING
 *   - MEDIUM/LOW 主动：只在 IDLE 时出队执行
 *   - 队列去重：同 label 的项只保留一个（防止 idle 刷屏）
 *   - 队列上限：MAX_QUEUE_SIZE，超出则丢弃最低优先级项
 */

import { randomUUID } from 'crypto'
import { Spine } from '../../runtime/spine'
import type { SpineEvent, AudioTranscriptPayload, AudioEmotionPayload, AudioKeywordPayload, AudioSpeakEndPayload } from '../../runtime/spine'
import { ConversationContext } from './context'
import { generateVoiceResponse } from '@/core/cognition/brain/conversation'
import { resetIdleTimer } from './active/idle-initiator'

// ─── 类型 ─────────────────────────────────────────────────────────────────────

export interface ConvRequest {
  id: string
  mode: 'passive' | 'active'
  /** 0=CRITICAL 1=HIGH 2=MEDIUM 3=LOW，数字越小优先级越高 */
  priority: 0 | 1 | 2 | 3
  /** 触发来源标识，用于日志和去重 */
  source: string
  /** 去重键：同 label 的活跃项只保留一个 */
  label: string
  /** 主动模式：直接说的文本；被动模式：唤醒句（可能包含完整问题） */
  content?: string
  /** 可选：主动发起时注入 brain 的情景说明 */
  triggerNote?: string
  /** 可选：过期时间戳，超过则静默丢弃 */
  expiresAt?: number
  createdAt: number
}

export type ConvState = 'IDLE' | 'LISTENING' | 'THINKING' | 'SPEAKING'

// ─── 常量 ─────────────────────────────────────────────────────────────────────

const MAX_QUEUE_SIZE = 5
/**
 * TTS 播完后等待用户跟进的倾听时长（重新进入 LISTENING 后的超时）。
 * 正常路径：Platform 发 sense.audio.speak_end → 立即进 LISTENING → 此计时器开始。
 */
const LISTEN_AFTER_SPEAK_MS = 10_000
/**
 * 兜底超时：若 sense.audio.speak_end 迟迟未到（网络抖动/Platform 异常），
 * 30 秒后强制回 IDLE，防止卡死在 SPEAKING 状态。
 */
const RESPONSE_WAIT_FALLBACK_MS = 30_000
/** LISTENING 状态无输入超时 */
const LISTEN_TIMEOUT_MS = 10_000

// ─── ConversationManager ─────────────────────────────────────────────────────

class ConversationManagerClass {
  private state: ConvState = 'IDLE'
  private queue: ConvRequest[] = []
  private context = new ConversationContext()
  private pendingEmotion = 'calm'

  private responseTimer: NodeJS.Timeout | null = null
  private listenTimer: NodeJS.Timeout | null = null

  // ─── 启动（由 conversation/index.ts 调用） ───────────────────────────────

  start(): void {
    // 订阅唤醒词事件（来自 thalamus）
    Spine.subscribe<AudioKeywordPayload>(['sense.audio.keyword'], (e) => {
      this._onWake(e.payload.transcript)
    })

    // 订阅转写文本（LISTENING / SPEAKING 状态时处理）
    Spine.subscribe<AudioTranscriptPayload>(['sense.audio.transcript'], (e) => {
      if (this.state === 'LISTENING') {
        this._clearListenTimer()
        this._startThinking(e.payload.text)
      } else if (this.state === 'SPEAKING') {
        // 用户说话打断 agent
        this._clearTimers()
        this._startThinking(e.payload.text, true)
      }
      // IDLE / THINKING 状态的 transcript 由旁听分析器 (AmbientAnalyzer) 处理
    })

    // 订阅情绪更新，暂存供下次 thinking 使用
    Spine.subscribe<AudioEmotionPayload>(['sense.audio.emotion'], (e) => {
      this.pendingEmotion = e.payload.emotion
    })

    // TTS 全部句子播完 → 立即回 LISTENING，等用户继续说
    Spine.subscribe<AudioSpeakEndPayload>(['sense.audio.speak_end'], () => {
      if (this.state === 'SPEAKING') {
        console.log('[ConvManager] TTS 播完，→ LISTENING（等待用户继续）')
        this._clearResponseTimer()
        this._startListeningAfterSpeak()
      }
    })

    console.log('[ConvManager] 已启动，监听 keyword / transcript / emotion / speak_end')
  }

  // ─── 公共 API ─────────────────────────────────────────────────────────────

  /**
   * 主动入队，由各触发器调用。
   *
   * priority 0 会立即中断当前状态，不走队列。
   * priority 1 会中断 SPEAKING 状态后插队到队列头部。
   * priority 2/3 按优先级插入，IDLE 时自动出队处理。
   */
  enqueue(req: Omit<ConvRequest, 'id' | 'createdAt'>): void {
    const item: ConvRequest = { ...req, id: randomUUID(), createdAt: Date.now() }

    if (item.priority === 0) {
      // CRITICAL：唤醒级，立即执行
      this._interrupt(item)
      return
    }

    if (item.priority === 1 && this.state === 'SPEAKING') {
      // HIGH + 正在说话：中断后插队头部
      this._publishStop()
      this.queue.unshift(item)
      this._toIdle()
      return
    }

    // 去重：同 label 跳过
    if (this.queue.some((q) => q.label === item.label)) {
      console.log(`[ConvManager] 队列去重，跳过：${item.label}`)
      return
    }

    // 按优先级插入（稳定排序）
    const insertIdx = this.queue.findIndex((q) => q.priority > item.priority)
    if (insertIdx === -1) {
      this.queue.push(item)
    } else {
      this.queue.splice(insertIdx, 0, item)
    }

    // 超出容量：丢弃尾部最低优先级项
    while (this.queue.length > MAX_QUEUE_SIZE) {
      const dropped = this.queue.pop()!
      console.log(`[ConvManager] 队列已满，丢弃：${dropped.label}`)
    }

    console.log(`[ConvManager] 入队：${item.label}（p=${item.priority}），队列长度=${this.queue.length}`)

    if (this.state === 'IDLE') {
      this._processQueue()
    }
  }

  /**
   * 快捷接口：让 agent 主动说一句话（供 task-narrator 调用）。
   * label 默认用时间戳，不去重（每条 narrate 都是独立消息）。
   */
  narrate(text: string, opts: { priority?: 2 | 3; label?: string; triggerNote?: string } = {}): void {
    this.enqueue({
      mode: 'active',
      priority: opts.priority ?? 2,
      source: 'task.narrate',
      label: opts.label ?? `narrate.${Date.now()}`,
      content: text,
      triggerNote: opts.triggerNote,
    })
  }

  getState(): ConvState {
    return this.state
  }

  // ─── 私有：唤醒处理 ───────────────────────────────────────────────────────

  private _onWake(transcript: string): void {
    resetIdleTimer()
    console.log(`[ConvManager] 唤醒！transcript="${transcript.slice(0, 40)}"`)
    this._interrupt({
      id: randomUUID(),
      mode: 'passive',
      priority: 0,
      source: 'wake',
      label: 'wake',
      content: transcript,
      createdAt: Date.now(),
    })
  }

  private _interrupt(req: ConvRequest): void {
    this._clearTimers()
    this.queue = []
    this.context.reset()
    this.pendingEmotion = 'calm'

    if (this.state === 'SPEAKING') {
      // 打断当前 TTS，先说一句应答让用户感知到
      Spine.publish({
        type: 'action.speak',
        priority: 'HIGH',
        source: 'conversation.manager',
        payload: { text: '嗯？', interrupt_current: true },
        summary: '唤醒打断当前播放，发出应答',
      })
    }

    this._startListening(req.content)
  }

  // ─── 私有：状态转换 ───────────────────────────────────────────────────────

  private _startListening(initialTranscript?: string): void {
    this.state = 'LISTENING'
    console.log('[ConvManager] → LISTENING')

    // 如果唤醒句里已经包含完整问题（去掉唤醒词后有实质内容），直接进 THINKING
    if (initialTranscript) {
      const query = stripWakeWords(initialTranscript)
      if (query.length > 2) {
        this._startThinking(query)
        return
      }
    }

    // 等待用户说话，超时回 IDLE
    this.listenTimer = setTimeout(() => {
      if (this.state === 'LISTENING') {
        console.log('[ConvManager] LISTEN 超时，回到 IDLE')
        this._toIdle()
      }
    }, LISTEN_TIMEOUT_MS)
  }

  private _startThinking(userText: string, interruptCurrent = false): void {
    resetIdleTimer()
    this.state = 'THINKING'
    console.log(`[ConvManager] → THINKING，用户：${userText.slice(0, 40)}`)

    const emotion = this.pendingEmotion
    this.pendingEmotion = 'calm'
    this.context.addUser(userText, emotion)

    let sentenceCount = 0
    generateVoiceResponse(this.context, (sentence, isFirst) => {
      this.state = 'SPEAKING'
      sentenceCount++
      Spine.publish({
        type: 'action.speak',
        priority: 'MEDIUM',
        source: 'conversation.manager',
        payload: { text: sentence, interrupt_current: isFirst && interruptCurrent },
        summary: `语音回复第 ${sentenceCount} 句："${sentence.slice(0, 40)}"`,
      })
    }).then((fullText) => {
      this.context.addAssistant(fullText)
      if (this.state === 'SPEAKING') {
        this._startResponseWait()
      }
    }).catch((err) => {
      console.error('[ConvManager] LLM 出错：', err)
      this._toIdle()
    })
  }

  private _speakActive(req: ConvRequest): void {
    if (!req.content) {
      console.warn(`[ConvManager] _speakActive called with empty content, source=${req.source}, skipping`)
      this._toIdle()
      return
    }

    this.state = 'SPEAKING'
    console.log(`[ConvManager] → SPEAKING（主动），source=${req.source}`)
    this.context.addAssistant(req.content)

    // priority <= 1（HIGH）时需要打断当前正在播放的 TTS
    const shouldInterrupt = req.priority <= 1

    Spine.publish({
      type: 'action.speak',
      priority: 'MEDIUM',
      source: 'conversation.manager',
      payload: { text: req.content, interrupt_current: shouldInterrupt },
      summary: `主动发言（${req.source}）："${req.content.slice(0, 40)}"`,
    })

    this._startResponseWait()
  }

  private _speakActiveLLM(req: ConvRequest): void {
    this.state = 'THINKING'
    console.log(`[ConvManager] → THINKING（主动 LLM），source=${req.source}`)

    let sentenceCount = 0
    generateVoiceResponse(this.context, (sentence, isFirst) => {
      this.state = 'SPEAKING'
      sentenceCount++
      Spine.publish({
        type: 'action.speak',
        priority: 'MEDIUM',
        source: 'conversation.manager',
        payload: { text: sentence, interrupt_current: false },
        summary: `主动发言 LLM 第 ${sentenceCount} 句（${req.source}）："${sentence.slice(0, 40)}"`,
      })
    }, req.triggerNote).then((fullText) => {
      if (fullText) this.context.addAssistant(fullText)
      if (this.state === 'SPEAKING') {
        this._startResponseWait()
      }
    }).catch((err) => {
      console.error('[ConvManager] 主动 LLM 出错：', err)
      this._toIdle()
    })
  }

  /** TTS 播完后进入的倾听窗口：用户可直接说话，无需再说唤醒词。 */
  private _startListeningAfterSpeak(): void {
    this.state = 'LISTENING'
    console.log('[ConvManager] → LISTENING（TTS 后倾听）')
    this.listenTimer = setTimeout(() => {
      if (this.state === 'LISTENING') {
        console.log('[ConvManager] 等待用户跟进超时，回到 IDLE')
        this._toIdle()
      }
    }, LISTEN_AFTER_SPEAK_MS)
  }

  /**
   * 兜底超时：若 Platform 未能及时发 speak_end，30 秒后强制回 IDLE。
   * 正常情况下会被 speak_end 提前取消。
   */
  private _startResponseWait(): void {
    this.responseTimer = setTimeout(() => {
      if (this.state === 'SPEAKING') {
        console.log('[ConvManager] 兜底超时（speak_end 未到），回到 IDLE')
        this._toIdle()
      }
    }, RESPONSE_WAIT_FALLBACK_MS)
  }

  private _toIdle(): void {
    this.state = 'IDLE'
    this._clearTimers()
    console.log('[ConvManager] → IDLE')
    this._processQueue()
  }

  private _processQueue(): void {
    // 清除已过期项
    const now = Date.now()
    const before = this.queue.length
    this.queue = this.queue.filter((q) => !q.expiresAt || q.expiresAt > now)
    if (this.queue.length < before) {
      console.log(`[ConvManager] 清除 ${before - this.queue.length} 个过期队列项`)
    }

    if (this.queue.length === 0) return

    const next = this.queue.shift()!
    console.log(`[ConvManager] 出队：${next.label}（mode=${next.mode}，p=${next.priority}）`)

    if (next.mode === 'passive') {
      this._startListening(next.content)
    } else if (next.content) {
      this._speakActive(next)
    } else {
      this._speakActiveLLM(next)
    }
  }

  // ─── 私有：辅助 ──────────────────────────────────────────────────────────

  /** 打断信号由即将出队的新内容携带 interrupt_current=true 实现，此处无需额外操作。 */
  private _publishStop(): void {}

  private _clearTimers(): void {
    this._clearListenTimer()
    this._clearResponseTimer()
  }

  private _clearListenTimer(): void {
    if (this.listenTimer) { clearTimeout(this.listenTimer); this.listenTimer = null }
  }

  private _clearResponseTimer(): void {
    if (this.responseTimer) { clearTimeout(this.responseTimer); this.responseTimer = null }
  }
}

// ─── 工具函数 ─────────────────────────────────────────────────────────────────

/** 从唤醒句中去掉唤醒词，提取实质问题内容 */
function stripWakeWords(text: string): string {
  const words = (process.env.WAKE_WORDS ?? 'Aria,小豆,狗蛋,aria').split(',').map((w) => w.trim())
  let result = text
  for (const word of words) {
    result = result.replace(new RegExp(word, 'gi'), '')
  }
  return result.replace(/^[\s，,、。！？]+/, '').trim()
}

// ─── 单例（Next.js 热重载安全） ──────────────────────────────────────────────

declare global {
  // eslint-disable-next-line no-var
  var __conversationManager: ConversationManagerClass | undefined
}

export const ConversationManager =
  globalThis.__conversationManager ??
  (globalThis.__conversationManager = new ConversationManagerClass())
