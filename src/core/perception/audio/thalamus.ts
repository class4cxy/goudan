/**
 * AudioThalamus — 丘脑层（音频）
 * ================================
 * 职责：
 *   - 订阅 sense.audio.speech_end（Bridge VAD 切片后的 PCM 块）
 *   - STT：调用 Qwen ASR API 将音频转为文字
 *   - 唤醒词检测：命中则发布 sense.audio.keyword（交给 ConversationManager）
 *   - 非唤醒音频：发布 sense.audio.transcript（交给对话管理器 / 旁听分析器）
 *   - 情绪分析：基于规则并行判断，发布 sense.audio.emotion
 *
 * 这是"原始音频流 → 离散语义事件"的转换边界，不含对话状态逻辑。
 */

import { Spine } from '../../runtime/spine'
import type { SpineEvent, AudioSpeechEndPayload } from '../../runtime/spine'

const ASR_MODEL = 'qwen3-asr-flash'

const MIN_DURATION_MS = 800  // 短于此时长的片段丢弃（环境噪声 / 过短无法识别）

// 本地 STT 端点：优先用 LOCAL_STT_URL，留空时从 PLATFORM_URL 自动派生
const LOCAL_STT_URL: string | null = (() => {
  if (process.env.LOCAL_STT_URL) return process.env.LOCAL_STT_URL
  if (process.env.PLATFORM_URL) return `${process.env.PLATFORM_URL.replace(/\/$/, '')}/stt/transcribe`
  return null
})()

// 唤醒词列表，逗号分隔，支持环境变量覆盖
const WAKE_WORDS: string[] = (process.env.WAKE_WORDS ?? 'Aria,小豆,狗蛋,aria')
  .split(',')
  .map((w) => w.trim())
  .filter(Boolean)

const NOISE_PATTERNS = [
  /^(嗯+|啊+|哦+|唉+|哈+|额+)[。！!？?，,、\s]*$/i,
  /^(是吗|好吧|好的|行吧|ok|okay|all right|alright)[。！!？?，,、\s]*$/i,
]
const STRONG_INTENT_WORDS = ['停止', '暂停', '继续', '开始', '回家', '回去', '前进', '后退', '左转', '右转', '拍照']
const MIN_MEANINGFUL_TEXT_LEN = Number(process.env.MIN_MEANINGFUL_TEXT_LEN ?? 2)

// ─── 情绪关键词规则表 ─────────────────────────────────────────────────────────

const EMOTION_RULES: Array<{
  emotion: string
  priority: 'LOW' | 'MEDIUM' | 'HIGH'
  keywords: string[]
}> = [
  {
    emotion: 'urgent',
    priority: 'HIGH',
    keywords: ['救命', '帮帮我', '快来', '出事了', '不行了', '危险'],
  },
  {
    emotion: 'crying',
    priority: 'HIGH',
    keywords: ['哭', '呜呜', '呜', '伤心', '难过', '委屈'],
  },
  {
    emotion: 'arguing',
    priority: 'MEDIUM',
    keywords: ['你错了', '不对', '凭什么', '滚', '闭嘴', '烦死了', '你别'],
  },
]

// ─── 启动函数 ─────────────────────────────────────────────────────────────────

export function startAudioThalamus(): void {
  Spine.subscribe<AudioSpeechEndPayload>(
    ['sense.audio.speech_end'],
    async (event: SpineEvent<AudioSpeechEndPayload>) => {
      const { audio_b64, sample_rate, duration_ms } = event.payload

      console.log(`[AudioThalamus] ← speech_end  时长=${duration_ms}ms  字节=${Math.round(audio_b64.length * 0.75 / 1024)}KB`)

      if (duration_ms < MIN_DURATION_MS) {
        console.log(`[AudioThalamus] 时长过短（<${MIN_DURATION_MS}ms），丢弃`)
        return
      }

      try {
        console.log(`[AudioThalamus] → STT 请求中...`)
        const sttStart = Date.now()
        const text = await transcribe(audio_b64, sample_rate)
        const sttMs = Date.now() - sttStart
        console.log(`[AudioThalamus] STT 耗时=${sttMs}ms（音频=${duration_ms}ms，实时率=${(duration_ms/sttMs).toFixed(1)}x）`)

        if (!text?.trim()) {
          console.log(`[AudioThalamus] STT 返回空，丢弃`)
          return
        }

        const normalized = normalizeTranscript(text)
        if (!normalized) {
          console.log('[AudioThalamus] STT 文本判定为弱信号/环境噪声，丢弃')
          return
        }

        console.log(`[AudioThalamus] STT 结果："${normalized.slice(0, 60)}${normalized.length > 60 ? '…' : ''}"`)

        // 唤醒词检测：命中则走 sense.audio.keyword 路径，不再发 transcript
        const hitWord = detectWakeWord(normalized)
        if (hitWord) {
          console.log(`[AudioThalamus] 唤醒词命中："${hitWord}"`)
          Spine.publish({
            type: 'sense.audio.keyword',
            priority: 'HIGH',
            source: 'thalamus.audio',
            payload: { keyword: hitWord, transcript: normalized, duration_ms },
            summary: `唤醒词命中："${hitWord}"，原句：${normalized.slice(0, 40)}`,
          })
          return
        }

        // 普通语音：发布转写结果
        Spine.publish({
          type: 'sense.audio.transcript',
          priority: 'MEDIUM',
          source: 'thalamus.audio',
          payload: { text: normalized, duration_ms },
          summary: `转写完成："${normalized.slice(0, 40)}${normalized.length > 40 ? '…' : ''}"`,
        })

        // 并行发布情绪分析
        const emotion = analyzeEmotion(normalized)
        Spine.publish({
          type: 'sense.audio.emotion',
          priority: emotion.priority,
          source: 'thalamus.audio',
          payload: {
            emotion: emotion.emotion,
            confidence: emotion.confidence,
            text_snippet: normalized.slice(0, 60),
          },
          summary: `情绪：${emotion.emotion}（置信度 ${Math.round(emotion.confidence * 100)}%）`,
        })
      } catch (err) {
        console.error('[AudioThalamus] 处理失败：', err)
      }
    }
  )

  console.log(`[AudioThalamus] 已启动，唤醒词：[${WAKE_WORDS.join(', ')}]`)
}

// ─── 唤醒词检测 ───────────────────────────────────────────────────────────────

function detectWakeWord(text: string): string | null {
  for (const word of WAKE_WORDS) {
    if (text.toLowerCase().includes(word.toLowerCase())) {
      return word
    }
  }
  return null
}

function normalizeTranscript(rawText: string): string | null {
  const text = rawText.trim()
  if (!text) return null

  for (const pattern of NOISE_PATTERNS) {
    if (pattern.test(text)) return null
  }

  const compact = text.replace(/[\s，,。！？!?.、；;:："'“”‘’]/g, '')
  if (!compact) return null

  const hasWakeWord = detectWakeWord(text) !== null
  const hasStrongIntent = STRONG_INTENT_WORDS.some((k) => text.includes(k))
  if (!hasWakeWord && !hasStrongIntent && compact.length < MIN_MEANINGFUL_TEXT_LEN) {
    return null
  }

  return text
}

// ─── STT ─────────────────────────────────────────────────────────────────────

/** 单次 PCM 音频允许发往 ASR 的最大字节数（~30s @ 16kHz 16-bit mono = 960KB PCM）。
 *  网关批处理后端对超大音频容易返回 500，裁剪后只保留最近部分。 */
const MAX_PCM_BYTES = 960_000

async function transcribe(audio_b64: string, sampleRate: number): Promise<string> {
  // 超长音频统一截断（约 30s），防止 Whisper 静音幻觉 & 网关超时
  let pcmBuffer = Buffer.from(audio_b64, 'base64')
  if (pcmBuffer.length > MAX_PCM_BYTES) {
    console.log(`[AudioThalamus] 音频过长（${Math.round(pcmBuffer.length / 1024)}KB），截断至 ${Math.round(MAX_PCM_BYTES / 1024)}KB`)
    pcmBuffer = pcmBuffer.subarray(pcmBuffer.length - MAX_PCM_BYTES)
    audio_b64 = pcmBuffer.toString('base64')
  }

  // ── 优先尝试本地 STT ──────────────────────────────────────────────────────
  if (LOCAL_STT_URL) {
    try {
      const t0 = Date.now()
      const text = await transcribeLocal(audio_b64, sampleRate)
      console.log(`[AudioThalamus] 本地 STT 耗时=${Date.now()-t0}ms`)
      return text
    } catch (err) {
      console.warn(`[AudioThalamus] 本地 STT 失败，降级云端：${(err as Error).message}`)
    }
  }

  // ── 云端 ASR（Qwen）———降级路径───────────────────────────────────────────
  const t0 = Date.now()
  const text = await transcribeCloud(audio_b64, sampleRate)
  console.log(`[AudioThalamus] 云端 ASR 耗时=${Date.now()-t0}ms`)
  return text
}

/** 调用本地 platform FastAPI /stt/transcribe。 */
async function transcribeLocal(audio_b64: string, sampleRate: number): Promise<string> {
  const res = await fetch(LOCAL_STT_URL!, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ audio_b64, sample_rate: sampleRate }),
    signal: AbortSignal.timeout(30_000),
  })

  if (res.status === 503) {
    throw new Error('本地 STT 引擎不可用（模型未加载）')
  }
  if (!res.ok) {
    const body = await res.text().catch(() => '')
    throw new Error(`本地 STT 返回错误：status=${res.status}, body=${body.slice(0, 200)}`)
  }

  const data = (await res.json()) as { text?: string }
  return data.text ?? ''
}

/** 调用云端 Qwen ASR API（兼容 OpenAI audio 格式）。 */
async function transcribeCloud(audio_b64: string, sampleRate: number): Promise<string> {
  const speechApiUrl = process.env.SPEECH_API_URL
  const speechApiKey = process.env.SPEECH_API_KEY

  if (!speechApiUrl || !speechApiKey) {
    throw new Error('语音转文字未配置：请设置 SPEECH_API_URL / SPEECH_API_KEY')
  }

  // PCM → WAV，base64 编码后作为 input_audio data URI 传入 chat/completions
  let pcmBuffer = Buffer.from(audio_b64, 'base64')

  // 超长音频裁剪：保留最近的 MAX_PCM_BYTES 字节（避免网关 500 batching 错误）
  if (pcmBuffer.length > MAX_PCM_BYTES) {
    console.log(`[AudioThalamus] 音频过长（${Math.round(pcmBuffer.length / 1024)}KB），裁剪至 ${Math.round(MAX_PCM_BYTES / 1024)}KB`)
    pcmBuffer = pcmBuffer.subarray(pcmBuffer.length - MAX_PCM_BYTES)
  }

  const wavBuffer = pcmToWav(pcmBuffer, sampleRate)
  const dataUri = `data:audio/wav;base64,${wavBuffer.toString('base64')}`

  const body = JSON.stringify({
    model: ASR_MODEL,
    messages: [
      {
        role: 'user',
        content: [{ type: 'input_audio', input_audio: { data: dataUri } }],
      },
    ],
    stream: false,
  })

  // 最多重试 1 次（针对 500 批处理后端瞬时错误）
  let lastErr: Error | null = null
  for (let attempt = 0; attempt < 2; attempt++) {
    if (attempt > 0) {
      await new Promise((r) => setTimeout(r, 1500))
      console.log(`[AudioThalamus] 云端 ASR 重试（第 ${attempt} 次）...`)
    }

    const res = await fetch(speechApiUrl, {
      method: 'POST',
      headers: {
        Authorization: `Bearer ${speechApiKey}`,
        'Content-Type': 'application/json',
      },
      body,
      signal: AbortSignal.timeout(60_000),
    })

    if (res.ok) {
      const data = (await res.json().catch(() => null)) as {
        choices?: Array<{ message?: { content?: unknown } }>
      } | null

      const text =
        typeof data?.choices?.[0]?.message?.content === 'string'
          ? data.choices[0].message.content
          : ''

      return text
    }

    const resBody = await res.text().catch(() => '')
    lastErr = new Error(`云端 ASR 调用失败：status=${res.status}, body=${resBody.slice(0, 200)}`)

    // 只对 5xx 重试，4xx 直接抛出
    if (res.status < 500) break
  }

  throw lastErr!
}

/** 将原始 PCM（16bit mono）封装为标准 WAV，供 Whisper API 识别。 */
function pcmToWav(pcm: Buffer, sampleRate: number): Buffer {
  const numChannels = 1
  const bitsPerSample = 16
  const byteRate = (sampleRate * numChannels * bitsPerSample) / 8
  const blockAlign = (numChannels * bitsPerSample) / 8
  const dataSize = pcm.length
  const wav = Buffer.alloc(44 + dataSize)

  wav.write('RIFF', 0)
  wav.writeUInt32LE(36 + dataSize, 4)
  wav.write('WAVE', 8)
  wav.write('fmt ', 12)
  wav.writeUInt32LE(16, 16)
  wav.writeUInt16LE(1, 20)
  wav.writeUInt16LE(numChannels, 22)
  wav.writeUInt32LE(sampleRate, 24)
  wav.writeUInt32LE(byteRate, 28)
  wav.writeUInt16LE(blockAlign, 32)
  wav.writeUInt16LE(bitsPerSample, 34)
  wav.write('data', 36)
  wav.writeUInt32LE(dataSize, 40)
  pcm.copy(wav, 44)

  return wav
}

// ─── 情绪分析 ─────────────────────────────────────────────────────────────────

function analyzeEmotion(text: string): {
  emotion: string
  confidence: number
  priority: 'LOW' | 'MEDIUM' | 'HIGH'
} {
  for (const rule of EMOTION_RULES) {
    const matched = rule.keywords.filter((k) => text.includes(k))
    if (matched.length > 0) {
      const confidence = Math.min(0.6 + matched.length * 0.1, 0.95)
      return { emotion: rule.emotion, confidence, priority: rule.priority }
    }
  }
  return { emotion: 'calm', confidence: 0.8, priority: 'LOW' }
}
