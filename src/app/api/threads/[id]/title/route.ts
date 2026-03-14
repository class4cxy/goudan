import { generateText } from "ai";
import { AGENT_MODEL } from "@/core/cognition/tools";
import { queries } from "@/lib/db";
import type { Thread } from "@/lib/db";
import type { UIMessage } from "ai";
import { NextResponse } from "next/server";

export const runtime = "nodejs";

/**
 * POST /api/threads/[id]/title
 * 用 LLM 为指定会话生成标题（只在尚无标题时生效），返回 { title }。
 * 由客户端在 AI 每次回复完成后主动调用，确保标题生成后再刷新侧边栏。
 */
export async function POST(
  _req: Request,
  { params }: { params: Promise<{ id: string }> }
) {
  const { id } = await params;

  const thread = queries.getThread.get(id) as Thread | undefined;
  if (thread?.title) {
    return NextResponse.json({ title: thread.title });
  }

  const row = queries.getThreadMessages.get(id) as { messages: string } | undefined;
  const messages = row ? (JSON.parse(row.messages) as UIMessage[]) : [];

  const firstUser      = messages.find((m) => m.role === "user");
  const firstAssistant = messages.find((m) => m.role === "assistant");

  const userText = firstUser?.parts
    ?.filter((p): p is { type: "text"; text: string } => p.type === "text")
    .map((p) => p.text).join("") ?? "";

  const assistantText = firstAssistant?.parts
    ?.filter((p): p is { type: "text"; text: string } => p.type === "text")
    .map((p) => p.text).join("").slice(0, 300) ?? "";

  if (!userText) {
    return NextResponse.json({ title: null });
  }

  try {
    const { text } = await generateText({
      model: AGENT_MODEL,
      prompt:
        `根据以下对话，生成一个简洁的中文标题（不超过 15 个字，不要引号、不要标点结尾）：\n` +
        `用户：${userText}\n` +
        (assistantText ? `助手：${assistantText}` : ""),
    });
    const title = text.trim().slice(0, 30) || userText.slice(0, 30);
    queries.setThreadTitle.run(title, id);
    return NextResponse.json({ title });
  } catch {
    const fallback = userText.slice(0, 30);
    queries.setThreadTitle.run(fallback, id);
    return NextResponse.json({ title: fallback });
  }
}
