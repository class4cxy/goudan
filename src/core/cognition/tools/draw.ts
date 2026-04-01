/**
 * draw.ts — 手绘风图形生成工具
 * =========================================
 * 使用 Rough.js 驱动的手绘风格渲染。
 * 工具在服务端生成结构化 JSON（DrawScene），
 * 前端 RoughRenderer 组件负责实际 Canvas 渲染。
 *
 * 画布坐标系：左上角原点，x 向右，y 向下。
 * 默认画布尺寸：600×400。
 */

import { tool } from "ai";
import { z } from "zod";

const roughOptsSchema = z.object({
  roughness: z.number().min(0).max(4).optional()
    .describe("手绘抖动程度，0=光滑，1.5~2=手绘感，4=极乱"),
  stroke: z.string().optional().describe("描边颜色，如 #333 或 brown"),
  strokeWidth: z.number().optional().describe("描边粗细，默认 1"),
  fill: z.string().optional().describe("填充颜色"),
  fillStyle: z.enum(["hachure", "solid", "zigzag", "cross-hatch", "dots", "dashed", "zigzag-line"])
    .optional().describe("填充样式，hachure=斜线纹，solid=实心"),
  seed: z.number().int().optional()
    .describe("随机种子，相同 seed 每次渲染结果一致"),
}).optional();

const commandSchema = z.discriminatedUnion("type", [
  z.object({
    type: z.literal("rect"),
    x: z.number(), y: z.number(), w: z.number(), h: z.number(),
    opts: roughOptsSchema,
  }),
  z.object({
    type: z.literal("circle"),
    x: z.number().describe("圆心 x"),
    y: z.number().describe("圆心 y"),
    d: z.number().describe("直径"),
    opts: roughOptsSchema,
  }),
  z.object({
    type: z.literal("ellipse"),
    x: z.number().describe("中心 x"),
    y: z.number().describe("中心 y"),
    w: z.number().describe("宽"),
    h: z.number().describe("高"),
    opts: roughOptsSchema,
  }),
  z.object({
    type: z.literal("line"),
    x1: z.number(), y1: z.number(), x2: z.number(), y2: z.number(),
    opts: roughOptsSchema,
  }),
  z.object({
    type: z.literal("polygon"),
    points: z.array(z.tuple([z.number(), z.number()])).describe("顶点列表 [[x,y],...]"),
    opts: roughOptsSchema,
  }),
  z.object({
    type: z.literal("path"),
    d: z.string().describe("SVG path data，如 M 10 10 L 100 50 Z"),
    opts: roughOptsSchema,
  }),
  z.object({
    type: z.literal("text"),
    x: z.number(), y: z.number(),
    text: z.string(),
    size: z.number().optional().describe("字号，默认 16"),
    color: z.string().optional().describe("文字颜色"),
    font: z.string().optional().describe("字体，默认使用系统默认字体"),
  }),
]);

export const drawScene = tool({
  description: `
在聊天界面以手绘/素描风格画一幅创意图，适合教学示意图、儿童插画等场景。

坐标系：左上角 (0,0)，x 向右，y 向下。
默认画布尺寸 600×400，可自定义。

可用图元：
- rect: 矩形（x, y 为左上角）
- circle: 圆（x, y 为圆心，d 为直径）
- ellipse: 椭圆（x, y 为中心，w/h 为宽高）
- line: 直线（x1,y1 → x2,y2）
- polygon: 多边形（points 为顶点数组）
- path: SVG path（适合复杂轮廓）
- text: 文字标注

绘图建议：
- roughness 1.5~2 获得自然手绘感
- fillStyle "hachure" 适合草图风，"solid" 适合儿童插画
- 每条 command 加合适的 seed 确保重现性
- 地图/轮廓用 polygon 或 path
- 用 text 标注地名、图例等
`.trim(),
  inputSchema: z.object({
    width: z.number().default(600).describe("画布宽度，像素"),
    height: z.number().default(400).describe("画布高度，像素"),
    background: z.string().optional().describe("背景色，如 #fef9ef 或 white"),
    title: z.string().optional().describe("图片标题，显示在卡片顶部"),
    commands: z.array(commandSchema).describe("绘图指令列表，按绘制顺序排列（下层先画）"),
  }),
  execute: async (scene) => {
    // 服务端不渲染，直接把结构化数据透传给前端 RoughRenderer
    return { success: true, scene };
  },
});
