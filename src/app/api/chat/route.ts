import { streamText, convertToModelMessages, stepCountIs } from "ai";
import type { UIMessage } from "ai";
import { AGENT_MODEL, ALL_TOOLS } from "@/core/cognition/tools";
import { buildSystemPrompt } from "@/core/cognition/tools/prompts";
import { queries } from "@/lib/db";
import { ConversationBuffer } from "@/core/cognition/memory/conversation-buffer";
import { notifyChatInput, notifyChatComplete } from "@/core/perception/chat";
import { PlatformConnector } from "@/core/runtime/platform-connector";

export const runtime = "nodejs";
export const maxDuration = 60;

/** Extract first text string from a UIMessage's parts. */
function firstText(msg: UIMessage): string | null {
  for (const part of msg.parts ?? []) {
    if (part.type === "text" && part.text) return part.text;
  }
  return null;
}

/** Collect all text segments from all steps into a single string for TTS. */
function extractFullText(steps: Parameters<Parameters<typeof streamText>[0]['onFinish']>[0]['steps']): string {
  return steps
    .map((s) => s.text ?? '')
    .join('')
    .trim()
}


export async function POST(req: Request) {
  const { messages, threadId, voiceMode } = (await req.json()) as {
    messages: UIMessage[];
    threadId?: string;
    /** 为 true 时，AI 回复完成后通过蓝牙扬声器朗读全文（action.speak → Platform TTS） */
    voiceMode?: boolean;
  };

  // 通知 Spine：文字对话开始（侧链事件，供 ConversationManager 状态同步）
  const userText = firstText(messages[messages.length - 1]) ?? '';
  notifyChatInput(userText, threadId);

  // ── ConversationBuffer：历史摘要注入 + rawTail 裁剪 ──────────────────────
  const buffer = threadId ? new ConversationBuffer(threadId) : null
  const historyContext = buffer?.assembleHistoryContext()
  // 如果有历史 chunks，只把未覆盖的 rawTail 传给 LLM；否则传全部消息
  const llmMessages = buffer ? buffer.getRawTail(messages) : messages

  const result = streamText({
    model: AGENT_MODEL,
    system: buildSystemPrompt(historyContext || undefined),
    messages: await convertToModelMessages(llmMessages),
    tools: ALL_TOOLS,
    stopWhen: stepCountIs(8),
    onStepFinish({ toolCalls, usage }) {
      if (toolCalls.length > 0) {
        const names = toolCalls.map((c) => c.toolName).join(", ");
        console.log(`[Agent] 工具调用：${names}，token 消耗：${usage?.totalTokens ?? "?"}`);
      }
    },
    onFinish: async ({ steps }) => {
      // 通知 Spine：文字对话轮次完成
      notifyChatComplete(threadId);

      // voiceMode：将 AI 回复全文发送到 Platform 通过蓝牙朗读
      if (voiceMode) {
        const fullText = extractFullText(steps);
        if (fullText) {
          PlatformConnector.send({
            type: 'action.speak',
            payload: { text: fullText, interrupt_current: true },
          });
        }
      }

      if (!threadId) return;

      // Build assistant UIMessage parts from all steps
      const parts: object[] = [];
      for (const step of steps) {
        parts.push({ type: "step-start" });
        if (step.text) {
          parts.push({ type: "text", text: step.text });
        }
        for (const tc of step.toolCalls ?? []) {
            const toolResult = (step.toolResults as Array<{ toolCallId: string; output: unknown }> | undefined)
            ?.find((r) => r.toolCallId === tc.toolCallId);
          const tcInput = (tc as unknown as { input: unknown }).input ?? (tc as unknown as { args: unknown }).args;
          if (toolResult) {
            parts.push({
              type: "dynamic-tool",
              toolName: tc.toolName,
              toolCallId: tc.toolCallId,
              state: "output-available",
              input: tcInput,
              output: toolResult.output,
            });
          } else {
            parts.push({
              type: "dynamic-tool",
              toolName: tc.toolName,
              toolCallId: tc.toolCallId,
              state: "call",
              input: tcInput,
            });
          }
        }
      }

      const assistantMsg = {
        id: Date.now().toString(36) + Math.random().toString(36).slice(2, 9),
        role: "assistant",
        parts,
        createdAt: new Date().toISOString(),
      };

      const allMessages = [...messages, assistantMsg] as UIMessage[];

      // Ensure thread exists then save messages
      queries.createThread.run(threadId);
      queries.saveThreadMessages.run(threadId, JSON.stringify(allMessages));
      queries.touchThread.run(threadId);

      // 异步压缩（fire-and-forget，不阻塞响应流）
      if (buffer) {
        buffer.maybeCompress(allMessages).catch((err) => {
          console.error("[ConversationBuffer] 压缩失败：", err);
        });
      }

      // 标题由客户端在回复结束后主动调用 POST /api/threads/[id]/title 生成
    },
  });

  return result.toUIMessageStreamResponse();
}
