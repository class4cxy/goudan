import { queries } from "@/lib/db";
import type { Thread } from "@/lib/db";
import { NextResponse } from "next/server";

export const runtime = "nodejs";

export async function GET(
  _req: Request,
  { params }: { params: Promise<{ id: string }> }
) {
  const { id } = await params;
  const row = queries.getThreadMessages.get(id) as { messages: string } | undefined;
  const messages = row ? (JSON.parse(row.messages) as unknown[]) : [];
  return NextResponse.json(messages);
}

export async function DELETE(
  _req: Request,
  { params }: { params: Promise<{ id: string }> }
) {
  const { id } = await params;
  queries.deleteThread.run(id);
  return NextResponse.json({ success: true });
}

export async function PATCH(
  req: Request,
  { params }: { params: Promise<{ id: string }> }
) {
  const { id } = await params;
  const { title } = (await req.json()) as { title?: string };
  if (title !== undefined) {
    queries.setThreadTitle.run(title, id);
  }
  const thread = queries.getThread.get(id) as Thread | undefined;
  return NextResponse.json(thread ?? { error: "not found" });
}
