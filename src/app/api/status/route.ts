import { NextResponse } from "next/server";

export const runtime = "nodejs";

const BRIDGE_URL =
  process.env.ROBOROCK_BRIDGE_URL ?? "http://localhost:8001";

export interface StatusResponse {
  bridge_ok: boolean;
  roborock: {
    state?: string;
    state_code?: number;
    battery?: number;
    fan_power?: number;
    error_code?: number;
    in_cleaning?: boolean;
    in_returning?: boolean;
  } | null;
  robot: {
    power: {
      voltage_v: number | null;
      current_ma: number | null;
      power_mw: number | null;
      battery_pct: number | null;
      is_charging: boolean | null;
    };
    modules: {
      lidar: boolean;
      chassis: boolean;
    };
  } | null;
}

export async function GET(): Promise<NextResponse<StatusResponse>> {
  try {
    const [roborockRes, robotRes] = await Promise.all([
      fetch(`${BRIDGE_URL}/status`,       { signal: AbortSignal.timeout(5000) }),
      fetch(`${BRIDGE_URL}/robot/status`, { signal: AbortSignal.timeout(5000) }),
    ]);

    const roborock = roborockRes.ok ? await roborockRes.json() : null;
    const robot    = robotRes.ok    ? await robotRes.json()    : null;

    return NextResponse.json({
      bridge_ok: roborockRes.ok || robotRes.ok,
      roborock,
      robot,
    });
  } catch {
    return NextResponse.json({ bridge_ok: false, roborock: null, robot: null });
  }
}
