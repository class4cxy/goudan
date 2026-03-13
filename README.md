# Home Agent

基于树莓派 5 的具身家庭智能体。不是问答式助手，而是一个能持续感知环境、主动判断并自主行动的机器人。

---

## 整体架构

### 神经系统模型

系统以人体神经系统为蓝本，每一层对应一个生物角色：

```
┌──────────────────────────────────────────────────────────────────────┐
│                      Platform（Python FastAPI）                        │
│                                                                      │
│  Sensory 感官层（始终运行）              Action 效应器层                │
│  ┌─────────────────────────┐          ┌──────────────────┐           │
│  │ AudioSensor             │          │ AudioEffector    │ TTS       │
│  │  ├ VAD（人声检测）        │          │ MotorDriver      │ 移动      │
│  │  ├ openWakeWord（唤醒词） │          └────────▲─────────┘           │
│  │  └ YAMNet（环境声音分类） │                   │ 行动指令              │
│  │ Camera（摄像头 + 帧处理） │                   │                     │
│  └───────────┬─────────────┘                   │                     │
│              │ 离散事件（WebSocket /ws）          │                     │
└──────────────┼──────────────────────────────────┼─────────────────────┘
               │                                  │
               ▼                                  │
┌──────────────────────────────────────────────────────────────────────┐
│                        Node.js（Next.js）                             │
│                                                                      │
│  core/runtime/platform-connector   Platform事件 ↔ SpineEvent 双向适配 │
│                                │                                     │
│                  ┌─────────────▼─────────────┐                       │
│                  │           Spine           │  事件总线               │
│                  │  优先级队列 + 感知缓冲(L0)  │                       │
│                  └──┬────────────────────┬───┘                       │
│                     │                    │                           │
│   ┌─────────────────┴──┐        ┌────────▼─────────────────────┐    │
│   │  perception/ 感知层 │        │        cognition/ 认知层      │    │
│   │                    │        │                              │    │
│   │  audio/thalamus    │        │  brain/conversation          │    │
│   │   STT + 唤醒词      │        │   语音对话 LLM（流式分句）      │    │
│   │   情绪分析          │        │  brain/task                 │    │
│   │                    │        │   定时任务自主执行             │    │
│   │  chat/             │        │  brain/prompts              │    │
│   │   Web Chat → Spine │        │   人设 + Prompt 构建         │    │
│   │   侧链事件          │        │  memory/ L1~L4 分级记忆      │    │
│   │                    │        │  tools/ AI 工具注册表         │    │
│   └────────────────────┘        └──────────┬───────────────────┘    │
│                                            │                        │
│                  ┌─────────────────────────▼───────────────────┐    │
│                  │              behavior/ 行为层                 │    │
│                  │  conversation/ — 状态机（IDLE/LISTENING/      │    │
│                  │               THINKING/SPEAKING）            │    │
│                  │  motor/       — 运动行为（订阅 lidar 事件）    │    │
│                  │  scheduler/   — 自主定时任务（自主神经）       │    │
│                  └─────────────────────────────────────────────┘    │
└──────────────────────────────────────────────────────────────────────┘
```

### 各层职责

| 层 | 生物类比 | 运行位置 | 职责 |
|----|---------|---------|------|
| **Sensory** | 感觉器官 | Platform/Python | 始终运行：VAD / 唤醒词 / 环境声音分类 / 摄像头 |
| **runtime/spine** | 脊柱 | Node.js | 事件传输、优先级排队、感知缓冲(L0) |
| **runtime/platform-connector** | 传入神经 | Node.js | Platform 硬件事件 ↔ Spine 事件双向适配 |
| **perception/audio** | 丘脑（音频） | Node.js | 原始音频 → 语义事件（STT、情绪、唤醒词路由）|
| **perception/chat** | 丘脑（文字） | Node.js | Web Chat 输入 → Brain 直连流式 + Spine 侧链 |
| **cognition/brain** | 大脑皮层 | Node.js/LLM | 统一推理层：对话、任务执行、Prompt 管理 |
| **cognition/memory** | 记忆系统 | Node.js/SQLite | L1~L4 分层记忆，支持无限长对话压缩 |
| **cognition/tools** | 前额叶（能力） | Node.js | AI 可调用工具注册表（14 个工具） |
| **behavior/conversation** | 语言中枢 | Node.js | 交流能力：被动唤醒 + 主动发起，状态机 + 优先队列 |
| **behavior/motor** | 脊髓运动核 | Node.js | 运动行为：导航意图 + 电机指令转发 |
| **behavior/scheduler** | 自主神经系统 | Node.js | 定时任务引擎（静默时段感知） |
| **Action** | 效应器 | Platform/Python | 订阅行动事件，驱动 TTS / 电机等硬件 |

### 核心设计原则

1. **所有层间通信走 Spine**，模块之间互不直接依赖
2. **连续流在 Sensory 内部消化**，Spine 只传离散事件（避免总线过载）
3. **Brain 永远只发布事件**，不直接调用效应器
4. **Memory 是渠道无关的**，Web Chat / Voice / 微信共用同一套压缩机制
5. **Perception 层多通道对称**：audio 和 chat 都是感知入口，Brain 不区分来源

---

## 项目结构

```
goudan/
├── platform/                      # Python — 硬件抽象层（端口 8001）
│   ├── main.py                    # FastAPI + WebSocket /ws
│   ├── audio_sensor.py            # 麦克风采集 + VAD + 唤醒词
│   ├── audio_effector.py          # TTS + 扬声器
│   ├── lidar_sensor.py            # 激光雷达 + SLAM 广播
│   ├── devices/                   # 硬件驱动（电机/舵机/摄像头/激光雷达）
│   └── slam/                      # SLAM 算法层（breezyslam）
│
├── src/
│   ├── app/api/                   # Next.js API Routes
│   │   ├── chat/                  # 聊天接口（流式 SSE）
│   │   ├── threads/               # 对话线程 CRUD
│   │   ├── tasks/                 # 定时任务管理
│   │   └── status/                # Platform 健康检查代理
│   │
│   ├── lib/                       # 通用基础设施（无业务属性）
│   │   ├── db/                    # SQLite 数据访问层
│   │   └── utils.ts               # 通用工具函数
│   │
│   └── core/                      # Agent 神经系统核心
│       ├── runtime/               # 物理基底
│       │   ├── spine/             # 事件总线 + 感知缓冲 L0
│       │   └── platform-connector/ # Platform ↔ Spine 双向适配器
│       │
│       ├── perception/            # 多通道感知层（信号 → 语义事件）
│       │   ├── audio/             # 语音通道：丘脑（STT + 唤醒词 + 情绪）
│       │   └── chat/              # 文字通道：Web Chat → Brain + Spine 侧链
│       │
│       ├── cognition/             # 认知层（思维 · 记忆 · 能力）
│       │   ├── brain/             # LLM 推理层（对话/任务/Prompt）
│       │   ├── memory/            # 分级对话记忆 L1-L4
│       │   └── tools/             # Agent 工具注册表（14 个工具）
│       │
│       └── behavior/              # 行为层（认知 → 输出）
│           ├── conversation/      # 语言行为：状态机 + 主/被动发起
│           ├── motor/             # 运动行为：导航 + 电机指令
│           └── scheduler/         # 自主定时任务
│
└── data/
    └── home-agent.db              # SQLite 数据库（WAL 模式）
```

---

## 快速启动

### 1. 启动 Platform（Python 硬件层）

```bash
cd platform
pip install -r requirements.txt
uvicorn main:app --host 0.0.0.0 --port 8001 --reload
# 或使用 npm 脚本：
pnpm platform
```

### 2. 启动 Node.js

```bash
pnpm install
pnpm dev
```

### 3. 环境变量（`.env`）

```env
# AI 模型
DEEPSEEK_API_KEY=sk-...              # DeepSeek 主推理模型
DASHSCOPE_API_KEY=sk-...             # 通义千问 VL（视觉分析）

# 硬件
CAMERA_RTSP_URL=rtsp://...           # 摄像头 RTSP 地址
ROBOROCK_USERNAME=your@email.com     # 石头扫地机账号
ROBOROCK_REGION=cn

# Platform 服务
PLATFORM_URL=http://localhost:8001
PLATFORM_WS_URL=ws://localhost:8001/ws
```

---

## 模块文档

| 模块 | 文档 | 说明 |
|------|------|------|
| 事件总线 | [src/core/runtime/spine/README.md](src/core/runtime/spine/README.md) | Spine 事件系统、优先级、感知缓冲 |
| 记忆系统 | [src/core/cognition/memory/README.md](src/core/cognition/memory/README.md) | L0~L4 分层记忆、对话压缩算法 |
| 音频感知 | [src/core/perception/audio/README.md](src/core/perception/audio/README.md) | STT 管道、唤醒词检测、情绪分析 |
| 大脑推理 | [src/core/cognition/brain/.agent.memory.md](src/core/cognition/brain/.agent.memory.md) | AGENT_MODEL、Prompt、对话/任务推理 |
| 交流能力 | [src/core/behavior/conversation/.agent.memory.md](src/core/behavior/conversation/.agent.memory.md) | 状态机、优先队列、被动/主动对话 |
| 工具注册 | [src/core/cognition/tools/README.md](src/core/cognition/tools/README.md) | ALL_TOOLS、工具实现 |
| 硬件层 | [platform/README.md](platform/README.md) | REST 端点、WebSocket 协议、设备驱动 |
