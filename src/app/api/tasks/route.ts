import { NextRequest, NextResponse } from "next/server";
import { queries, parseRawTask } from "@/lib/db";
import { toggleTask, removeTask } from "@/core/behavior/scheduler";
import type { RawScheduledTask } from "@/lib/db";
import { z } from "zod";

export const runtime = "nodejs";

export async function GET() {
  const rawTasks = queries.getAllTasks.all() as RawScheduledTask[];
  const tasks = rawTasks.map(parseRawTask);
  return NextResponse.json({ tasks });
}

const ToggleSchema = z.object({
  id: z.number().int(),
  enabled: z.boolean(),
});

const DeleteSchema = z.object({ id: z.number().int() });

export async function PATCH(req: NextRequest) {
  const body = await req.json();
  const parsed = ToggleSchema.safeParse(body);
  if (!parsed.success) {
    return NextResponse.json({ error: "参数错误" }, { status: 400 });
  }
  toggleTask(parsed.data.id, parsed.data.enabled);
  return NextResponse.json({ ok: true });
}

export async function DELETE(req: NextRequest) {
  const body = await req.json();
  const parsed = DeleteSchema.safeParse(body);
  if (!parsed.success) {
    return NextResponse.json({ error: "参数错误" }, { status: 400 });
  }
  removeTask(parsed.data.id);
  return NextResponse.json({ ok: true });
}
