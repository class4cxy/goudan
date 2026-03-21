import { queries } from "@/lib/db";
import type { VoiceLogSession, VoiceLogMessage } from "@/lib/db";
import { NextRequest, NextResponse } from "next/server";

export const runtime = "nodejs";

/**
 * GET /api/voice-logs/sessions?day=2026-03-21
 *   → 返回当天的所有 session（含消息）
 *
 * GET /api/voice-logs/sessions?session_id=xxx
 *   → 返回指定 session 的所有消息
 */
export async function GET(req: NextRequest) {
  const { searchParams } = new URL(req.url);
  const day = searchParams.get("day");
  const sessionId = searchParams.get("session_id");

  if (sessionId) {
    const messages = queries.getVoiceSession.all(sessionId) as VoiceLogMessage[];
    return NextResponse.json(messages);
  }

  if (!day) {
    return NextResponse.json({ error: "day or session_id required" }, { status: 400 });
  }

  const sessions = queries.listVoiceSessionsByDay.all(day) as VoiceLogSession[];
  return NextResponse.json(sessions);
}
