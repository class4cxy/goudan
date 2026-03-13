import { queries } from "@/lib/db";
import type { Thread } from "@/lib/db";
import { NextResponse } from "next/server";

export const runtime = "nodejs";

export async function GET() {
  const threads = queries.listThreads.all() as Thread[];
  return NextResponse.json(threads);
}

export async function POST(req: Request) {
  const { id } = (await req.json()) as { id: string };
  if (!id) return NextResponse.json({ error: "id required" }, { status: 400 });

  queries.createThread.run(id);
  const thread = queries.getThread.get(id) as Thread;
  return NextResponse.json(thread);
}
