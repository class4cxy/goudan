import { tool } from "ai";
import { z } from "zod";
import { execFile } from "child_process";
import { promisify } from "util";
import fs from "fs";
import path from "path";
import crypto from "crypto";

const execFileAsync = promisify(execFile);

const SNAPSHOT_DIR =
  process.env.CAMERA_SNAPSHOT_DIR ?? path.join(process.cwd(), "tmp/snapshots");
const RTSP_URL = process.env.CAMERA_RTSP_URL ?? "";

fs.mkdirSync(SNAPSHOT_DIR, { recursive: true });

async function captureFrame(rtspUrl: string): Promise<{ filePath: string; base64: string }> {
  if (!rtspUrl) {
    throw new Error("未配置摄像头 RTSP 地址，请在 .env 中设置 CAMERA_RTSP_URL");
  }

  const filename = `snapshot_${crypto.randomUUID()}.jpg`;
  const filePath = path.join(SNAPSHOT_DIR, filename);

  await execFileAsync(
    "ffmpeg",
    ["-rtsp_transport", "tcp", "-i", rtspUrl, "-vframes", "1", "-q:v", "2", "-y", filePath],
    { timeout: 15000 }
  );

  const buffer = fs.readFileSync(filePath);
  return { filePath, base64: buffer.toString("base64") };
}

export function cleanupSnapshot(filePath: string) {
  try {
    if (fs.existsSync(filePath)) fs.unlinkSync(filePath);
  } catch {
    // ignore
  }
}

export const takePhoto = tool({
  description:
    "通过小米摄像头（CMSXJ60A）拍摄一张实时截图，用于查看房间当前状况。" +
    "返回图片的 base64 编码，供视觉分析工具使用。",
  inputSchema: z.object({
    camera_id: z.string().default("main").describe("摄像头标识，默认为 main（主摄像头）"),
  }),
  execute: async ({ camera_id }) => {
    try {
      const rtspUrl =
        camera_id === "main"
          ? RTSP_URL
          : (process.env[`CAMERA_RTSP_URL_${camera_id.toUpperCase()}`] ?? RTSP_URL);

      const { filePath, base64 } = await captureFrame(rtspUrl);

      return {
        success: true,
        camera_id,
        snapshot_path: filePath,
        image_base64: base64,
        message: "截图成功，可以使用 analyzeImage 工具分析卫生状况",
      };
    } catch (err) {
      return {
        success: false,
        error: String(err),
        hint: "请确保：1) FFmpeg 已安装；2) CAMERA_RTSP_URL 已正确配置；3) 摄像头与本机在同一局域网",
      };
    }
  },
});

export const checkCameraSetup = tool({
  description: "检查摄像头环境是否配置正确（FFmpeg 是否安装、RTSP 地址是否设置）",
  inputSchema: z.object({}),
  execute: async () => {
    const checks: Record<string, boolean | string> = {};

    try {
      await execFileAsync("ffmpeg", ["-version"]);
      checks.ffmpeg = true;
    } catch {
      checks.ffmpeg = "未安装，请运行 brew install ffmpeg";
    }

    checks.rtsp_configured = RTSP_URL
      ? `已配置 (${RTSP_URL.replace(/:[^:@]+@/, ":***@")})`
      : "未配置，请在 .env 中设置 CAMERA_RTSP_URL";

    checks.snapshot_dir = SNAPSHOT_DIR;

    return { checks };
  },
});
