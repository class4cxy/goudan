# Memory — 记忆系统

Agent 的分层记忆管理模块，解决多渠道（Web Chat / Voice / 微信）下无限增长的对话历史如何高效存储、检索和注入 LLM 上下文的问题。

---

## 记忆分层模型（L0~L4）

```
L4  程序性记忆    工具注册表（代码）         进程生命周期    每次 LLM 调用作为 tools 传入
L3  语义事实      SQLite facts 表           30天半衰期     每次调用注入（高置信度条目）
L2  片段记忆      SQLite episodes 表        7天半衰期      FTS5 相关性召回 + 最近 N 条
L1  渠道会话      SQLite thread_messages    会话生命周期    当前渠道消息历史（rawTail）
L0  感知缓冲      RAM（Spine workingMemory） 10分钟滑动窗口  每次调用注入 system prompt
```

### L0 — 感知缓冲（Spine Working Memory）

所有模块通过 `Spine.publish({ summary })` 自动写入，无需额外操作。包含近 10 分钟内所有传感器事件的时间线（语音检测、马达动作、视觉事件等）。

读取方式：
```typescript
import { Spine } from '@/lib/spine'
const context = Spine.formatMemoryForLLM()  // 返回格式化文本
```

### L1 — 渠道会话（ConversationBuffer）

每个 thread 对应一个 `ConversationBuffer` 实例：

- **rawTail**：未被任何 chunk 覆盖的最新消息（最多 20 条），直接作为 LLM messages 传入
- **chunks**：已压缩的历史片段，注入 system prompt（线性权重衰减）

渠道差异通过 session timeout 控制，压缩算法本身渠道无关：

| 渠道 | Session 策略 |
|------|-------------|
| Web Chat | 用户手动创建/切换 thread |
| Voice | 30 分钟无声自动关闭，下次开口创建新 thread |
| 微信 | 单一永久 thread，无会话边界 |

### L2 — 片段记忆（Episodes）

*计划中，尚未实现。*

会话结束后由 LLM 生成 ~150 字摘要，写入 `episodes` 表，支持 FTS5 全文检索。实现跨渠道连贯性（在微信提到 Web Chat 里安排的任务）。

### L3 — 语义事实（Facts）

*计划中，尚未实现。*

从对话中提炼的持久性事实（用户偏好、家庭信息、行为规律），带置信度分数，周期性衰减与更新。

---

## ConversationBuffer

核心类，位于 `conversation-buffer.ts`。

### 压缩触发规则

```
rawTail 消息数 > 20 条
    │
    ▼
每 10 条刷出 → LLM 压缩 → level-1 chunk（写 DB）
    │
level-1 chunks 累积 ≥ 5 个
    │
    ▼
合并 → level-2 chunk（50 条消息 → 1 段摘要）
    │
level-2 chunks 累积 ≥ 5 个
    │
    ▼
合并 → level-3 chunk（250 条消息 → 1 段摘要）
```

### 线性权重上下文组装

chunks 注入 system prompt 时，越新的 chunk 分配越多 token：

```
共 n 个 chunk，第 i 个（从旧到新，i=1..n）权重 = i
token 预算 = (i / (n*(n+1)/2)) × 1500

示例（5 个 chunk）：
  chunk 1（最旧）：  6.7%  → ~100 tokens
  chunk 2        ： 13.3%  → ~200 tokens
  chunk 3        ： 20.0%  → ~300 tokens
  chunk 4        ： 26.7%  → ~400 tokens
  chunk 5（最新）： 33.3%  → ~500 tokens
```

### API

```typescript
const buffer = new ConversationBuffer(threadId)

// 同步 - 请求路径上可直接调用
buffer.getChunks()              // 获取所有 chunks
buffer.getRawTail(messages)     // 截取未压缩的尾部消息
buffer.assembleHistoryContext() // 生成注入 system prompt 的摘要文本

// 异步 - 在 onFinish 里 fire-and-forget
buffer.maybeCompress(allMessages).catch(console.error)
```

---

## 数据库 Schema

```sql
CREATE TABLE conversation_chunks (
  id            TEXT PRIMARY KEY,
  thread_id     TEXT NOT NULL,
  level         INTEGER NOT NULL DEFAULT 1,   -- 压缩代数：1/2/3
  message_count INTEGER NOT NULL,             -- 覆盖的原始消息数
  covers_from   INTEGER NOT NULL,             -- 最早消息时间戳（ms）
  covers_to     INTEGER NOT NULL,             -- 最晚消息时间戳（ms）
  summary       TEXT NOT NULL,               -- LLM 生成的压缩摘要
  token_count   INTEGER NOT NULL DEFAULT 0,  -- 摘要的估算 token 数
  created_at    INTEGER NOT NULL DEFAULT (unixepoch()),
  FOREIGN KEY (thread_id) REFERENCES threads(id) ON DELETE CASCADE
);

-- 计划中：
-- CREATE TABLE episodes (...)   -- L2 片段记忆
-- CREATE TABLE facts (...)      -- L3 语义事实
-- CREATE VIRTUAL TABLE episodes_fts USING fts5(...)  -- 全文检索
```

---

## 与 /api/chat 的集成

```
POST /api/chat
  │
  ├─ new ConversationBuffer(threadId)
  ├─ assembleHistoryContext()   → 注入 system prompt
  ├─ getRawTail(messages)       → 传给 LLM 的 messages
  │
  └─ onFinish（LLM 回复完成后）
       ├─ 保存完整消息到 thread_messages（供前端展示）
       └─ maybeCompress(allMessages)  [fire-and-forget]
```

注意：`thread_messages` 存储完整历史供前端展示，`conversation_chunks` 存储压缩摘要供 LLM 使用，两者独立，互不影响。
