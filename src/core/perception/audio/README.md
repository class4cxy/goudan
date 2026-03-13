# Audio — 音频模块

负责语音输入的感知理解（Thalamus）和语音输出的调度（Effector）。

---

## 数据流

```
Bridge AudioSensor
  │ PCM base64 块（VAD 切分后）
  ▼
Spine: sense.audio.speech_end
  │
  ▼
AudioThalamus（订阅者）
  ├─ 丢弃 < 400ms 的短音频（噪声过滤）
  ├─ Whisper STT → sense.audio.transcript
  └─ 关键词规则 → sense.audio.emotion / sense.audio.keyword
  
[Brain 订阅 transcript → 推理 → action.speak]  ← 尚未接通

Spine: action.speak
  │
  ▼
AudioEffector（订阅者）
  │
  ▼
BridgeConnector.send(action.speak)
  │ WebSocket
  ▼
Bridge AudioEffector → edge-tts → 扬声器
```

---

## AudioThalamus

文件：`thalamus.ts`

**STT 配置：**
- 模型：Whisper（通过 OpenAI 兼容接口）
- 最小音频时长：400ms（`MIN_DURATION_MS`），过短直接丢弃
- 输入格式：PCM base64 → WAV Buffer → Whisper API

**情绪分析：**
- 基于关键词规则（非模型推理），与 STT 并行执行
- 检测到哭声 / 呼救词 → `HIGH` 优先级情绪事件
- 普通对话 → `LOW` 优先级

**已知缺口：**
`AudioThalamus` 发布 `sense.audio.transcript` 后，没有订阅者将其送入 LLM。Voice → Brain 的链路尚未接通，是当前最优先的待实现功能。

---

## AudioEffector

文件：`effector.ts`

订阅 `action.speak` 事件，将文本通过 `BridgeConnector.send()` 转发给 Bridge，由 Bridge 调用 edge-tts 合成并播放。

---

## 模块启动

文件：`index.ts`

```typescript
import { startAudio } from '@/lib/audio'
startAudio()  // 启动 Thalamus 订阅 + Effector 订阅
```

在 Next.js 服务端初始化时调用一次，之后持续监听 Spine 事件。
