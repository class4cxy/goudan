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

const MIN_DURATION_MS = 400  // 短于此时长的片段丢弃（环境噪声）

// 唤醒词列表，逗号分隔，支持环境变量覆盖
const WAKE_WORDS: string[] = (process.env.WAKE_WORDS ?? 'Aria,小豆,狗蛋,aria')
  .split(',')
  .map((w) => w.trim())
  .filter(Boolean)

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
        const text = await transcribe(audio_b64, sample_rate)

        if (!text?.trim()) {
          console.log(`[AudioThalamus] STT 返回空，丢弃`)
          return
        }

        console.log(`[AudioThalamus] STT 结果："${text.slice(0, 60)}${text.length > 60 ? '…' : ''}"`)

        // 唤醒词检测：命中则走 sense.audio.keyword 路径，不再发 transcript
        const hitWord = detectWakeWord(text)
        if (hitWord) {
          console.log(`[AudioThalamus] 唤醒词命中："${hitWord}"`)
          Spine.publish({
            type: 'sense.audio.keyword',
            priority: 'HIGH',
            source: 'thalamus.audio',
            payload: { keyword: hitWord, transcript: text, duration_ms },
            summary: `唤醒词命中："${hitWord}"，原句：${text.slice(0, 40)}`,
          })
          return
        }

        // 普通语音：发布转写结果
        Spine.publish({
          type: 'sense.audio.transcript',
          priority: 'MEDIUM',
          source: 'thalamus.audio',
          payload: { text, duration_ms },
          summary: `转写完成："${text.slice(0, 40)}${text.length > 40 ? '…' : ''}"`,
        })

        // 并行发布情绪分析
        const emotion = analyzeEmotion(text)
        Spine.publish({
          type: 'sense.audio.emotion',
          priority: emotion.priority,
          source: 'thalamus.audio',
          payload: {
            emotion: emotion.emotion,
            confidence: emotion.confidence,
            text_snippet: text.slice(0, 60),
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

// ─── STT ─────────────────────────────────────────────────────────────────────

async function transcribe(audio_b64: string, sampleRate: number): Promise<string> {
  const speechApiUrl = process.env.SPEECH_API_URL
  const speechApiKey = process.env.SPEECH_API_KEY

  if (!speechApiUrl || !speechApiKey) {
    throw new Error('语音转文字API配置不完整，请检查 SPEECH_API_URL / SPEECH_API_KEY')
  }

  // PCM → WAV，base64 编码后作为 input_audio data URI 传入 chat/completions
  const pcmBuffer = Buffer.from(audio_b64, 'base64')
  const wavBuffer = pcmToWav(pcmBuffer, sampleRate)
  const wavBase64 = wavBuffer.toString('base64')
  const dataUri = `data:audio/wav;base64,${wavBase64}`

  const res = await fetch(speechApiUrl, {
    method: 'POST',
    headers: {
      Authorization: `Bearer ${speechApiKey}`,
      'Content-Type': 'application/json',
    },
    body: JSON.stringify({
      model: ASR_MODEL,
      messages: [
        {
          role: 'user',
          content: [
            {
              type: 'input_audio',
              input_audio: { data: dataUri },
            },
          ],
        },
      ],
      stream: false,
    }),
    signal: AbortSignal.timeout(60_000),
  })

  if (!res.ok) {
    const body = await res.text().catch(() => '')
    throw new Error(`ASR 调用失败：status=${res.status}, body=${body.slice(0, 200)}`)
  }

  const data = (await res.json().catch(() => null)) as {
    choices?: Array<{ message?: { content?: unknown } }>
  } | null

  const text =
    typeof data?.choices?.[0]?.message?.content === 'string'
      ? data.choices[0].message.content
      : ''

  return text
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
