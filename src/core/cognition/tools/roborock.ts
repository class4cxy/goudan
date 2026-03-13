import { tool } from "ai";
import { z } from "zod";
import { queries } from "@/lib/db";

const BRIDGE_URL = process.env.PLATFORM_URL ?? "http://localhost:8001";

async function callBridge<T = unknown>(
  method: "GET" | "POST",
  path: string,
  body?: unknown
): Promise<T> {
  const res = await fetch(`${BRIDGE_URL}${path}`, {
    method,
    headers: { "Content-Type": "application/json" },
    body: body ? JSON.stringify(body) : undefined,
    signal: AbortSignal.timeout(15000),
  });

  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: res.statusText }));
    throw new Error(
      `Bridge 错误 [${res.status}]: ${(err as { detail?: string }).detail ?? res.statusText}`
    );
  }
  return res.json() as Promise<T>;
}

export const getRobotStatus = tool({
  description: "获取扫地机器人当前状态，包括电量、清扫状态、错误信息等",
  inputSchema: z.object({}),
  execute: async () => {
    try {
      const status = await callBridge("GET", "/status");
      return { success: true, status };
    } catch (err) {
      return { success: false, error: String(err) };
    }
  },
});

export const getRooms = tool({
  description: "获取家里所有已识别的房间列表及其 ID，用于指定房间清扫",
  inputSchema: z.object({}),
  execute: async () => {
    try {
      const data = await callBridge<{ rooms: Record<string, number> }>("GET", "/rooms");
      return { success: true, rooms: data.rooms };
    } catch (err) {
      return { success: false, error: String(err) };
    }
  },
});

export const startFullCleaning = tool({
  description: "启动全屋清扫模式，机器人会自动清扫所有可达区域",
  inputSchema: z.object({}),
  execute: async () => {
    try {
      const result = await callBridge<{ ok: boolean; action: string }>("POST", "/clean/start");
      queries.insertCleaningRecord.run(null, "full", "user", null, null);
      return { success: true, message: result.action };
    } catch (err) {
      return { success: false, error: String(err) };
    }
  },
});

export const cleanRooms = tool({
  description: "让机器人清扫指定的一个或多个房间，使用中文房间名称（如：客厅、主卧、厨房）",
  inputSchema: z.object({
    rooms: z.array(z.string()).min(1).describe("要清扫的房间名称列表，如 ['客厅', '厨房']"),
    repeat: z.number().int().min(1).max(3).default(1).describe("清扫遍数，1-3次"),
  }),
  execute: async ({ rooms, repeat }) => {
    try {
      const result = await callBridge<{ ok: boolean; action: string }>("POST", "/clean/rooms", {
        room_names: rooms,
        repeat,
      });
      queries.insertCleaningRecord.run(JSON.stringify(rooms), "rooms", "user", null, null);
      return { success: true, message: result.action };
    } catch (err) {
      return { success: false, error: String(err) };
    }
  },
});

export const pauseCleaning = tool({
  description: "暂停当前的清扫任务，机器人会停在原地等待",
  inputSchema: z.object({}),
  execute: async () => {
    try {
      const result = await callBridge<{ ok: boolean; action: string }>("POST", "/clean/pause");
      return { success: true, message: result.action };
    } catch (err) {
      return { success: false, error: String(err) };
    }
  },
});

export const resumeCleaning = tool({
  description: "继续之前暂停的清扫任务",
  inputSchema: z.object({}),
  execute: async () => {
    try {
      const result = await callBridge<{ ok: boolean; action: string }>("POST", "/clean/resume");
      return { success: true, message: result.action };
    } catch (err) {
      return { success: false, error: String(err) };
    }
  },
});

export const returnHome = tool({
  description: "停止清扫，让机器人返回充电桩充电",
  inputSchema: z.object({}),
  execute: async () => {
    try {
      const result = await callBridge<{ ok: boolean; action: string }>("POST", "/home");
      return { success: true, message: result.action };
    } catch (err) {
      return { success: false, error: String(err) };
    }
  },
});

export const getCleaningHistory = tool({
  description: "查询最近的清扫历史记录，包括清扫时间、区域和时长",
  inputSchema: z.object({
    limit: z.number().int().min(1).max(20).default(5).describe("返回记录数量"),
  }),
  execute: async ({ limit }) => {
    const records = queries.getCleaningHistory.all(limit);
    return { success: true, records };
  },
});
