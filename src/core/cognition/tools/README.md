# Agent — 大脑

LLM 推理层，包含模型配置、工具注册表和 Prompt 设计。

---

## 模型配置

| 用途 | 模型 | 接口 |
|------|------|------|
| 主推理（对话/决策/压缩） | DeepSeek `deepseek-chat` | `DEEPSEEK_API_KEY` |
| 视觉分析 | 通义千问 `qwen-vl-max` | `DASHSCOPE_API_KEY` |

两者均使用 OpenAI 兼容接口，可替换为其他模型。

---

## 工具注册表（15 个工具）

| 工具 | 文件 | 后端 |
|------|------|------|
| `getRobotStatus` | `roborock.ts` | HTTP → Bridge REST |
| `getRooms` | `roborock.ts` | HTTP → Bridge REST |
| `startFullCleaning` | `roborock.ts` | HTTP → Bridge REST |
| `cleanRooms` | `roborock.ts` | HTTP → Bridge REST |
| `pauseCleaning` | `roborock.ts` | HTTP → Bridge REST |
| `resumeCleaning` | `roborock.ts` | HTTP → Bridge REST |
| `returnHome` | `roborock.ts` | HTTP → Bridge REST |
| `getCleaningHistory` | `roborock.ts` | HTTP → Bridge REST |
| `takePhoto` | `camera.ts` | ffmpeg + RTSP |
| `checkCameraSetup` | `camera.ts` | 本地检测 |
| `analyzeImage` | `vision.ts` | Dashscope Qwen-VL API |
| `getInspectionHistory` | `vision.ts` | SQLite |
| `addScheduledTask` | `scheduler-tool.ts` | node-cron + SQLite |
| `listScheduledTasks` | `scheduler-tool.ts` | SQLite |
| `navigateTo` | `motor.ts` | **Spine.publish**（唯一走 Spine 的工具）|

`navigateTo` 是架构上的特例：它不直接调用硬件，而是发布 `action.navigate` 事件到 Spine，保持 Agent 与电机执行的解耦。

---

## Prompt 设计

### 对话 Prompt（`buildSystemPrompt`）

```
角色定义（Aria）+ 能力说明 + 行为准则 + 房间称呼规范 + 清扫决策规则
  + 当前时间（动态注入）
  + [历史对话摘要]（ConversationBuffer 注入，有历史时追加）
```

### 定时任务 Prompt（`SCHEDULER_PROMPT`）

更精简的 4 步执行提示：拍照 → 分析 → 按需清扫 → 输出报告。不包含历史摘要（定时任务是单次自主执行）。

---

## 对话执行

### Web Chat（流式 SSE）

```typescript
streamText({
  model: AGENT_MODEL,
  system: buildSystemPrompt(historyContext),  // 含历史压缩摘要
  messages: rawTailMessages,                  // 最近 20 条完整原文
  tools: ALL_TOOLS,
  stopWhen: stepCountIs(8),                   // 最多 8 步工具调用链
})
```

### 定时任务（自主执行）

```typescript
generateText({
  model: AGENT_MODEL,
  system: SCHEDULER_PROMPT,
  prompt: `定时任务触发：${task.name}...`,
  tools: ALL_TOOLS,
  stopWhen: stepCountIs(6),
})
```

---

## LLM Context 组装（完整视图）

```
system prompt
  ├─ Aria 角色定义与能力说明
  ├─ 当前上海时间（buildSystemPrompt 动态注入）
  └─ [历史对话摘要，线性权重衰减]  ← ConversationBuffer.assembleHistoryContext()

messages[]
  └─ rawTail：最近 20 条未压缩消息  ← ConversationBuffer.getRawTail()

tools
  └─ ALL_TOOLS（15 个工具）
```
