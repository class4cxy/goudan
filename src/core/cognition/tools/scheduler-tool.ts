import { tool } from "ai";
import { z } from "zod";
import { addScheduledTask } from "@/core/behavior/scheduler";
import { queries, parseRawTask } from "@/lib/db";
import type { RawScheduledTask } from "@/lib/db";

export const addScheduledTaskTool = tool({
  description:
    "为家庭智能管家添加一个定时任务。支持三种类型：" +
    "inspect_and_clean（定时巡检+按需清扫）、clean_rooms（定时清扫指定房间）、clean_full（定时全屋清扫）。" +
    "cron 示例：'0 9 * * *' 表示每天早上9点，'0 9 * * 1-5' 表示工作日早上9点。",
  inputSchema: z.object({
    name: z.string().describe("任务名称，如 '每天早上巡检'"),
    cron: z.string().describe("cron 表达式，如 '0 9 * * *'"),
    task_type: z.enum(["inspect_and_clean", "clean_rooms", "clean_full"]).describe("任务类型"),
    rooms: z.array(z.string()).optional().describe("指定房间列表（仅 clean_rooms 需要）"),
  }),
  execute: async ({ name, cron, task_type, rooms }) => {
    try {
      const config: Record<string, unknown> = {};
      if (rooms && rooms.length > 0) config.rooms = rooms;

      const task = addScheduledTask(name, cron, task_type, config);
      return {
        success: true,
        task_id: task.id,
        message: `定时任务"${name}"已创建，将按照 ${cron} 执行`,
      };
    } catch (err) {
      return { success: false, error: String(err) };
    }
  },
});

export const listScheduledTasksTool = tool({
  description: "查看所有已设置的定时任务",
  inputSchema: z.object({}),
  execute: async () => {
    const rawTasks = queries.getAllTasks.all() as RawScheduledTask[];
    const tasks = rawTasks.map((t) => {
      const task = parseRawTask(t);
      return {
        id: task.id,
        name: task.name,
        cron: task.cron,
        task_type: task.task_type,
        enabled: task.enabled,
        last_run_at: t.last_run_at
          ? new Date(t.last_run_at * 1000).toLocaleString("zh-CN")
          : "从未运行",
      };
    });
    return { success: true, count: tasks.length, tasks };
  },
});
