# Spine — 事件总线

系统的神经中枢，所有模块间通信的唯一通路。任何模块都不直接调用其他模块，只通过 Spine 发布和订阅事件。

---

## 核心 API

```typescript
import { Spine } from '@/lib/spine'

// 发布事件（summary 必填，自动写入感知缓冲）
Spine.publish({
  type: 'sense.audio.transcript',
  priority: 'MEDIUM',
  source: 'audio-thalamus',
  payload: { text: '帮我扫地', duration: 1800 },
  summary: '用户说：帮我扫地',
})

// 订阅事件（返回取消订阅函数）
const unsub = Spine.subscribe(['sense.audio.transcript'], (event) => {
  console.log(event.payload)
})
unsub()  // 取消订阅

// 读取感知缓冲（L0 记忆）
Spine.getWorkingMemory(5 * 60_000)   // 最近 5 分钟
Spine.formatMemoryForLLM()            // 格式化为 LLM 可读文本
```

---

## 事件优先级

| 优先级 | 级别 | 分发方式 | 适用场景 |
|--------|------|---------|---------|
| `CRITICAL` | 0 | `Promise.resolve()`（最快） | 摔倒、火灾、紧急安全事件 |
| `HIGH` | 1 | `setImmediate()` | 婴儿哭声、关键词唤醒、异常检测 |
| `MEDIUM` | 2 | `setImmediate()` | 语音转写、导航指令 |
| `LOW` | 3 | `setImmediate()` | 背景环境感知、状态心跳 |

---

## 事件类型速查

### 感知事件（`sense.*`）

| 事件类型 | 优先级 | 来源 | 说明 |
|---------|-------|------|------|
| `sense.audio.speech_start` | LOW | Bridge/AudioSensor | VAD 检测到说话开始 |
| `sense.audio.speech_end` | MEDIUM | Bridge/AudioSensor | 说话结束，含 PCM base64 |
| `sense.audio.transcript` | MEDIUM | AudioThalamus | Whisper STT 转写结果 |
| `sense.audio.emotion` | LOW/HIGH | AudioThalamus | 情绪分析结果 |
| `sense.audio.keyword` | HIGH | AudioThalamus | 关键词命中（呼叫名字等） |
| `sense.video.fall` | CRITICAL | Bridge/Camera | 摔倒检测 |
| `sense.video.anomaly` | HIGH | Bridge/Camera | 视觉异常 |
| `sense.video.person` | MEDIUM | Bridge/Camera | 检测到人 |
| `sense.system.battery` | LOW | Bridge | 电池电量状态 |
| `sense.system.obstacle` | HIGH | Bridge/Motor | 障碍物检测 |

### 调度事件（`schedule.*`）

| 事件类型 | 优先级 | 来源 | 说明 |
|---------|-------|------|------|
| `schedule.trigger` | HIGH | Scheduler | 定时任务触发 |

### 行动事件（`action.*`）

| 事件类型 | 优先级 | 来源 | 说明 |
|---------|-------|------|------|
| `action.speak` | HIGH | Brain | 语音播报指令 |
| `action.navigate` | MEDIUM | Brain | 移动导航指令 |
| `action.motor` | MEDIUM | Brain/Navigation | 底层电机控制 |
| `action.capture` | MEDIUM | Brain | 摄像头拍照 |
| `action.patrol` | LOW | Brain/Scheduler | 巡逻任务 |
| `action.notify` | HIGH | Brain | 微信/SMS 通知 |
| `action.ignore` | LOW | Brain | 主动忽略事件 |

完整类型定义见 `types.ts`。

---

## 感知缓冲（L0 记忆）

每次 `Spine.publish()` 自动将 `summary` 字段写入 `workingMemory[]`：

```
容量：最近 10 分钟 或 300 条，取先到者
存储：纯 RAM（进程重启清空）
格式：{ timestamp, type, priority, source, summary }
```

`formatMemoryForLLM()` 输出示例：

```
14:32:10 [HIGH]  检测到有人开始说话，时长 2300ms
14:32:14         语音转写完成：「帮我扫地」，情绪：平静
14:33:01         摄像头拍照完成
14:33:05         机器车收到导航指令：前往客厅
```

---

## 单例模式

Spine 以 `globalThis.__spine` 存储单例，在 Next.js 热重载时不会重复初始化：

```typescript
export const Spine = globalThis.__spine ?? (globalThis.__spine = new SpineClass())
```
