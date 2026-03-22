// ─── 优先级 ──────────────────────────────────────────────────────────────────

export type EventPriority = 'CRITICAL' | 'HIGH' | 'MEDIUM' | 'LOW'

export const PRIORITY_RANK: Record<EventPriority, number> = {
  CRITICAL: 0, // 老人摔倒、火灾、紧急呼救 → 立即中断一切
  HIGH: 1,     // 孩子哭泣、异常声音、巡检到点
  MEDIUM: 2,   // 对话值得介入、主人呼唤、房间异常
  LOW: 3,      // 背景环境感知、上下文积累
}

// ─── 事件类型命名空间 ─────────────────────────────────────────────────────────
// 格式：<层级>.<来源>.<信号>

export type SpineEventType =
  // 感官层 → Thalamus 过滤后上报
  | 'sense.audio.speech_start'    // VAD 检测到有人开始说话
  | 'sense.audio.speech_end'      // 说话停止（可以转写了）
  | 'sense.audio.speak_end'       // TTS 所有句子播放完毕（Platform 回调，触发下一轮倾听）
  | 'sense.audio.transcript'      // 语音转文字完成
  | 'sense.audio.emotion'         // 情绪信号（平静/激动/哭泣/争吵）
  | 'sense.audio.keyword'         // 关键词触发（呼叫机器人名字等）
  | 'sense.video.person'          // 检测到人
  | 'sense.video.fall'            // 摔倒检测
  | 'sense.video.anomaly'         // 视觉异常（碎玻璃、火焰、烟雾等）
  | 'sense.video.room_snapshot'   // 巡检时拍下的房间快照（含 VLM 分析结果）
  | 'sense.system.battery'        // 机器车电池电量变化（INA219 低电量告警）
  | 'sense.system.location'       // 机器人位置变化
  | 'sense.system.obstacle'       // 行进中遇到障碍
  | 'sense.audio.environment'     // 环境声音分类（门铃/哭声/报警/狗叫等，YAMNet）
  | 'sense.agent.idle'            // agent 空闲超时，可发起主动对话
  | 'sense.agent.task_issue'      // 任务执行遇到问题，需要告知用户
  | 'sense.conversation.interest' // 旁听分析：内容值得插话
  | 'sense.chat.message'          // Web Chat 文字输入（感知侧链事件）
  | 'sense.chat.complete'         // Web Chat 对话轮次完成

  // 调度器触发
  | 'schedule.trigger'            // 定时任务到点（巡检、提醒等）

  // 大脑决策结果 → Dispatcher 分发给效应器
  | 'action.speak'                // 语音播报
  | 'action.navigate'             // 移动到目标位置（高层意图，由 MotorEffector / NavigationThalamus 处理）
  | 'action.motor'                // 底层电机指令（由 MotorEffector 内部使用，不直接暴露给 Agent）
  | 'action.notify'               // 发送通知（微信/SMS）
  | 'action.capture'              // 拍照/录像
  | 'action.patrol'               // 开始巡检路线
  | 'action.ignore'               // 显式决策：本次不介入
  | 'action.explore'              // 自主建图探索控制（start/stop）

// ─── Payload 类型定义 ─────────────────────────────────────────────────────────

export interface AudioKeywordPayload {
  keyword: string           // 命中的唤醒词（如 "小豆"、"Aria"）
  transcript: string        // 包含唤醒词的完整句子
  duration_ms: number
}

export interface AudioAmbientChunkPayload {
  text: string              // 旁听转写文字（未命中唤醒词的闲聊/背景对话）
  duration_ms: number
}

export interface AudioEnvironmentPayload {
  label: string             // YAMNet 分类标签，如 "doorbell" / "baby_crying" / "smoke_detector"
  confidence: number        // 0-1
  category: 'alert' | 'activity' | 'ambient'
}

export interface AgentIdlePayload {
  idle_since_ms: number     // 上次对话活动距今毫秒数
}

export interface AgentTaskIssuePayload {
  task_name: string
  issue: string             // 问题描述（自然语言）
  suggestion?: string       // 可选：建议告知用户的应对措辞
}

export interface ConversationInterestPayload {
  context_snippet: string   // 触发兴趣的旁听对话片段
  interest_score: number    // 0-10
  suggested_reply: string   // LLM 建议的插话内容
}

export interface AudioSpeechEndPayload {
  audio_b64: string              // base64 编码的原始 PCM 数据
  sample_rate: number            // 采样率，固定 16000Hz
  duration_ms: number            // 语音时长（含尾部静音）
  trace_id?: string              // Platform 生成的 8 位追踪 ID，贯穿整个 STT 流程
  platform_vad_flush_ms?: number // Platform 麦克风 VAD 质量门控耗时（可选）
}

export interface AudioSpeakEndPayload {
  // TTS 全部句子播完，Platform 发回此事件触发 ConvManager 重新进入 LISTENING
}

export interface AudioTranscriptPayload {
  text: string
  duration_ms: number
  speaker_id?: string           // 未来可接说话人识别
  language?: string
}

export interface AudioEmotionPayload {
  emotion: 'calm' | 'excited' | 'crying' | 'arguing' | 'unknown'
  confidence: number            // 0-1
  text_snippet?: string         // 触发情绪判断的文字片段
}

export interface VideoFallPayload {
  room: string
  confidence: number
  snapshot_path?: string        // 本地截图路径
}

export interface VideoAnomalyPayload {
  room: string
  anomaly_type: string          // 'broken_glass' | 'smoke' | 'water' | ...
  description: string           // 供 LLM 理解的自然语言描述
  confidence: number
  snapshot_path?: string
}

export interface VideoRoomSnapshotPayload {
  room: string
  snapshot_path: string
  vlm_analysis: string          // 视觉大模型对房间卫生/状态的描述
  issues: string[]              // 发现的具体问题列表
}

export interface ScheduleTriggerPayload {
  task_id: string
  task_name: string
  cron: string
  params?: Record<string, unknown>
}

export interface ActionSpeakPayload {
  text: string
  interrupt_current?: boolean   // 是否打断当前正在播放的语音
}

export interface ActionNavigatePayload {
  destination: string           // 房间名或坐标（待激光雷达地图支持后生效）
  reason?: string
}

export interface ActionMotorPayload {
  command: 'forward' | 'backward' | 'turn_left' | 'turn_right' | 'stop'
  speed?: number                // 0–100，不传使用 Bridge 默认速度（CHASSIS_DEFAULT_SPEED，默认 35）
  duration?: number             // 秒，不传=持续运动直到下一条指令（由 NavigationThalamus 控制）
}

export interface ActionNotifyPayload {
  channel: 'wechat' | 'sms' | 'both'
  message: string
  attachments?: string[]        // 图片路径列表
}

export interface ActionCapturePayload {
  reason: string
  save_path?: string
}

export interface SystemBatteryPayload {
  voltage_v: number        // 总线电压（V）
  current_ma: number       // 电流（mA，正=放电，负=充电）
  power_mw: number         // 功率（mW）
  battery_pct: number      // 剩余电量（%）
  is_charging: boolean     // 是否正在充电
  is_low: boolean          // 是否低电量（< 阈值）
  threshold_pct: number    // 告警阈值（%）
  message: string          // 人类可读描述
}

export interface ActionPatrolPayload {
  rooms: string[]               // 巡检房间顺序
  triggered_by: 'schedule' | 'anomaly' | 'manual'
}

// ─── 核心事件结构 ─────────────────────────────────────────────────────────────

export interface SpineEvent<T = unknown> {
  id: string
  type: SpineEventType
  priority: EventPriority
  source: string                // 哪个模块发布（'thalamus.audio' | 'scheduler' | 'brain' 等）
  payload: T
  timestamp: number
  metadata?: Record<string, unknown>
}

// ─── 工作记忆条目（供 LLM 消费的格式化摘要）────────────────────────────────────

export interface MemoryEntry {
  timestamp: number
  type: SpineEventType
  priority: EventPriority
  source: string
  summary: string               // 人类可读的一句话摘要，由发布方填写
}

export type EventHandler<T = unknown> = (event: SpineEvent<T>) => void | Promise<void>

export type Unsubscribe = () => void
