import { tool } from "ai";
import { z } from "zod";

const PLATFORM_URL = process.env.PLATFORM_URL ?? "http://localhost:8001";

async function callPlatform<T = unknown>(
  method: "GET" | "POST",
  path: string,
  body?: unknown
): Promise<T> {
  const res = await fetch(`${PLATFORM_URL}${path}`, {
    method,
    headers: { "Content-Type": "application/json" },
    body: body ? JSON.stringify(body) : undefined,
    signal: AbortSignal.timeout(60000),
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: res.statusText }));
    throw new Error(
      `Platform 错误 [${res.status}]: ${(err as { detail?: string }).detail ?? res.statusText}`
    );
  }
  return res.json() as Promise<T>;
}

export const playMusic = tool({
  description:
    "播放音乐。支持在线搜索（YouTube，需要联网 + yt-dlp 已安装）和本地文件（/home/pi/Music 目录）。" +
    "用法示例：播放某首歌时传歌名 + 歌手，如"周杰伦 晴天"；播放本地文件时传文件名，如"song.mp3"。" +
    "默认打断当前播放；设 interrupt=false 可加入队列末尾。",
  inputSchema: z.object({
    query: z
      .string()
      .describe("搜索词（如"周杰伦 晴天"、"Taylor Swift Shake It Off"）、本地文件名（如"song.mp3"）或 HTTP URL"),
    interrupt: z
      .boolean()
      .optional()
      .default(true)
      .describe("是否打断当前播放（默认 true）。false 时加入队列末尾"),
  }),
  execute: async ({ query, interrupt }) => {
    try {
      const data = await callPlatform<{ ok: boolean; queued: string; title?: string; is_local?: boolean }>(
        "POST",
        "/music/play",
        { query, interrupt }
      );
      return {
        success: data.ok,
        message: `正在播放：${data.title || data.queued}${data.is_local ? "（本地文件）" : "（在线搜索中…）"}`,
        title: data.title,
        query: data.queued,
        is_local: data.is_local,
      };
    } catch (err) {
      return { success: false, error: String(err) };
    }
  },
});

export const controlMusic = tool({
  description:
    "控制音乐播放器：暂停、继续、停止、下一曲或调整音量。" +
    "当用户说"暂停音乐"、"停止"、"下一首"、"声音大一点/小一点"时使用此工具。",
  inputSchema: z.object({
    action: z
      .enum(["pause", "resume", "stop", "next", "volume"])
      .describe("操作类型：pause（暂停）/ resume（继续）/ stop（停止并清空队列）/ next（下一曲）/ volume（设置音量）"),
    volume: z
      .number()
      .min(0)
      .max(2)
      .optional()
      .describe("音量值（0.0–2.0，1.0 为原始音量，1.5 为默认值）。仅 action=volume 时有效"),
  }),
  execute: async ({ action, volume }) => {
    try {
      if (action === "volume") {
        if (volume === undefined) {
          return { success: false, error: "设置音量时必须提供 volume 参数（0.0–2.0）" };
        }
        const data = await callPlatform<{ ok: boolean; volume: number }>(
          "POST",
          "/music/volume",
          { volume }
        );
        return { success: data.ok, volume: data.volume, message: `音量已设为 ${(data.volume * 100).toFixed(0)}%` };
      }

      const endpointMap: Record<string, string> = {
        pause:  "/music/pause",
        resume: "/music/resume",
        stop:   "/music/stop",
        next:   "/music/next",
      };

      const data = await callPlatform<{ ok: boolean; state?: string; message?: string }>(
        "POST",
        endpointMap[action]
      );
      const actionLabel: Record<string, string> = {
        pause:  "已暂停",
        resume: "已继续播放",
        stop:   "已停止，队列已清空",
        next:   "已跳至下一曲",
      };
      return {
        success: data.ok,
        message: data.message ?? actionLabel[action],
        state: data.state,
      };
    } catch (err) {
      return { success: false, error: String(err) };
    }
  },
});

export const getMusicStatus = tool({
  description:
    "获取当前音乐播放状态：是否在播放、当前曲目名称、待播队列、音量等。" +
    "当用户询问"现在播的什么歌"、"播放状态如何"时使用。",
  inputSchema: z.object({}),
  execute: async () => {
    try {
      const data = await callPlatform<{
        state: string;
        current: { title: string; query: string; is_local: boolean } | null;
        queue_length: number;
        queue_preview: string[];
        volume: number;
      }>("GET", "/music/status");

      const stateLabel: Record<string, string> = {
        idle:    "空闲（未播放）",
        loading: "加载中",
        playing: "播放中",
        paused:  "已暂停",
      };

      return {
        success: true,
        state: data.state,
        state_label: stateLabel[data.state] ?? data.state,
        current_title: data.current?.title ?? null,
        queue_length: data.queue_length,
        queue_preview: data.queue_preview,
        volume_pct: Math.round(data.volume * 100),
      };
    } catch (err) {
      return { success: false, error: String(err) };
    }
  },
});

export const listLocalMusic = tool({
  description:
    "列出树莓派本地音乐目录（/home/pi/Music）中的所有音频文件。" +
    "当用户问"本地有什么歌"、"我存了哪些音乐"时使用。",
  inputSchema: z.object({}),
  execute: async () => {
    try {
      const data = await callPlatform<{
        music_dir: string;
        count: number;
        files: string[];
      }>("GET", "/music/list");
      return {
        success: true,
        music_dir: data.music_dir,
        count: data.count,
        files: data.files,
      };
    } catch (err) {
      return { success: false, error: String(err) };
    }
  },
});
