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
    signal: AbortSignal.timeout(30000),
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: res.statusText }));
    throw new Error(
      `Platform 错误 [${res.status}]: ${(err as { detail?: string }).detail ?? res.statusText}`
    );
  }
  return res.json() as Promise<T>;
}

interface SearchResult {
  title: string;
  url: string;
  snippet: string;
}

export const searchWeb = tool({
  description:
    "搜索互联网获取实时公开信息，适用于：天气、新闻、时事资讯、技术文档、价格查询、人物介绍等任何需要最新信息的场景。" +
    "返回搜索结果列表（标题、链接、摘要）。如需获取某条结果的详细内容，可再调用 fetchWebPage。",
  inputSchema: z.object({
    query: z.string().describe("搜索关键词，支持中英文，建议简洁具体"),
    maxResults: z
      .number()
      .int()
      .min(1)
      .max(10)
      .optional()
      .default(5)
      .describe("返回结果数量，默认 5"),
  }),
  execute: async ({ query, maxResults }) => {
    try {
      const data = await callPlatform<{ query: string; results: SearchResult[] }>(
        "GET",
        `/search?q=${encodeURIComponent(query)}&max_results=${maxResults}`
      );
      return { success: true, query: data.query, results: data.results };
    } catch (err) {
      return { success: false, error: String(err), results: [] };
    }
  },
});

export const fetchWebPage = tool({
  description:
    "抓取指定网页的完整正文内容（自动去除广告、导航栏等噪音，返回纯文本）。" +
    "通常与 searchWeb 配合使用：先搜索拿到 URL，再用此工具读取详细内容。",
  inputSchema: z.object({
    url: z.string().url().describe("要抓取的网页 URL"),
    maxChars: z
      .number()
      .int()
      .min(500)
      .max(8000)
      .optional()
      .default(3000)
      .describe("返回正文最大字符数，默认 3000"),
  }),
  execute: async ({ url, maxChars }) => {
    try {
      const data = await callPlatform<{ url: string; content: string | null; ok: boolean }>(
        "POST",
        "/fetch",
        { url, max_chars: maxChars }
      );
      if (!data.ok || data.content === null) {
        return { success: false, error: "网页内容提取失败，可能是反爬保护或页面为空", content: null };
      }
      return { success: true, url: data.url, content: data.content };
    } catch (err) {
      return { success: false, error: String(err), content: null };
    }
  },
});
