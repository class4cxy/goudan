/**
 * agent/ — 工具注册表 + 任务执行绑定
 * ======================================
 * 职责：
 *   - 聚合所有 tool 定义（ALL_TOOLS）
 *   - 将 brain/task.ts 与 ALL_TOOLS 绑定，导出 executeScheduledTask
 *
 * 模型与 Prompt 实际定义在 src/lib/brain/，此处 re-export 保持向后兼容。
 */

import { executeScheduledTask as _execTask } from "@/core/cognition/brain/task";
import { getRobotStatus, getRooms, startFullCleaning, cleanRooms, pauseCleaning, resumeCleaning, returnHome, getCleaningHistory } from "@/core/cognition/tools/roborock";
import { takePhoto, checkCameraSetup } from "@/core/cognition/tools/camera";
import { analyzeImage, getInspectionHistory } from "@/core/cognition/tools/vision";
import { addScheduledTaskTool, listScheduledTasksTool } from "@/core/cognition/tools/scheduler-tool";
import { navigateTo } from "@/core/cognition/tools/motor";
import { getPowerStatus } from "@/core/cognition/tools/power";
import type { ScheduledTask } from "@/lib/db";

export const ALL_TOOLS = {
  getRobotStatus,
  getRooms,
  startFullCleaning,
  cleanRooms,
  pauseCleaning,
  resumeCleaning,
  returnHome,
  getCleaningHistory,
  takePhoto,
  checkCameraSetup,
  analyzeImage,
  getInspectionHistory,
  addScheduledTask: addScheduledTaskTool,
  listScheduledTasks: listScheduledTasksTool,
  navigateTo,
  getPowerStatus,
};

/** 将 tools 注入 brain/task，由 scheduler 调用 */
export async function executeScheduledTask(task: ScheduledTask): Promise<string> {
  return _execTask(task, ALL_TOOLS);
}

// ── 向后兼容 re-export（已有代码 import from '@/core/cognition/tools' 不需要改动）──
export { AGENT_MODEL } from "@/core/cognition/brain";
