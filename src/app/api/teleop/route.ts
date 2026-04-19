import { NextRequest, NextResponse } from "next/server";

export const runtime = "nodejs";

const PLATFORM_URL = process.env.PLATFORM_URL ?? "http://localhost:8001";

type TeleopAction = "start" | "command" | "stop";

export async function POST(req: NextRequest) {
  let body: Record<string, unknown>;
  try {
    body = (await req.json()) as Record<string, unknown>;
  } catch {
    return NextResponse.json({ error: "请求体必须是 JSON" }, { status: 400 });
  }

  const action = body.action as TeleopAction | undefined;
  if (!action || !["start", "command", "stop"].includes(action)) {
    return NextResponse.json({ error: "action 必须是 start/command/stop" }, { status: 400 });
  }

  let endpoint = "";
  let payload: Record<string, unknown> = {};
  if (action === "start") {
    endpoint = "/teleop/start";
    payload = {
      timeout_ms: body.timeout_ms,
      max_speed: body.max_speed,
      deadband: body.deadband,
      min_safe_mm: body.min_safe_mm,
      front_half_angle_deg: body.front_half_angle_deg,
      scan_freshness_ms: body.scan_freshness_ms,
    };
  } else if (action === "command") {
    endpoint = "/teleop/command";
    payload = {
      throttle: body.throttle,
      steer: body.steer,
      max_speed: body.max_speed,
    };
  } else {
    endpoint = "/teleop/stop";
  }

  try {
    const res = await fetch(`${PLATFORM_URL}${endpoint}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: action === "stop" ? undefined : JSON.stringify(payload),
      signal: AbortSignal.timeout(3000),
    });
    const json = await res.json().catch(() => ({}));
    return NextResponse.json(json, { status: res.status });
  } catch {
    return NextResponse.json({ error: "Platform teleop 服务不可达" }, { status: 503 });
  }
}

export async function GET() {
  try {
    const res = await fetch(`${PLATFORM_URL}/teleop/status`, {
      signal: AbortSignal.timeout(3000),
    });
    const json = await res.json().catch(() => ({}));
    return NextResponse.json(json, { status: res.status });
  } catch {
    return NextResponse.json({ error: "Platform teleop 服务不可达" }, { status: 503 });
  }
}
