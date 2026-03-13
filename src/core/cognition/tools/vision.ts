import { tool, generateText } from "ai";
import { createOpenAI } from "@ai-sdk/openai";
import { z } from "zod";
import { queries } from "@/lib/db";
import { cleanupSnapshot } from "@/core/cognition/tools/camera";

const qwen = createOpenAI({
  baseURL: "https://dashscope.aliyuncs.com/compatible-mode/v1",
  apiKey: process.env.DASHSCOPE_API_KEY ?? "",
});

const VISION_MODEL = "qwen-vl-max";

const CLEANLINESS_PROMPT = `你是一个专业的家庭卫生检查助手。请分析这张房间图片，评估地面卫生状况。

请以 JSON 格式输出，不要有任何其他文字：
{
  "score": <整数 1-5，1=非常干净，5=非常脏>,
  "has_trash": <布尔值，地面是否有可见垃圾/碎屑/灰尘>,
  "dirty_zones": [<描述脏乱区域的字符串数组，如 "左侧角落有纸屑"、"沙发下有灰尘">],
  "recommendation": <清洁建议，如 "建议对客厅进行一次强力清扫" 或 "房间较干净，无需清扫">,
  "clean_mode": <"none"|"standard"|"strong"，推荐清扫模式>
}`;

export interface CleanlinessReport {
  score: number;
  has_trash: boolean;
  dirty_zones: string[];
  recommendation: string;
  clean_mode: "none" | "standard" | "strong";
}

async function analyzeWithQwenVL(imageBase64: string): Promise<CleanlinessReport> {
  const { text } = await generateText({
    model: qwen(VISION_MODEL),
    messages: [
      {
        role: "user",
        content: [
          { type: "image", image: `data:image/jpeg;base64,${imageBase64}` },
          { type: "text", text: CLEANLINESS_PROMPT },
        ],
      },
    ],
    maxOutputTokens: 512,
  });

  const jsonMatch = text.match(/\{[\s\S]*\}/);
  if (!jsonMatch) throw new Error(`Qwen-VL 返回格式异常：${text}`);

  const report = JSON.parse(jsonMatch[0]) as CleanlinessReport;
  if (typeof report.score !== "number" || report.score < 1 || report.score > 5) report.score = 3;
  if (!Array.isArray(report.dirty_zones)) report.dirty_zones = [];

  return report;
}

export const analyzeImage = tool({
  description:
    "使用 Qwen-VL 视觉大模型分析摄像头截图的卫生状况，判断是否需要清扫及哪些区域需要清洁。" +
    "需要先调用 takePhoto 工具获取图片。",
  inputSchema: z.object({
    image_base64: z.string().describe("摄像头截图的 base64 编码（由 takePhoto 工具返回）"),
    snapshot_path: z.string().optional().describe("截图文件路径（分析后自动清理）"),
    camera_id: z.string().default("main").describe("摄像头标识"),
  }),
  execute: async ({ image_base64, snapshot_path, camera_id }) => {
    try {
      const report = await analyzeWithQwenVL(image_base64);

      queries.insertInspection.run(
        camera_id,
        report.score,
        report.has_trash ? 1 : 0,
        JSON.stringify(report.dirty_zones),
        null
      );

      if (snapshot_path) cleanupSnapshot(snapshot_path);

      return {
        success: true,
        report,
        summary: `卫生评分：${report.score}/5，${report.has_trash ? "发现垃圾" : "未发现明显垃圾"}。${report.recommendation}`,
      };
    } catch (err) {
      return {
        success: false,
        error: String(err),
        hint: "请确保 DASHSCOPE_API_KEY 已配置，且阿里云 Dashscope 服务可访问",
      };
    }
  },
});

export const getInspectionHistory = tool({
  description: "查询最近的摄像头巡检记录，了解家里历史卫生状况",
  inputSchema: z.object({
    limit: z.number().int().min(1).max(10).default(3).describe("返回记录数量"),
  }),
  execute: async ({ limit }) => {
    const records = queries.getInspections.all(limit);
    return { success: true, records };
  },
});
