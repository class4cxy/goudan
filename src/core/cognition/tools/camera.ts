/**
 * camera.ts — 机器车摄像头 + 云台工具
 * =========================================
 * 对接 platform 的 /camera/* REST 接口：
 *   takeRobotPhoto    — 拍照，返回 base64 JPEG
 *   moveCameraMount   — 水平定位云台（pan）
 *   centerCameraMount — 云台归中
 */

import { tool } from "ai";
import { z } from "zod";

const PLATFORM_URL = process.env.PLATFORM_URL ?? "http://localhost:8001";

export const takeRobotPhoto = tool({
  description:
    "用机器车上的摄像头拍一张照片，保存到服务器并返回图片 URL。" +
    "拍照前可先用 moveCameraMount 调整云台朝向，确保拍到想要的区域。",
  inputSchema: z.object({}),
  execute: async () => {
    try {
      const res = await fetch(`${PLATFORM_URL}/camera/capture/save`, {
        signal: AbortSignal.timeout(10_000),
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const { path } = (await res.json()) as { path: string };
      const filename = path.split("/").pop()!;
      const image_url = `/api/snapshot/${filename}`;
      return { success: true, image_url, timestamp: Date.now() };
    } catch (err) {
      return {
        success: false,
        error: String(err),
        hint: "请检查机器车摄像头是否连接（/dev/video0），以及 platform 服务是否在线",
      };
    }
  },
});

export const moveCameraMount = tool({
  description:
    "控制摄像头云台水平朝向（单轴舵机）。" +
    "Pan（水平）0°=最左 / 110°=正前 / 180°=最右。",
  inputSchema: z.object({
    pan: z
      .number()
      .min(0)
      .max(180)
      .describe("水平角度（0–180°，110=正前）"),
  }),
  execute: async ({ pan }) => {
    try {
      const res = await fetch(`${PLATFORM_URL}/camera/look_at`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ pan }),
        signal: AbortSignal.timeout(5_000),
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data = (await res.json()) as Record<string, unknown>;
      return { success: true, ...data };
    } catch (err) {
      return { success: false, error: String(err) };
    }
  },
});

export const openCameraStream = tool({
  description:
    "开启机器车摄像头直播流，在对话中显示实时画面卡片。" +
    "用户说「开摄像头」「直播」「实时查看」时调用。" +
    "调用后立即返回，画面在卡片中持续更新，不阻塞对话。",
  inputSchema: z.object({}),
  execute: async () => {
    try {
      const res = await fetch(`${PLATFORM_URL}/camera/capture/status`, {
        signal: AbortSignal.timeout(5_000),
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      return { success: true, stream_url: "/api/camera/stream" };
    } catch (err) {
      return {
        success: false,
        error: String(err),
        hint: "请检查机器车摄像头是否连接（/dev/video0），以及 platform 服务是否在线",
      };
    }
  },
});

export const closeCameraStream = tool({
  description:
    "关闭机器车摄像头直播流，关闭对话中的实时画面卡片。" +
    "用户说「关摄像头」「停止直播」「关掉」时调用。",
  inputSchema: z.object({}),
  execute: async () => {
    return { success: true };
  },
});

export const centerCameraMount = tool({
  description: "将摄像头云台归中（Pan=110°，正视前方）。",
  inputSchema: z.object({}),
  execute: async () => {
    try {
      const res = await fetch(`${PLATFORM_URL}/camera/center`, {
        method: "POST",
        signal: AbortSignal.timeout(5_000),
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data = (await res.json()) as { ok: boolean; status: { pan: number } };
      return { success: true, status: data.status };
    } catch (err) {
      return { success: false, error: String(err) };
    }
  },
});
