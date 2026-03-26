/**
 * 助手对外显示名（唤醒词兜底、UI、STT 词表、压缩上下文中的助手角色名等）。
 *
 * 配置：进程环境变量 `AGENT_DISPLAY_NAME`；未设置或仅为空白时默认为「狗蛋」。
 */
export const DEFAULT_AGENT_DISPLAY_NAME = "狗蛋";

export function agentDisplayName(): string {
  const v = process.env.AGENT_DISPLAY_NAME?.trim();
  return v && v.length > 0 ? v : DEFAULT_AGENT_DISPLAY_NAME;
}
