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
你当前在语音对话模式，必须严格遵守以下输出规范。
1) 只输出可直接朗读的纯文本自然段；句数和长度按用户需求自适应，讲故事或详细解释时可以更长，不要换行。
2) 绝对不要输出任何 Markdown 或排版符号：# * ** _ - > \` [] () | --- 以及编号/项目符号。
3) 绝对不要输出标题、列表、代码块、链接、表格、引用。
4) 允许自然的礼貌用语、关怀语气、轻松玩笑或撒娇式表达，用于缓解气氛；但要保持内容连贯、可朗读。
5) 输出前先自检：如果包含任意格式符号或列表痕迹，立刻重写为纯口语句子后再输出。`

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
    // 语音场景允许长回答（如讲故事），不对输出 token 做硬上限。
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
