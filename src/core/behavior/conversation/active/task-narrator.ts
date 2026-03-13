/**
 * TaskNarrator — 任务表达接口
 * ==============================
 * 供业务层（agent tools、scheduler 等）调用，让 agent 在执行任务时主动说话。
 *
 * 使用方式：
 *   import { narrate, narrateIssue } from '@/core/behavior/conversation/active/task-narrator'
 *   narrate('好的，我现在去关客厅的灯')
 *   narrateIssue('扫地机器人', '设备离线，无法执行清扫')
 */

import { ConversationManager } from '../manager'
import { Spine } from '../../../runtime/spine'
import type { AgentTaskIssuePayload } from '../../../runtime/spine'

/**
 * 让 agent 主动说一句话（任务进度通报、操作结果汇报等）。
 *
 * @param text     要说的内容
 * @param priority 2=正常任务通报，3=低优先级闲聊
 */
export function narrate(text: string, priority: 2 | 3 = 2): void {
  ConversationManager.narrate(text, {
    priority,
    label: `narrate.${Date.now()}`,
    triggerNote: '正在执行任务，需要向用户汇报',
  })
}

/**
 * 任务执行遇到问题时调用，发布 sense.agent.task_issue 并入队告知用户。
 *
 * @param taskName   任务名称
 * @param issue      问题描述
 * @param suggestion 可选：建议用户的应对措辞
 */
export function narrateIssue(taskName: string, issue: string, suggestion?: string): void {
  const payload: AgentTaskIssuePayload = { task_name: taskName, issue, suggestion }

  Spine.publish({
    type: 'sense.agent.task_issue',
    priority: 'MEDIUM',
    source: 'task.narrator',
    payload,
    summary: `任务「${taskName}」遇到问题：${issue}`,
  })

  const text = suggestion
    ? `执行「${taskName}」时遇到问题：${issue}。${suggestion}`
    : `执行「${taskName}」时遇到问题：${issue}`

  ConversationManager.enqueue({
    mode: 'active',
    priority: 2,
    source: 'task.issue',
    label: `task.issue.${taskName}`,
    content: text,
    triggerNote: `任务执行异常：${taskName}`,
    expiresAt: Date.now() + 5 * 60_000,  // 5 分钟内未处理则过期
  })
}
