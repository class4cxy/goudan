/**
 * agent/prompts.ts — 向后兼容 re-export
 * =========================================
 * 实际内容已移至 src/lib/brain/prompts.ts。
 * 保留此文件，让现有 import from '@/core/cognition/tools/prompts' 不需要改动。
 */
export { buildSystemPrompt, SYSTEM_PROMPT, SCHEDULER_PROMPT } from "@/core/cognition/brain/prompts";
