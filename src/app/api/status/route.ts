import { NextResponse } from "next/server";

export const runtime = "nodejs";

const BRIDGE_URL = process.env.PLATFORM_URL ?? "http://localhost:8001";

export async function GET() {
  try {
    const [statusRes, healthRes] = await Promise.all([
      fetch(`${BRIDGE_URL}/status`, { signal: AbortSignal.timeout(5000) }),
      fetch(`${BRIDGE_URL}/health`, { signal: AbortSignal.timeout(5000) }),
    ]);

    const status = statusRes.ok ? await statusRes.json() : null;
    const health = healthRes.ok ? await healthRes.json() : null;

    return NextResponse.json({ status, health, platform_ok: statusRes.ok });
  } catch {
    return NextResponse.json({ status: null, health: null, platform_ok: false });
  }
}
