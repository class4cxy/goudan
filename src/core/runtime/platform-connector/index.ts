/**
 * PlatformConnector — Bridge ↔ Spine 双向适配器
 * =============================================
 * 职责：
 *   - 作为 WebSocket 客户端连接 Bridge（Python FastAPI /ws）
 *   - Bridge → Node.js：将硬件事件翻译为 SpineEvent 并 publish 到 Spine
 *   - Node.js → Bridge：订阅 Spine 的 action.* 事件，转发给 Bridge 执行
 *
 * 单例，通过 PlatformConnector.start() 启动，自动重连。
 */

import WebSocket from 'ws'
import { Spine } from '../spine'
import type { SpineEventType, EventPriority } from '../spine'

const PLATFORM_WS_URL = process.env.PLATFORM_WS_URL ?? 'ws://localhost:8001/ws'
const RECONNECT_DELAY_MS = 3000
const MAX_PENDING_MESSAGES = 50

// ─── Bridge 事件 → Spine 事件 映射表 ────────────────────────────────────────

interface EventMapping {
  type: SpineEventType
  priority: EventPriority
  summary: (payload: Record<string, unknown>) => string
}

const INBOUND_MAP: Record<string, EventMapping> = {
  'sense.audio.speech_start': {
    type: 'sense.audio.speech_start',
    priority: 'LOW',
    summary: () => '检测到有人开始说话',
  },
  'sense.audio.speech_end': {
    type: 'sense.audio.speech_end',
    priority: 'MEDIUM',
    summary: (p) => `说话结束，时长 ${p.duration_ms ?? '?'}ms，等待丘脑转写`,
  },
  // TTS 全部句子播放完毕，通知 ConvManager 重新进入 LISTENING
  'sense.audio.speak_end': {
    type: 'sense.audio.speak_end',
    priority: 'LOW',
    summary: () => 'TTS 播放完毕',
  },
  // 低电量告警：充电中不触发，由 PowerSensor 节流（最多每 60s 一次）
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
      // 断线时先缓存 action 消息，重连后补发，避免 speak 丢包导致状态机卡在 SPEAKING。
      this.pendingMessages.push(message)
      if (this.pendingMessages.length > MAX_PENDING_MESSAGES) {
        this.pendingMessages.shift()
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

    if (msg.type === 'sense.audio.speech_end') {
      const trace = msg.payload.trace_id ?? ''
      const dur = msg.payload.duration_ms ?? '?'
      console.log(`[PlatformConnector] ← speech_end  trace=${trace}  时长=${dur}ms → Spine`)
    }

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
}

declare global {
  // eslint-disable-next-line no-var
  var __platformConnector: PlatformConnectorClass | undefined
}

export const PlatformConnector =
  globalThis.__platformConnector ??
  (globalThis.__platformConnector = new PlatformConnectorClass())
