import { NextResponse } from "next/server";

export const runtime = "nodejs";

const BRIDGE_URL = process.env.ROBOROCK_BRIDGE_URL ?? "http://localhost:8001";

/**
 * GET /api/snapshot/[filename]
 * 代理转发 platform 快照图片，避免浏览器直连 platform 端口的跨域问题。
 */
export async function GET(
  _req: Request,
  { params }: { params: Promise<{ filename: string }> }
) {
  const { filename } = await params;

  // 只允许 .jpg / .jpeg / .png，防止路径穿越
  if (!/^[\w\-]+\.(jpg|jpeg|png)$/i.test(filename)) {
    return NextResponse.json({ error: "invalid filename" }, { status: 400 });
  }

  try {
    const res = await fetch(`${BRIDGE_URL}/snapshots/${filename}`, {
      signal: AbortSignal.timeout(8_000),
    });
    if (!res.ok) {
      return NextResponse.json({ error: "snapshot not found" }, { status: res.status });
    }

    const buffer = await res.arrayBuffer();
    return new Response(buffer, {
      headers: {
        "Content-Type": "image/jpeg",
        "Cache-Control": "public, max-age=86400",
      },
    });
  } catch {
    return NextResponse.json({ error: "platform unreachable" }, { status: 503 });
  }
}
