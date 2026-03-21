import { queries } from "@/lib/db";
import type { VoiceLogDay } from "@/lib/db";
import { NextResponse } from "next/server";

export const runtime = "nodejs";

/** GET /api/voice-logs — 返回最近 90 天中有语音记录的日期列表 */
export async function GET() {
  const days = queries.listVoiceLogDays.all() as VoiceLogDay[];
  return NextResponse.json(days);
}
