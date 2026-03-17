/**
 * Brain/Conversation — 语音对话推理
 * =====================================
 * 职责：
 *   - 接收对话上下文，流式生成语音回复
 *   - 按句子边界切分输出，逐句回调供 ConversationManager 发布 action.speak
 *   - 构建适合语音播报的 system prompt（简洁口语化）
 *
 * 首句延迟目标：< 1.5s（从用户说完到第一句 TTS 开始播放）
 */

import { streamText, stepCountIs } from 'ai'
import { AGENT_MODEL } from './index'
import { buildSystemPrompt } from './prompts'
import type { ConversationContext } from '@/core/behavior/conversation/context'

// 句子边界：中文句号/感叹/问号/换行，以及英文标点
const SENTENCE_BOUNDARY = /([。！？!?\n]+)/

const VOICE_ADDENDUM = `

## 当前为语音对话模式
- 回复简洁口语化，每次不超过 3 句话
- 禁止使用 Markdown 格式（*、#、-、**等）
- 禁止列表格式，用自然连贯的句子代替
- 不要说"好的，有其他需要随时说"这类套话
- 直接给出答案或行动，不需要复述用户的问题`

/**
 * 流式生成语音回复，按句子边界逐句回调。
 *
 * @param context       当前会话上下文（含对话历史）
 * @param onSentence    每生成一句就立即回调，isFirst=true 代表第一句
 * @param triggerNote   可选：主动发起时注入的情景说明
 * @param tools         可选：工具集（ALL_TOOLS），由调用方注入避免循环依赖
 * @returns             完整回复文本
 */
export async function generateVoiceResponse(
  context: ConversationContext,
  onSentence: (text: string, isFirst: boolean) => void,
  triggerNote?: string,
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  tools?: Record<string, any>,
): Promise<string> {
  const systemPrompt = buildVoiceSystemPrompt(context.getLastEmotion(), triggerNote)
  const rawMessages = context.getMessages()
  // AI SDK 要求 messages 不能为空数组；主动发言时 context 可能尚无历史，注入占位消息
  const messages = rawMessages.length > 0
    ? rawMessages
    : [{ role: 'user' as const, content: '（主动发言）' }]

  const hasTools = tools && Object.keys(tools).length > 0

  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  const { textStream } = streamText({
    model: AGENT_MODEL,
    system: systemPrompt,
    messages,
    // 有工具时不限制输出 token（工具调用后 LLM 需要额外生成一次口语回复）
    ...(hasTools ? {} : { maxOutputTokens: 200 }),
    ...(hasTools ? { tools, stopWhen: stepCountIs(5) } : {}),
  } as Parameters<typeof streamText>[0])

  let buffer = ''
  let fullText = ''
  let isFirst = true

  for await (const chunk of textStream) {
    if (!chunk) continue
    buffer += chunk
    fullText += chunk

    const parts = buffer.split(SENTENCE_BOUNDARY)
    while (parts.length >= 3) {
      const sentence = (parts.shift() ?? '') + (parts.shift() ?? '')
      const trimmed = sentence.trim()
      if (trimmed) {
        onSentence(trimmed, isFirst)
        isFirst = false
      }
    }
    buffer = parts[0] ?? ''
  }

  const remaining = buffer.trim()
  if (remaining) {
    onSentence(remaining, isFirst)
  }

  return fullText
}

function buildVoiceSystemPrompt(emotion: string, triggerNote?: string): string {
  let prompt = buildSystemPrompt() + VOICE_ADDENDUM

  if (emotion && emotion !== 'calm') {
    prompt += `\n\n## 当前用户情绪\n检测到用户情绪为「${emotion}」，请适当调整语气，优先处理情绪诉求。`
  }

  if (triggerNote) {
    prompt += `\n\n## 当前发言背景\n${triggerNote}`
  }

  return prompt
}
