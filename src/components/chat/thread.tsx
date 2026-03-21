"use client";

import {
  ThreadPrimitive,
  MessagePrimitive,
  ComposerPrimitive,
  ActionBarPrimitive,
  BranchPickerPrimitive,
  useAui,
  type ToolCallMessagePartComponent,
} from "@assistant-ui/react";
import {
  BotIcon, SendIcon, UserIcon,
  ChevronUpIcon, ChevronDownIcon,
  CopyIcon, RefreshCwIcon,
  MicIcon, Volume2Icon, VolumeXIcon,
} from "lucide-react";
import { cn } from "@/lib/utils";
import { useCallback, useEffect, useRef, useState, useSyncExternalStore } from "react";
import { voiceModeStore } from "@/components/chat/voice-mode-store";
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

// ── Voice mode toggle ─────────────────────────────────────────────
function VoiceModeToggle() {
  const voiceMode = useSyncExternalStore(
    voiceModeStore.subscribe,
    voiceModeStore.getSnapshot,
    voiceModeStore.getServerSnapshot,
  );

  return (
    <button
      onClick={() => voiceModeStore.toggle()}
      title={voiceMode ? "关闭蓝牙外放" : "开启蓝牙外放"}
      className={cn(
        "flex h-8 w-8 shrink-0 items-center justify-center rounded-lg transition-colors",
        voiceMode
          ? "bg-primary/20 text-primary hover:bg-primary/30"
          : "text-muted-foreground hover:bg-accent hover:text-foreground",
      )}
    >
      {voiceMode ? <Volume2Icon className="h-4 w-4" /> : <VolumeXIcon className="h-4 w-4" />}
    </button>
  );
}

// ── Microphone voice input ────────────────────────────────────────
/**
 * 使用浏览器 Web Speech API 录音，结果转成文字后填入 composer。
 * 仅在支持的浏览器（Chrome / Edge / Safari）上显示。
 */
// Minimal type shim for Web Speech API (not in all TS lib versions)
type AnyRecognition = {
  lang: string;
  interimResults: boolean;
  maxAlternatives: number;
  onstart: (() => void) | null;
  onend: (() => void) | null;
  onerror: (() => void) | null;
  onresult: ((e: { results: { [i: number]: { [j: number]: { transcript: string } } } }) => void) | null;
  start(): void;
  stop(): void;
};

function MicButton() {
  const aui = useAui();
  const [listening, setListening] = useState(false);
  const recognitionRef = useRef<AnyRecognition | null>(null);

  const isSupported =
    typeof window !== "undefined" &&
    ("SpeechRecognition" in window || "webkitSpeechRecognition" in window);

  const startListening = useCallback(() => {
    if (listening) {
      recognitionRef.current?.stop();
      return;
    }

    const w = window as unknown as Record<string, unknown>;
    const SR = (w["SpeechRecognition"] ?? w["webkitSpeechRecognition"]) as (new () => AnyRecognition) | undefined;
    if (!SR) return;

    const rec = new SR();
    rec.lang = "zh-CN";
    rec.interimResults = false;
    rec.maxAlternatives = 1;

    rec.onstart = () => setListening(true);
    rec.onend = () => setListening(false);
    rec.onerror = () => setListening(false);

    rec.onresult = (event) => {
      const transcript = event.results[0]?.[0]?.transcript ?? "";
      if (transcript) {
        const prev = aui.composer().getState().text;
        aui.composer().setText(prev.trim() ? `${prev} ${transcript}` : transcript);
      }
    };

    recognitionRef.current = rec;
    rec.start();
  }, [listening, aui]);

  // 组件卸载时停止录音
  useEffect(() => () => recognitionRef.current?.stop(), []);

  if (!isSupported) return null;

  return (
    <button
      onClick={startListening}
      title={listening ? "停止录音" : "语音输入（中文）"}
      className={cn(
        "flex h-8 w-8 shrink-0 items-center justify-center rounded-lg transition-colors",
        listening
          ? "bg-red-500/20 text-red-400 animate-pulse"
          : "text-muted-foreground hover:bg-accent hover:text-foreground",
      )}
    >
      <MicIcon className="h-4 w-4" />
    </button>
  );
}

// ── Voice mode hint bar ───────────────────────────────────────────
function VoiceModeHint() {
  const voiceMode = useSyncExternalStore(
    voiceModeStore.subscribe,
    voiceModeStore.getSnapshot,
    voiceModeStore.getServerSnapshot,
  );

  if (!voiceMode) return null;

  return (
    <p className="mt-1.5 flex items-center gap-1.5 px-1 text-xs text-primary/70">
      <Volume2Icon className="h-3 w-3" />
      外放模式已开启，AI 回复将通过蓝牙扬声器播出
    </p>
  );
}

// ── Generic tool fallback ─────────────────────────────────────────
const FallbackToolUI: ToolCallMessagePartComponent = ({ toolName, status }) => {
  const running = status.type === "running" || status.type === "requires-action";
  return (
    <div className="my-2 flex items-center gap-2 rounded-lg border border-border bg-zinc-900 px-3 py-2 text-xs text-muted-foreground">
      <span className={cn("h-1.5 w-1.5 rounded-full", running ? "bg-yellow-400 animate-pulse" : "bg-emerald-500")} />
      <span className="font-mono">{toolName}</span>
      {running && <span>执行中...</span>}
    </div>
  );
};

// ── User message ──────────────────────────────────────────────────
function UserMessage() {
  return (
    <MessagePrimitive.Root className="flex justify-end gap-3 px-4 py-2">
      <div className="max-w-[75%] rounded-2xl rounded-br-sm bg-primary px-4 py-2.5 text-sm text-primary-foreground">
        <MessagePrimitive.Parts />
      </div>
      <div className="flex h-8 w-8 shrink-0 items-center justify-center rounded-full bg-zinc-800">
        <UserIcon className="h-4 w-4 text-zinc-400" />
      </div>
    </MessagePrimitive.Root>
  );
}

// ── Assistant message ─────────────────────────────────────────────
function AssistantMessage() {
  return (
    <MessagePrimitive.Root className="flex gap-3 px-4 py-2">
      <div className="flex h-8 w-8 shrink-0 items-center justify-center rounded-full bg-primary/20">
        <BotIcon className="h-4 w-4 text-primary" />
      </div>
      <div className="flex-1 min-w-0">
        <div className="prose prose-sm prose-invert max-w-none text-sm leading-relaxed">
          <MessagePrimitive.Parts
            components={{
              Text: ({ text }) => <p className="mb-2 last:mb-0 whitespace-pre-wrap">{text}</p>,
              tools: { Fallback: FallbackToolUI },
            }}
          />
        </div>
        <ActionBarPrimitive.Root
          hideWhenRunning
          autohide="not-last"
          className="mt-1 flex items-center gap-1 opacity-0 transition-opacity group-hover:opacity-100 data-[visible]:opacity-100"
        >
          <ActionBarPrimitive.Copy asChild>
            <button className="rounded p-1 text-muted-foreground hover:bg-accent hover:text-foreground">
              <CopyIcon className="h-3 w-3" />
            </button>
          </ActionBarPrimitive.Copy>
          <ActionBarPrimitive.Reload asChild>
            <button className="rounded p-1 text-muted-foreground hover:bg-accent hover:text-foreground">
              <RefreshCwIcon className="h-3 w-3" />
            </button>
          </ActionBarPrimitive.Reload>
        </ActionBarPrimitive.Root>
        <BranchPickerPrimitive.Root
          hideWhenSingleBranch
          className="mt-1 flex items-center gap-1 text-xs text-muted-foreground"
        >
          <BranchPickerPrimitive.Previous asChild>
            <button className="rounded p-0.5 hover:bg-accent">
              <ChevronUpIcon className="h-3 w-3" />
            </button>
          </BranchPickerPrimitive.Previous>
          <span>
            <BranchPickerPrimitive.Number /> / <BranchPickerPrimitive.Count />
          </span>
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

// ── Welcome screen ────────────────────────────────────────────────
const SUGGESTIONS = [
  { prompt: "帮我打扫一下客厅", label: "🧹 打扫客厅" },
  { prompt: "家里现在脏不脏？", label: "📷 查看卫生" },
  { prompt: "机器人现在什么状态？", label: "🤖 查看状态" },
  { prompt: "设置每天早上9点自动打扫全屋", label: "⏰ 设定定时" },
];

function WelcomeScreen() {
  return (
    <ThreadPrimitive.Empty>
      <div className="flex flex-col items-center justify-center gap-8 py-20 text-center">
        <div className="flex h-16 w-16 items-center justify-center rounded-2xl bg-primary/20">
          <BotIcon className="h-8 w-8 text-primary" />
        </div>
        <div>
          <h2 className="text-xl font-semibold">你好，我是 Aria</h2>
          <p className="mt-1 text-sm text-muted-foreground">你的家庭智能管家，随时帮你打理家务</p>
        </div>
        <div className="grid grid-cols-2 gap-2">
          {SUGGESTIONS.map((s) => (
            <ThreadPrimitive.Suggestion
              key={s.prompt}
              prompt={s.prompt}
              method="replace"
              autoSend
              className="rounded-xl border border-border bg-zinc-900 px-4 py-3 text-sm text-left hover:bg-zinc-800 hover:border-primary/40 transition-colors cursor-pointer"
            >
              {s.label}
            </ThreadPrimitive.Suggestion>
          ))}
        </div>
      </div>
    </ThreadPrimitive.Empty>
  );
}

// ── Main Thread ───────────────────────────────────────────────────
export function ChatThread() {
  return (
    <>
      {/* Register custom tool UIs (renders nothing, hooks into context) */}
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

      <ThreadPrimitive.Root className="flex h-full flex-col bg-zinc-950 group">
        <ThreadPrimitive.Viewport className="flex-1 overflow-y-auto">
          <WelcomeScreen />
          <ThreadPrimitive.Messages
            components={{
              UserMessage,
              AssistantMessage,
            }}
          />
        </ThreadPrimitive.Viewport>

        {/* Scroll-to-bottom button */}
        <ThreadPrimitive.ScrollToBottom asChild>
          <button className="absolute bottom-24 right-6 flex h-8 w-8 items-center justify-center rounded-full border border-border bg-zinc-900 shadow-lg opacity-0 transition-opacity data-[visible]:opacity-100 hover:bg-zinc-800">
            <ChevronDownIcon className="h-4 w-4" />
          </button>
        </ThreadPrimitive.ScrollToBottom>

        {/* Composer */}
        <div className="border-t border-border bg-zinc-950 p-4">
          <ComposerPrimitive.Root className="flex items-end gap-2 rounded-xl border border-border bg-zinc-900 px-3 py-2.5 focus-within:border-primary/50 transition-colors">
            <ComposerPrimitive.Input
              placeholder="发消息给 Aria…"
              className="flex-1 resize-none bg-transparent text-sm text-foreground placeholder:text-muted-foreground focus:outline-none max-h-40"
              rows={1}
            />
            {/* 语音输入（麦克风） */}
            <MicButton />
            {/* 蓝牙外放模式开关 */}
            <VoiceModeToggle />
            <ComposerPrimitive.Send asChild>
              <button className="flex h-8 w-8 shrink-0 items-center justify-center rounded-lg bg-primary text-primary-foreground hover:bg-primary/90 disabled:opacity-40 disabled:pointer-events-none transition-colors">
                <SendIcon className="h-4 w-4" />
              </button>
            </ComposerPrimitive.Send>
          </ComposerPrimitive.Root>
          {/* 状态提示行 */}
          <VoiceModeHint />
        </div>
      </ThreadPrimitive.Root>
    </>
  );
}
