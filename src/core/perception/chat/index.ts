/**
 * perception/chat — 文字对话感知通道
 * ======================================
 * 职责：
 *   - 作为 Web Chat（HTTP 文字输入）的感知入口，对称于 perception/audio/
 *   - 接收文字输入后向 Spine 发布 sense.chat.message 事件（侧链通知）
 *   - 供 app/api/chat/route.ts 调用，共享上下文构建逻辑
 *
 * 与语音通道的区别：
 *   - 语音：异步事件驱动（VAD → STT → Spine → Brain）
 *   - 文字：同步请求响应（HTTP POST → 直连 Brain 流式 → SSE 返回前端）
 *           同时通过 Spine 侧链发布事件，让其他模块感知对话活动
 *
 * 设计原则：
 *   - Brain 收到的输入格式与语音通道一致（都是文本），Brain 无需区分来源
 *   - 侧链事件用于 ConversationManager 状态同步、Memory 记录等
 */

import { Spine } from '../../runtime/spine'

/**
 * 通知 Spine 有文字对话活动（侧链事件，不阻塞主响应流）。
 *
 * 在 app/api/chat/route.ts 开始流式推理前调用，让其他模块（如
 * ConversationManager、AmbientAnalyzer）感知到用户正在通过 Web Chat 交互。
 */
export function notifyChatInput(text: string, threadId?: string): void {
  Spine.publish({
    type: 'sense.chat.message',
    priority: 'MEDIUM',
    source: 'chat',
    payload: { text, threadId },
    summary: `Web Chat 输入：${text.slice(0, 50)}${text.length > 50 ? '…' : ''}`,
  })
}

/**
 * 通知 Spine 文字对话已完成（Brain 推理结束）。
 * 可供 ConversationManager 重置空闲计时器。
 */
export function notifyChatComplete(threadId?: string): void {
  Spine.publish({
    type: 'sense.chat.complete',
    priority: 'LOW',
    source: 'chat',
    payload: { threadId },
    summary: 'Web Chat 对话轮次完成',
  })
}
