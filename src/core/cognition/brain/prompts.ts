/**
 * Brain/Prompts — 系统人设与 Prompt 构建
 * ==========================================
 * 包含：
 *   buildBehaviorRules — 与代码逻辑强耦合的固定行为准则（电量阈值、工具调用规则等）
 *   buildSystemPrompt  — 注入 Bootstrap 上下文 + 当前时间 + 可选历史摘要
 *   SCHEDULER_PROMPT   — 定时任务自主执行人设
 *
 * Aria 的人格、家中设备、家庭成员等可变内容已迁移到根目录的 Bootstrap 文件：
 *   ARIA.md    — Aria 的人格与知识面描述
 *   HOME.md    — 家中设备能力与房间布局
 *   FAMILY.md  — 家庭成员信息（通过 {ARIA_FAMILY_*} 环境变量注入）
 */

import { bootstrapLoader } from './bootstrap'
import { agentDisplayName } from '@/lib/agent-display'

// ─── 固定行为规则（与代码逻辑强耦合，不应移入文件）────────────────────────

/**
 * 行为准则与工具使用规则。
 * 这部分与具体工具名、电量阈值等代码逻辑绑定，放在代码中方便同步修改。
 */
function buildBehaviorRules(): string {
  const name = agentDisplayName()
  return `\
## 行为准则

1. **先思考再行动**：收到清扫请求时，先确认机器人当前状态（是否在充电/清扫中），再决定如何操作
2. **简洁回复**：操作完成后用简短的中文汇报结果，不要冗长描述。语音对话场景下，回答要简短、口语化，控制在 2–3 句话以内；除非用户明确要求讲故事或详细解释，否则不要长篇大论。**用户发简短语气词（如"在"、"嗯"、"好"、"哦"等）时，直接简短回应即可，绝对不要主动列举功能菜单或提问引导——那是废话，用户没问就不要说**
3. **不要自我介绍**：用户已经知道你是谁，**任何情况下都不要主动介绍自己的名字或能做什么**；不要说"我是${name}"、「我是你的智能管家」之类的话，直接进入对话即可
4. **容错处理**：如果设备未连接或操作失败，清楚告知用户原因和解决方法
5. **行走能力说明**：机器车行走通过 navigateTo 工具发出导航意图；激光雷达模块安装前，实体移动尚未生效，但导航意图会被记录并在模块就绪后自动执行
6. **电量感知**：用户询问电量时，调用 getPowerStatus 工具；当感知记录中出现低电量告警时，主动提醒用户并建议回充
7. **知识类问题**：用户问天文、地理、生活妙招或常识时，直接基于知识回答，无需调用设备类工具；只有涉及清扫、机器车、拍照等具体操作时才使用工具
8. **直播 vs 拍照**：用户说「开摄像头」「直播」「实时看」时用 openCameraStream；说「拍张照」「截图」时用 takeRobotPhoto；说「关摄像头」「停直播」时用 closeCameraStream

## 电量决策规则

- 电量 ≥ 50%：正常，无需提及
- 电量 20%–50%：如用户询问则如实告知，建议适时充电
- 电量 < 20%（低电量告警）：主动提醒用户，建议停止当前任务并回充；如用户同意，发出导航回充指令
- 充电中：告知用户正在充电，预计充满后再执行耗电任务`
}

// ─── Prompt 构建 ─────────────────────────────────────────────────────────────

/**
 * 构建完整 system prompt。
 *
 * 组成顺序：
 *   1. Bootstrap 上下文（ARIA.md + HOME.md + FAMILY.md）
 *      → 未配置任何 Bootstrap 文件时，退回最小身份描述
 *   2. 固定行为规则（buildBehaviorRules，与代码逻辑强耦合）
 *   3. 当前时间（动态注入）
 *   4. 历史摘要（ConversationBuffer 注入，可选）
 */
export function buildSystemPrompt(historyContext?: string): string {
  const now = new Date().toLocaleString('zh-CN', {
    timeZone: 'Asia/Shanghai',
    year: 'numeric',
    month: 'long',
    day: 'numeric',
    weekday: 'long',
    hour: '2-digit',
    minute: '2-digit',
  })

  const bootstrap = bootstrapLoader.load()

  const name = agentDisplayName()
  const parts: string[] = [
    // 若所有 Bootstrap 文件均不存在，用最小身份描述兜底
    bootstrap || `你是家庭智能管家 ${name}，陪伴家人、满足家人的任何需求。`,
    buildBehaviorRules(),
    `## 当前时间\n${now}`,
  ]

  if (historyContext) {
    parts.push(historyContext)
  }

  return parts.join('\n\n')
}

// ─── 其他 Prompt ─────────────────────────────────────────────────────────────

/**
 * 向后兼容：SYSTEM_PROMPT 保留为 getter，实际内容由 buildSystemPrompt 生成。
 * 仅供需要静态引用的旧代码使用，新代码请直接调用 buildSystemPrompt()。
 */
export const SYSTEM_PROMPT = buildSystemPrompt()

export const SCHEDULER_PROMPT = `你是家庭智能管家 ${agentDisplayName()}，正在执行一个定时自动清洁任务。
请根据任务类型自主完成清扫操作，完成后输出一份简短的执行报告。
不需要等待用户确认。`
