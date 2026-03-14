/**
 * camera.ts — 机器车摄像头 + 云台工具
 * =========================================
 * 对接 platform 的 /camera/* REST 接口：
 *   takeRobotPhoto    — 拍照，返回 base64 JPEG
 *   moveCameraMount   — 绝对定位云台（pan / tilt）
 *   centerCameraMount — 云台双轴归中
 */

import { tool } from "ai";
import { z } from "zod";

const BRIDGE_URL = process.env.ROBOROCK_BRIDGE_URL ?? "http://localhost:8001";

export const takeRobotPhoto = tool({
  description:
    "用机器车上的摄像头拍一张照片，保存到服务器并返回图片 URL。" +
    "拍照前可先用 moveCameraMount 调整云台朝向，确保拍到想要的区域。",
  inputSchema: z.object({}),
  execute: async () => {
    try {
      const res = await fetch(`${BRIDGE_URL}/camera/capture/save`, {
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
    "控制摄像头云台朝向（双轴舵机）。" +
    "Pan（水平）0°=最左 / 90°=正前 / 180°=最右；" +
    "Tilt（垂直）75°=最低 / 90°=水平 / 105°=最高（硬件限制）。" +
    "两个参数均可省略（省略则该轴保持当前角度）。",
  inputSchema: z.object({
    pan: z
      .number()
      .min(0)
      .max(180)
      .optional()
      .describe("水平角度（0–180°，90=正前）"),
    tilt: z
      .number()
      .min(75)
      .max(105)
      .optional()
      .describe("垂直俯仰角度（75–105°，90=水平）"),
  }),
  execute: async ({ pan, tilt }) => {
    try {
      const res = await fetch(`${BRIDGE_URL}/camera/look_at`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ pan, tilt }),
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

export const centerCameraMount = tool({
  description:
    "将摄像头云台双轴归中（Pan=90°，Tilt=90°，正视前方）。",
  inputSchema: z.object({}),
  execute: async () => {
    try {
      const res = await fetch(`${BRIDGE_URL}/camera/center`, {
        method: "POST",
        signal: AbortSignal.timeout(5_000),
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data = (await res.json()) as { ok: boolean; status: { pan: number; tilt: number } };
      return { success: true, status: data.status };
    } catch (err) {
      return { success: false, error: String(err) };
    }
  },
});
