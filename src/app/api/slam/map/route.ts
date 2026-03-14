import { NextResponse } from "next/server";

export const runtime = "nodejs";

const BRIDGE_URL = process.env.ROBOROCK_BRIDGE_URL ?? "http://localhost:8001";

/**
 * GET /api/slam/map
 * 代理返回 Platform SLAM 当前地图 PNG，供前端 <img> 直接引用。
 * 加 ?t=<timestamp> 做缓存破坏，确保每次获取最新地图。
 */
export async function GET(): Promise<Response> {
  try {
    const res = await fetch(`${BRIDGE_URL}/slam/map`, {
      signal: AbortSignal.timeout(8_000),
    });

    if (!res.ok) {
      return NextResponse.json(
        { error: "地图尚未生成，请先启动建图" },
        { status: res.status }
      );
    }

    const data = (await res.json()) as {
      image_b64: string;
      width: number;
      height: number;
      mm_per_pixel: number;
      robot_pixel: { x: number; y: number };
      pose: { x_mm: number; y_mm: number; theta_deg: number };
      scan_count: number;
    };

    // 将 base64 转为二进制 PNG 直接返回，避免前端再做 base64 decode
    const binary = Buffer.from(data.image_b64, "base64");
    return new Response(binary, {
      headers: {
        "Content-Type": "image/png",
        // 不缓存：地图实时变化
        "Cache-Control": "no-store",
        // 在响应头里附带元数据，供 Tool UI 展示
        "X-Scan-Count": String(data.scan_count),
        "X-Pose": JSON.stringify(data.pose),
        "X-Mm-Per-Pixel": String(data.mm_per_pixel),
      },
    });
  } catch {
    return NextResponse.json({ error: "platform 不可达" }, { status: 503 });
  }
}
