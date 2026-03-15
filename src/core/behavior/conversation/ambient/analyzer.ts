/**
 * AmbientAnalyzer — 旁听分析器
 * ================================
 * 职责：
 *   - 在 ConversationManager 处于 IDLE 状态时，持续收集 sense.audio.transcript
 *   - 维护一个 60 秒滑动窗口的旁听上下文
 *   - 每当新增一句，检查是否值得插话（LLM 评分 0-10）
 *   - 评分超过阈值 → 发布 sense.conversation.interest 并入队
 *   - 有冷却机制，避免频繁打扰
 */

import { streamText } from 'ai'
import { AGENT_MODEL } from '@/core/cognition/tools'
import { ConversationManager } from '../manager'
import { Spine } from '../../../runtime/spine'
import type { SpineEvent, AudioTranscriptPayload } from '../../../runtime/spine'

const AMBIENT_WINDOW_MS = 60_000     // 旁听上下文保留 60 秒
const INTEREST_THRESHOLD = 7         // 评分 ≥ 7 才插话（0-10）
const COOLDOWN_MS = 5 * 60_000       // 插话后冷却 5 分钟
const MIN_TURNS_TO_ANALYZE = 2       // 至少积累 2 句才开始分析
const MIN_AMBIENT_TEXT_LEN = Number(process.env.MIN_AMBIENT_TEXT_LEN ?? 4)

const AMBIENT_NOISE_PATTERNS = [
  /^(嗯+|啊+|哦+|唉+|哈+|额+)[。！!？?，,、\s]*$/i,
  /^(是吗|好吧|好的|行吧|ok|okay|all right|alright)[。！!？?，,、\s]*$/i,
]

interface AmbientEntry {
  text: string
  timestamp: number
}

let ambientBuffer: AmbientEntry[] = []
let lastInterestMs = 0

export function startAmbientAnalyzer(): void {
  Spine.subscribe<AudioTranscriptPayload>(
    ['sense.audio.transcript'],
    async (event: SpineEvent<AudioTranscriptPayload>) => {
      // 只在 IDLE 状态旁听，对话进行中不干扰
      if (ConversationManager.getState() !== 'IDLE') return
      if (!shouldAnalyzeAmbient(event.payload.text)) return

      const now = Date.now()

      // 追加到缓冲区，清除过期条目
      ambientBuffer.push({ text: event.payload.text, timestamp: now })
      ambientBuffer = ambientBuffer.filter((e) => now - e.timestamp < AMBIENT_WINDOW_MS)

      if (ambientBuffer.length < MIN_TURNS_TO_ANALYZE) return
      if (now - lastInterestMs < COOLDOWN_MS) return

      // 异步评分，不阻塞事件处理
      analyzeInterest(ambientBuffer.map((e) => e.text)).catch((err) => {
        console.error('[AmbientAnalyzer] 评分出错：', err)
      })
    }
  )

  console.log('[AmbientAnalyzer] 已启动，订阅 sense.audio.transcript（IDLE 旁听）')
}

function shouldAnalyzeAmbient(text: string): boolean {
  const trimmed = text.trim()
  if (!trimmed) return false

  for (const pattern of AMBIENT_NOISE_PATTERNS) {
    if (pattern.test(trimmed)) return false
  }

  const compact = trimmed.replace(/[\s，,。！？!?.、；;:："'“”‘’]/g, '')
  return compact.length >= MIN_AMBIENT_TEXT_LEN
}

// ─── 兴趣评分 ─────────────────────────────────────────────────────────────────

async function analyzeInterest(turns: string[]): Promise<void> {
  const context = turns.join('\n')

  const prompt = `你是家庭机器人 Aria，旁听到以下对话片段：

${context}

请评估：你是否应该主动参与这个对话？打分 0-10，并给出参与理由和建议回复。

评分标准：
- 0-3：纯闲聊、私人话题、与你无关 → 不参与
- 4-6：你有一些相关信息但不紧迫 → 酌情参与
- 7-10：你有实质性帮助（信息、提醒、可执行任务）→ 应该参与

请严格按以下 JSON 格式输出，不要加其他内容：
{"score": <0-10>, "reason": "<简短理由>", "reply": "<建议说的话，不超过两句>"}`

  let jsonStr = ''
  try {
    const { textStream } = streamText({
      model: AGENT_MODEL,
      prompt,
      maxOutputTokens: 150,
    })
    for await (const chunk of textStream) {
      jsonStr += chunk
    }

    const result = JSON.parse(jsonStr.trim()) as {
      score: number
      reason: string
      reply: string
    }

    if (result.score < INTEREST_THRESHOLD) return

    const now = Date.now()
    lastInterestMs = now

    console.log(`[AmbientAnalyzer] 兴趣评分 ${result.score}，准备插话`)

    Spine.publish({
      type: 'sense.conversation.interest',
      priority: 'LOW',
      source: 'ambient.analyzer',
      payload: {
        context_snippet: context.slice(0, 100),
        interest_score: result.score,
        suggested_reply: result.reply,
      },
      summary: `旁听兴趣 ${result.score} 分：${result.reason}`,
    })

    ConversationManager.enqueue({
      mode: 'active',
      priority: 3,
      source: 'ambient.interest',
      label: 'ambient.interest',
      content: result.reply,
      triggerNote: `旁听到对话，主动参与。对话内容：${context.slice(0, 80)}`,
      expiresAt: now + 3 * 60_000,
    })

    // 插话后清空旁听缓冲，避免重复分析相同内容
    ambientBuffer = []
  } catch {
    // JSON 解析失败时静默跳过（LLM 偶尔输出格式不对）
  }
}
