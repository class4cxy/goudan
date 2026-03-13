/**
 * Brain — 大脑层（统一 LLM 推理入口）
 * ======================================
 * 职责：定义推理模型实例，供各 Brain 子模块使用。
 *
 * 子模块：
 *   brain/prompts.ts       — 系统人设与 Prompt 构建
 *   brain/conversation.ts  — 语音对话推理（流式分句）
 *   brain/task.ts          — 定时任务自主执行推理
 */

import { createDeepSeek } from "@ai-sdk/deepseek";

const deepseek = createDeepSeek({
  apiKey: process.env.DEEPSEEK_API_KEY ?? "",
});

/** 主推理模型，供所有 brain 子模块共用 */
export const AGENT_MODEL = deepseek("deepseek-chat");

export { buildSystemPrompt, SYSTEM_PROMPT, SCHEDULER_PROMPT } from "./prompts";
export { generateVoiceResponse } from "./conversation";
export { executeScheduledTask } from "./task";
