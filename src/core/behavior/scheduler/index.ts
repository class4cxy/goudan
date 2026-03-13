import cron from "node-cron";
import { queries, isQuietHour, parseRawTask } from "@/lib/db";
import type { ScheduledTask, RawScheduledTask } from "@/lib/db";

const runningJobs = new Map<number, cron.ScheduledTask>();

export type TaskExecutor = (task: ScheduledTask) => Promise<unknown>;
let executor: TaskExecutor | null = null;

export function setTaskExecutor(fn: TaskExecutor) {
  executor = fn;
}

export function loadScheduledTasks() {
  const rawTasks = queries.getAllTasks.all() as RawScheduledTask[];
  const tasks = rawTasks.map(parseRawTask);
  for (const task of tasks) {
    if (task.enabled) scheduleTask(task);
  }
  console.log(`[Scheduler] 已加载 ${tasks.filter((t) => t.enabled).length} 个定时任务`);
}

export function scheduleTask(task: ScheduledTask) {
  if (runningJobs.has(task.id)) runningJobs.get(task.id)!.stop();

  if (!cron.validate(task.cron)) {
    console.error(`[Scheduler] 无效 cron：${task.cron}（任务：${task.name}）`);
    return;
  }

  const job = cron.schedule(task.cron, async () => {
    if (isQuietHour()) {
      console.log(`[Scheduler] 静默时段，跳过：${task.name}`);
      return;
    }
    console.log(`[Scheduler] 触发：${task.name}`);
    queries.updateTaskLastRun.run(task.id);
    if (executor) {
      try {
        await executor(task);
      } catch (err) {
        console.error(`[Scheduler] 执行失败：${task.name}`, err);
      }
    }
  });

  runningJobs.set(task.id, job);
}

export function unscheduleTask(taskId: number) {
  const job = runningJobs.get(taskId);
  if (job) { job.stop(); runningJobs.delete(taskId); }
}

export function addScheduledTask(
  name: string,
  cronExpr: string,
  taskType: ScheduledTask["task_type"],
  config: Record<string, unknown> = {}
): ScheduledTask {
  if (!cron.validate(cronExpr)) throw new Error(`无效 cron 表达式：${cronExpr}`);

  const result = queries.insertTask.run(name, cronExpr, taskType, JSON.stringify(config));
  const rawTasks = queries.getAllTasks.all() as RawScheduledTask[];
  const raw = rawTasks.find((t) => t.id === result.lastInsertRowid);
  if (!raw) throw new Error("任务创建失败");

  const task = parseRawTask(raw);
  scheduleTask(task);
  return task;
}

export function toggleTask(taskId: number, enabled: boolean) {
  queries.updateTaskEnabled.run(enabled ? 1 : 0, taskId);
  if (!enabled) {
    unscheduleTask(taskId);
  } else {
    const rawTasks = queries.getAllTasks.all() as RawScheduledTask[];
    const raw = rawTasks.find((t) => t.id === taskId);
    if (raw) scheduleTask(parseRawTask(raw));
  }
}

export function removeTask(taskId: number) {
  unscheduleTask(taskId);
  queries.deleteTask.run(taskId);
}
