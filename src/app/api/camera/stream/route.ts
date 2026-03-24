/**
 * GET /api/camera/stream
 * 代理转发 platform 的 MJPEG 直播流，避免浏览器直连 platform 端口的跨域问题。
 * 响应体直接透传 ReadableStream，不缓冲，保持低延迟。
 */

import { NextRequest, NextResponse } from "next/server";

const PLATFORM_URL = process.env.PLATFORM_URL ?? "http://localhost:8001";

export const dynamic = "force-dynamic";

export async function GET(_req: NextRequest) {
  let platformRes: Response;
  try {
    platformRes = await fetch(`${PLATFORM_URL}/camera/stream`, {
      // 不设置 AbortSignal.timeout：流是长连接，应跟随客户端断开而结束
      headers: { Accept: "multipart/x-mixed-replace" },
    });
  } catch {
    return NextResponse.json({ error: "摄像头服务不可用" }, { status: 503 });
  }

  if (!platformRes.ok) {
    return NextResponse.json(
      { error: `摄像头流错误：HTTP ${platformRes.status}` },
      { status: platformRes.status },
    );
  }

  return new Response(platformRes.body, {
    headers: {
      "Content-Type": "multipart/x-mixed-replace; boundary=frame",
      "Cache-Control": "no-cache, no-store",
      "X-Accel-Buffering": "no",
    },
  });
}
