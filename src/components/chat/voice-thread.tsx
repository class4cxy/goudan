"use client";

/**
 * VoiceThread — 纯语音交互界面（移动端优化）
 * ============================================
 * - 按住麦克风按钮开始录音，松开自动转文字并发送
 * - 默认外放模式（蓝牙 TTS），不可关闭
 * - 保留完整对话历史，样式简洁
 */

import {
  ThreadPrimitive,
  MessagePrimitive,
  ActionBarPrimitive,
  BranchPickerPrimitive,
  useMessagePartText,
  useAui,
  useAuiState,
  type ToolCallMessagePartComponent,
} from "@assistant-ui/react";
import {
  RobotStatusToolUI,
  CleanRoomsToolUI,
  FullCleanToolUI,
  PauseToolUI,
  ResumeToolUI,
  ReturnHomeToolUI,
  GetRoomsToolUI,
  AddTaskToolUI,
  ListTasksToolUI,
  CleaningHistoryToolUI,
  TakeRobotPhotoToolUI,
  MoveCameraMountToolUI,
  CenterCameraMountToolUI,
  StartExploringToolUI,
  StopExploringToolUI,
  GetMapStatusToolUI,
  GetMapImageToolUI,
} from "@/components/chat/tool-uis";
import {
  BotIcon,
  UserIcon,
  ChevronUpIcon,
  ChevronDownIcon,
  CopyIcon,
  MicIcon,
  Volume2Icon,
  ArrowLeftIcon,
  ChevronDownIcon as ScrollDownIcon,
} from "lucide-react";
import { cn } from "@/lib/utils";
import { useCallback, useEffect, useRef, useState } from "react";
import Link from "next/link";

// ── MediaRecorder helpers ─────────────────────────────────────────
function getSupportedMimeType(): string {
  const types = [
    "audio/webm;codecs=opus",
    "audio/webm",
    "audio/ogg;codecs=opus",
    "audio/mp4",
  ];
  for (const t of types) {
    if (typeof MediaRecorder !== "undefined" && MediaRecorder.isTypeSupported(t)) return t;
  }
  return "";
}

/** 若仍有 live 的音频轨则复用，避免同页反复 getUserMedia（家庭内网可减少授权打扰） */
function reuseLiveMicStream(existing: MediaStream | null): MediaStream | null {
  if (!existing) return null;
  const live = existing.getAudioTracks().filter((t) => t.readyState === "live");
  return live.length > 0 ? existing : null;
}

// ── Tool fallback ─────────────────────────────────────────────────
const FallbackToolUI: ToolCallMessagePartComponent = ({ toolName, status }) => {
  const running = status.type === "running" || status.type === "requires-action";
  return (
    <div className="my-1.5 flex items-center gap-2 rounded-lg border border-border bg-zinc-900 px-3 py-1.5 text-xs text-muted-foreground">
      <span className={cn("h-1.5 w-1.5 rounded-full", running ? "bg-yellow-400 animate-pulse" : "bg-emerald-500")} />
      <span className="font-mono">{toolName}</span>
      {running && <span>执行中…</span>}
    </div>
  );
};

// ── 语音气泡：根据文字长度换算宽度（类似微信，无最长限制）
function VoiceBubble({ text }: { text: string }) {
  // 按字符数换算气泡宽度，约 5px/字，最小 56px；上限仅防布局溢出，语义上无最长限制
  const widthPx = Math.max(56, Math.min(280, 56 + text.length * 5));
  return (
    <div
      className="flex items-center gap-2 rounded-2xl rounded-br-sm bg-primary px-3.5 py-2.5 text-primary-foreground"
      style={{ width: widthPx }}
    >
      <div className="flex-1 flex items-center justify-end gap-1 min-w-0">
        {/* 简易波形条，宽度随文字长度变化 */}
        <span className="h-2 w-1 rounded-full bg-primary-foreground/50" />
        <span className="h-2.5 w-1 rounded-full bg-primary-foreground/70" />
        <span className="h-2 w-1 rounded-full bg-primary-foreground/50" />
        <span className="h-3 w-1 rounded-full bg-primary-foreground/90" />
        <span className="h-2 w-1 rounded-full bg-primary-foreground/50" />
      </div>
      <MicIcon className="h-4 w-4 shrink-0 opacity-90" />
    </div>
  );
}

// ── 用户消息中的 Text 部件：用语音气泡替代原文（需在 MessagePart 上下文中用 hook 取文本）
function UserMessageTextPart() {
  const part = useMessagePartText();
  const text = part && "text" in part ? part.text : "";
  return <VoiceBubble text={text} />;
}

// ── Messages ──────────────────────────────────────────────────────
function UserMessage() {
  return (
    <MessagePrimitive.Root className="flex justify-end gap-2 px-3 py-1.5">
      <div className="max-w-[80%] min-w-0">
        <MessagePrimitive.Parts
          components={{
            Text: UserMessageTextPart,
          }}
        />
      </div>
      <div className="flex h-7 w-7 shrink-0 items-center justify-center rounded-full bg-zinc-800">
        <UserIcon className="h-3.5 w-3.5 text-zinc-400" />
      </div>
    </MessagePrimitive.Root>
  );
}

function AssistantMessage() {
  return (
    <MessagePrimitive.Root className="flex gap-2 px-3 py-1.5">
      <div className="flex h-7 w-7 shrink-0 items-center justify-center rounded-full bg-primary/20">
        <BotIcon className="h-3.5 w-3.5 text-primary" />
      </div>
      <div className="flex-1 min-w-0">
        <div className="prose prose-sm prose-invert max-w-none text-sm leading-relaxed">
          <MessagePrimitive.Parts
            components={{
              Text: ({ text }) => <p className="mb-1.5 last:mb-0 whitespace-pre-wrap">{text}</p>,
              tools: { Fallback: FallbackToolUI },
            }}
          />
        </div>
        <ActionBarPrimitive.Root
          hideWhenRunning
          autohide="not-last"
          className="mt-0.5 flex items-center gap-1 opacity-0 transition-opacity data-[visible]:opacity-100"
        >
          <ActionBarPrimitive.Copy asChild>
            <button className="rounded p-1 text-muted-foreground hover:bg-accent hover:text-foreground">
              <CopyIcon className="h-3 w-3" />
            </button>
          </ActionBarPrimitive.Copy>
        </ActionBarPrimitive.Root>
        <BranchPickerPrimitive.Root
          hideWhenSingleBranch
          className="mt-0.5 flex items-center gap-1 text-xs text-muted-foreground"
        >
          <BranchPickerPrimitive.Previous asChild>
            <button className="rounded p-0.5 hover:bg-accent">
              <ChevronUpIcon className="h-3 w-3" />
            </button>
          </BranchPickerPrimitive.Previous>
          <span><BranchPickerPrimitive.Number /> / <BranchPickerPrimitive.Count /></span>
          <BranchPickerPrimitive.Next asChild>
            <button className="rounded p-0.5 hover:bg-accent">
              <ChevronDownIcon className="h-3 w-3" />
            </button>
          </BranchPickerPrimitive.Next>
        </BranchPickerPrimitive.Root>
      </div>
    </MessagePrimitive.Root>
  );
}

// ── Welcome ───────────────────────────────────────────────────────
function WelcomeScreen() {
  return (
    <ThreadPrimitive.Empty>
      <div className="flex flex-col items-center justify-center gap-5 py-16 text-center px-6">
        <div className="flex h-16 w-16 items-center justify-center rounded-2xl bg-primary/20">
          <BotIcon className="h-8 w-8 text-primary" />
        </div>
        <div>
          <h2 className="text-xl font-semibold">你好，我是 Aria</h2>
          <p className="mt-1 text-sm text-muted-foreground">按住下方按钮开始说话</p>
        </div>
      </div>
    </ThreadPrimitive.Empty>
  );
}

// ── Hold-to-talk mic button ───────────────────────────────────────
type RecordState = "idle" | "recording" | "transcribing" | "processing";

function HoldMicButton() {
  const aui = useAui();
  const isRunning = useAuiState((s) => s.thread.isRunning);
  const [state, setState] = useState<RecordState>("idle");
  const [error, setError] = useState<string | null>(null);
  const mediaRecorderRef = useRef<MediaRecorder | null>(null);
  const chunksRef = useRef<Blob[]>([]);
  const streamRef = useRef<MediaStream | null>(null);
  const isHoldingRef = useRef(false);
  /** 手指仍按住（含 await getUserMedia 期间）；false 表示已松开，不应再启动本轮录制 */
  const pointerDownRef = useRef(false);

  const startRecording = useCallback(async () => {
    if (isHoldingRef.current) return;
    isHoldingRef.current = true;
    setError(null);
    chunksRef.current = [];

    try {
      let acquiredFreshStream = false;
      let stream = reuseLiveMicStream(streamRef.current);
      if (!stream) {
        streamRef.current?.getTracks().forEach((t) => t.stop());
        streamRef.current = null;
        stream = await navigator.mediaDevices.getUserMedia({ audio: true });
        streamRef.current = stream;
        acquiredFreshStream = true;
      }

      if (!pointerDownRef.current) {
        isHoldingRef.current = false;
        if (acquiredFreshStream) {
          stream.getTracks().forEach((t) => t.stop());
          streamRef.current = null;
        }
        return;
      }

      const mimeType = getSupportedMimeType();
      const mr = new MediaRecorder(stream, mimeType ? { mimeType } : undefined);

      mr.ondataavailable = (e) => {
        if (e.data.size > 0) chunksRef.current.push(e.data);
      };

      mr.onstop = async () => {
        isHoldingRef.current = false;
        mediaRecorderRef.current = null;
        // 不 stop 轨道：同页会话内复用麦克风流，离开页面时在 effect 里统一释放

        const blob = new Blob(chunksRef.current, { type: mr.mimeType || "audio/webm" });
        if (blob.size < 1000) {
          // 录音太短（< 1KB），忽略
          setState("idle");
          return;
        }

        setState("transcribing");

        // 上传到 /api/transcribe
        try {
          const form = new FormData();
          form.append("audio", blob, `recording.${mr.mimeType?.split("/")[1]?.split(";")[0] ?? "webm"}`);
          const resp = await fetch("/api/transcribe", { method: "POST", body: form });
          const data = await resp.json() as { text?: string; error?: string };

          if (!resp.ok || data.error) {
            setError(data.error ?? "转录失败");
            setState("idle");
            return;
          }

          const text = (data.text ?? "").trim();
          if (!text) {
            setState("idle");
            return;
          }

          // 直接发送到对话
          setState("processing");
          aui.thread().append({
            content: [{ type: "text", text }],
            runConfig: aui.composer().getState().runConfig,
          });
        } catch (e) {
          setError(e instanceof Error ? e.message : "网络错误");
          setState("idle");
        }
      };

      mr.start();
      mediaRecorderRef.current = mr;
      setState("recording");
    } catch (e) {
      isHoldingRef.current = false;
      const msg = e instanceof Error ? e.message : "麦克风权限被拒绝";
      setError(msg);
      setState("idle");
    }
  }, [aui]);

  const stopRecording = useCallback(() => {
    if (mediaRecorderRef.current?.state === "recording") {
      mediaRecorderRef.current.stop();
    }
  }, []);

  // 防止页面滚动 / iOS 长按上下文菜单
  const handlePointerDown = useCallback((e: React.PointerEvent) => {
    e.preventDefault();
    pointerDownRef.current = true;
    (e.currentTarget as HTMLElement).setPointerCapture(e.pointerId);
    void startRecording();
  }, [startRecording]);

  const handlePointerUp = useCallback(() => {
    pointerDownRef.current = false;
    stopRecording();
  }, [stopRecording]);

  // AI 回复结束后切回 idle
  useEffect(() => {
    if (!isRunning && state === "processing") {
      setState("idle");
    }
  }, [isRunning, state]);

  // 组件卸载时清理麦克风流
  useEffect(() => () => {
    streamRef.current?.getTracks().forEach((t) => t.stop());
  }, []);

  const disabled = state === "transcribing" || state === "processing";

  const statusText: Record<RecordState, string> = {
    idle: "",
    recording: "正在录音，松开发送…",
    transcribing: "正在转录…",
    processing: "正在思考…",
  };

  return (
    <div className="flex flex-col items-center gap-1.5">
      {/* 错误提示 */}
      {error && (
        <p className="text-xs text-red-400 text-center px-2">{error}</p>
      )}
      {/* 状态文字 + 操作提示合并为一行，节省垂直空间 */}
      <p className="text-xs text-muted-foreground h-4">
        {state === "idle" ? "按住说话" : statusText[state]}
      </p>

      {/* 外圈脉冲 + 主按钮 */}
      <div className="relative flex items-center justify-center">
        {state === "recording" && (
          <>
            <span className="absolute h-20 w-20 rounded-full bg-primary/20 animate-ping" />
            <span className="absolute h-16 w-16 rounded-full bg-primary/10 animate-pulse" />
          </>
        )}
        {(state === "transcribing" || state === "processing") && (
          <span className="absolute h-16 w-16 rounded-full bg-zinc-700/40 animate-pulse" />
        )}

        <button
          onPointerDown={handlePointerDown}
          onPointerUp={handlePointerUp}
          onPointerCancel={handlePointerUp}
          disabled={disabled}
          className={cn(
            "relative z-10 flex h-14 w-14 select-none touch-none items-center justify-center rounded-full transition-all duration-150 shadow-lg",
            state === "idle" && "bg-primary text-primary-foreground active:scale-95",
            state === "recording" && "bg-red-500 text-white scale-110",
            (state === "transcribing" || state === "processing") && "bg-zinc-700 text-zinc-400 cursor-not-allowed",
          )}
        >
          <MicIcon className={cn("h-6 w-6", state === "recording" && "animate-pulse")} />
        </button>
      </div>
    </div>
  );
}

// ── Thinking indicator ────────────────────────────────────────────
function ThinkingDots() {
  return (
    <ThreadPrimitive.If running>
      <div className="flex gap-2 px-3 py-1.5">
        <div className="flex h-7 w-7 shrink-0 items-center justify-center rounded-full bg-primary/20">
          <BotIcon className="h-3.5 w-3.5 text-primary" />
        </div>
        <div className="flex items-center gap-1.5 rounded-2xl rounded-bl-sm bg-zinc-800 px-3 py-2">
          <span className="h-1.5 w-1.5 rounded-full bg-zinc-400 animate-bounce [animation-delay:-0.3s]" />
          <span className="h-1.5 w-1.5 rounded-full bg-zinc-400 animate-bounce [animation-delay:-0.15s]" />
          <span className="h-1.5 w-1.5 rounded-full bg-zinc-400 animate-bounce" />
        </div>
      </div>
    </ThreadPrimitive.If>
  );
}

// ── Main ──────────────────────────────────────────────────────────
export function VoiceThread() {
  return (
    <>
      {/* 注册自定义工具 UI，与主聊天页一致，确保拍照、地图等结果能正确展示 */}
      <RobotStatusToolUI />
      <CleanRoomsToolUI />
      <FullCleanToolUI />
      <PauseToolUI />
      <ResumeToolUI />
      <ReturnHomeToolUI />
      <GetRoomsToolUI />
      <TakeRobotPhotoToolUI />
      <MoveCameraMountToolUI />
      <CenterCameraMountToolUI />
      <AddTaskToolUI />
      <ListTasksToolUI />
      <CleaningHistoryToolUI />
      <StartExploringToolUI />
      <StopExploringToolUI />
      <GetMapStatusToolUI />
      <GetMapImageToolUI />

      {/* relative 给 ScrollToBottom 绝对定位用，overflow-hidden 防止整体溢出 */}
      <ThreadPrimitive.Root className="relative flex h-full w-full flex-col overflow-hidden bg-zinc-950">

      {/* 顶部导航栏 — 固定高度，不参与 flex 拉伸 */}
      <header className="shrink-0 flex items-center justify-between border-b border-border px-4 py-3">
        <Link
          href="/"
          className="flex items-center gap-1.5 text-sm text-muted-foreground hover:text-foreground transition-colors"
        >
          <ArrowLeftIcon className="h-4 w-4" />
          返回
        </Link>
        <div className="flex items-center gap-1.5 text-sm font-medium">
          <BotIcon className="h-4 w-4 text-primary" />
          Aria 语音模式
        </div>
        <div className="flex items-center gap-1 text-xs text-primary/80">
          <Volume2Icon className="h-3.5 w-3.5" />
          外放
        </div>
      </header>

      {/* 消息区 — min-h-0 必须加，否则内容会撑开父容器引发整页滚动 */}
      <ThreadPrimitive.Viewport className="min-h-0 flex-1 overflow-y-auto py-3">
        <WelcomeScreen />
        <ThreadPrimitive.Messages
          components={{ UserMessage, AssistantMessage }}
        />
        <ThinkingDots />
      </ThreadPrimitive.Viewport>

      {/* 滚动到底部 */}
      <ThreadPrimitive.ScrollToBottom asChild>
        <button className="absolute right-4 bottom-28 flex h-8 w-8 items-center justify-center rounded-full border border-border bg-zinc-900 shadow-lg opacity-0 transition-opacity data-[visible]:opacity-100 hover:bg-zinc-800">
          <ScrollDownIcon className="h-4 w-4" />
        </button>
      </ThreadPrimitive.ScrollToBottom>

      {/* 底部麦克风区 — 紧凑布局，为聊天区留出更多空间 */}
      <div className="shrink-0 border-t border-border bg-zinc-950 flex flex-col items-center py-3 pb-6">
        <HoldMicButton />
      </div>

    </ThreadPrimitive.Root>
    </>
  );
}
