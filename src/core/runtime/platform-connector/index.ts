/**
 * PlatformConnector — Bridge ↔ Spine 双向适配器
 * =============================================
 * 职责：
 *   - 作为 WebSocket 客户端连接 Bridge（Python FastAPI /ws）
 *   - Bridge → Node.js：将硬件事件翻译为 SpineEvent 并 publish 到 Spine
 *   - Node.js → Bridge：订阅 Spine 的 action.* 事件，转发给 Bridge 执行
 *
 * 单例，通过 PlatformConnector.start() 启动，自动重连。
 *
 * Chat 模式变更（蓝牙外放）：
 *   - 移除了 sense.audio.speech_start / speech_end 映射（录音由手机端负责）
 *   - 保留 sense.audio.speak_end（TTS 播完通知，供 ConversationManager 使用）
 *   - 保留 sense.power.low_battery（低电量告警）
 */

import WebSocket from 'ws'
import { Spine } from '../spine'
import type { SpineEventType, EventPriority } from '../spine'

const PLATFORM_WS_URL = process.env.PLATFORM_WS_URL ?? 'ws://localhost:8001/ws'
const PLATFORM_URL = process.env.PLATFORM_URL ?? 'http://localhost:8001'
const RECONNECT_DELAY_MS = 3000
const MAX_PENDING_MESSAGES = 50

// ─── Bridge 事件 → Spine 事件 映射表 ────────────────────────────────────────

interface EventMapping {
  type: SpineEventType
  priority: EventPriority
  summary: (payload: Record<string, unknown>) => string
}

const INBOUND_MAP: Record<string, EventMapping> = {
  // TTS 全部句子播放完毕，通知 ConvManager Chat 回复已播完
  'sense.audio.speak_end': {
    type: 'sense.audio.speak_end',
    priority: 'LOW',
    summary: () => 'TTS 蓝牙播放完毕',
  },
  // 低电量告警：由 PowerSensor 节流（最多每 60s 一次）
  'sense.power.low_battery': {
    type: 'sense.system.battery',
    priority: 'HIGH',
    summary: (p) =>
      `⚠️ 低电量告警：${p.battery_pct ?? '?'}%（${p.voltage_v ?? '?'}V）` +
      `，请尽快回充电桩`,
  },
}

// Spine action 事件 → Bridge 直接转发（类型透传）
// 注意：有专属 Effector 的事件不在此列，避免重复转发：
//   action.speak   → AudioEffector 订阅并手动 send()
//   action.navigate → MotorEffector 订阅并处理（当前存根，待激光雷达后实装）
//   action.motor   → MotorEffector 订阅并手动 send()
const OUTBOUND_TYPES: SpineEventType[] = [
  'action.capture',
  'action.patrol',
]

// ─── PlatformConnector ─────────────────────────────────────────────────────────

class PlatformConnectorClass {
  private ws: WebSocket | null = null
  private reconnectTimer: ReturnType<typeof setTimeout> | null = null
  private started = false
  private pendingMessages: Array<{ type: string; payload: unknown }> = []

  get isConnected(): boolean {
    return this.ws?.readyState === WebSocket.OPEN
  }

  start(): void {
    if (this.started) return
    this.started = true
    this._connect()
    this._subscribeOutbound()
  }

  /** 主动向 Bridge 发送消息（供 AudioEffector 等模块使用）。 */
  send(message: { type: string; payload: unknown }): void {
    if (this.ws?.readyState === WebSocket.OPEN) {
      this.ws.send(JSON.stringify(message))
    } else {
      // 安全关键：电机 stop 在断线时不能只依赖重连补发，必须立即走 HTTP 兜底。
      if (this._isMotorStop(message)) {
        void this._sendMotorStopFallback(message.payload)
      }

      // 断线时先缓存 action 消息，重连后补发，避免 speak 丢包导致状态机卡在 SPEAKING。
      if (this._isMotorStop(message)) {
        // stop 放队头，确保重连后第一时间重放（HTTP 兜底失败时仍有二次保险）。
        this.pendingMessages.unshift(message)
        if (this.pendingMessages.length > MAX_PENDING_MESSAGES) {
          this.pendingMessages.pop()
        }
      } else {
        this.pendingMessages.push(message)
        if (this.pendingMessages.length > MAX_PENDING_MESSAGES) {
          this.pendingMessages.shift()
        }
      }

      console.warn(
        `[PlatformConnector] WebSocket 未连接，消息入队：${message.type}（pending=${this.pendingMessages.length}）`
      )
    }
  }

  // ─── 私有方法 ──────────────────────────────────────────────────────────────

  private _connect(): void {
    console.log(`[PlatformConnector] 连接 Bridge WebSocket：${PLATFORM_WS_URL}`)
    this.ws = new WebSocket(PLATFORM_WS_URL)

    this.ws.on('open', () => {
      console.log('[PlatformConnector] ✅ 已连接 Bridge')
      if (this.reconnectTimer) {
        clearTimeout(this.reconnectTimer)
        this.reconnectTimer = null
      }
      this._flushPending()
    })

    this.ws.on('message', (raw: WebSocket.RawData) => {
      try {
        const msg = JSON.parse(raw.toString()) as { type: string; payload: Record<string, unknown> }
        this._handleInbound(msg)
      } catch (e) {
        console.error('[PlatformConnector] 消息解析失败：', e)
      }
    })

    this.ws.on('close', () => {
      console.warn(`[PlatformConnector] 连接断开，${RECONNECT_DELAY_MS / 1000}s 后重连...`)
      this.reconnectTimer = setTimeout(() => this._connect(), RECONNECT_DELAY_MS)
    })

    this.ws.on('error', (err) => {
      console.error('[PlatformConnector] WebSocket 错误：', err.message)
    })
  }

  /** 将 Bridge 上报的硬件事件翻译后 publish 到 Spine。 */
  private _handleInbound(msg: { type: string; payload: Record<string, unknown> }): void {
    const mapping = INBOUND_MAP[msg.type]
    if (!mapping) return

    Spine.publish({
      type: mapping.type,
      priority: mapping.priority,
      source: 'platform',
      payload: msg.payload,
      summary: mapping.summary(msg.payload),
    })
  }

  /** 订阅 Spine 的 action 事件，转发给 Bridge 执行。 */
  private _subscribeOutbound(): void {
    Spine.subscribe(OUTBOUND_TYPES, (event) => {
      this.send({ type: event.type, payload: event.payload })
    })
  }

  private _flushPending(): void {
    if (!this.ws || this.ws.readyState !== WebSocket.OPEN) return
    if (this.pendingMessages.length === 0) return

    const toSend = this.pendingMessages.splice(0, this.pendingMessages.length)
    for (const message of toSend) {
      this.ws.send(JSON.stringify(message))
    }
    console.log(`[PlatformConnector] 已补发 ${toSend.length} 条待发送消息`)
  }

  private _isMotorStop(message: { type: string; payload: unknown }): boolean {
    if (message.type !== 'action.motor') return false
    if (!message.payload || typeof message.payload !== 'object') return false
    const cmd = (message.payload as { command?: unknown }).command
    return cmd === 'stop'
  }

  private async _sendMotorStopFallback(payload: unknown): Promise<void> {
    try {
      const body =
        payload && typeof payload === 'object'
          ? payload
          : { command: 'stop' }

      const res = await fetch(`${PLATFORM_URL}/motor/command`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
        signal: AbortSignal.timeout(1500),
      })

      if (!res.ok) {
        const text = await res.text().catch(() => '')
        console.error(`[PlatformConnector] 断线急停 HTTP 失败：${res.status} ${text}`)
        return
      }
      console.warn('[PlatformConnector] WebSocket 断线，已通过 HTTP 执行紧急 stop')
    } catch (err) {
      console.error('[PlatformConnector] 断线急停 HTTP 异常：', err)
    }
  }
}

declare global {
  // eslint-disable-next-line no-var
  var __platformConnector: PlatformConnectorClass | undefined
}

export const PlatformConnector =
  globalThis.__platformConnector ??
  (globalThis.__platformConnector = new PlatformConnectorClass())
