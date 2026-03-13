/**
 * Brain/Task — 定时任务自主执行推理
 * =====================================
 * 职责：
 *   - 接收定时任务描述，调用 LLM 自主完成（巡检/清扫等）
 *   - tools 通过参数注入，避免与 agent/ 产生循环依赖
 *
 * 使用方：agent/index.ts 将 ALL_TOOLS 注入后导出 executeScheduledTask。
 */

import { generateText, stepCountIs } from "ai";
import { AGENT_MODEL } from "./index";
import { SCHEDULER_PROMPT } from "./prompts";
import type { ScheduledTask } from "@/lib/db";

export async function executeScheduledTask(
  task: ScheduledTask,
  tools: Record<string, unknown>,
): Promise<string> {
  const taskDesc =
    task.task_type === "clean_rooms"
      ? `清扫房间：${JSON.stringify(task.config.rooms)}`
      : "全屋清扫";

  const { text } = await generateText({
    model: AGENT_MODEL,
    system: SCHEDULER_PROMPT,
    prompt: `定时任务触发：${task.name}。请执行：${taskDesc}`,
    tools: tools as Parameters<typeof generateText>[0]["tools"],
    stopWhen: stepCountIs(6),
  });

  return text;
}
