"use client";

import { useEffect, useRef, useMemo, useState, useSyncExternalStore } from "react";
import { AssistantRuntimeProvider } from "@assistant-ui/react";
import { useChatRuntime, AssistantChatTransport } from "@assistant-ui/react-ai-sdk";
import type { UIMessage } from "ai";
import { voiceModeStore } from "@/components/chat/voice-mode-store";

export interface ProvidersProps {
  children: React.ReactNode;
  threadId: string;
  /** AI 每次回复完成后触发（用于外层刷新侧边栏标题） */
  onAssistantReply?: () => void;
}

/**
 * Outer shell: fetches saved messages for the given threadId from the server,
 * then mounts the inner runtime once data is ready.
 * Using `key={threadId}` on ProvidersInner ensures a fresh runtime is created
 * whenever the active thread changes.
 */
export function Providers({ children, threadId, onAssistantReply }: ProvidersProps) {
  const [initialMessages, setInitialMessages] = useState<UIMessage[] | undefined>(undefined);
  const [ready, setReady] = useState(false);

  useEffect(() => {
    let cancelled = false;
    setReady(false);
    setInitialMessages(undefined);

    fetch(`/api/threads/${threadId}`)
      .then((r) => r.json())
      .then((msgs: UIMessage[]) => {
        if (!cancelled) {
          setInitialMessages(msgs.length > 0 ? msgs : undefined);
          setReady(true);
        }
      })
      .catch(() => {
        if (!cancelled) setReady(true);
      });

    return () => {
      cancelled = true;
    };
  }, [threadId]);

  if (!ready) return null;

  return (
    <ProvidersInner
      key={threadId}
      threadId={threadId}
      initialMessages={initialMessages}
      onAssistantReply={onAssistantReply}
    >
      {children}
    </ProvidersInner>
  );
}

function ProvidersInner({
  children,
  threadId,
  initialMessages,
  onAssistantReply,
}: {
  children: React.ReactNode;
  threadId: string;
  initialMessages?: UIMessage[];
  onAssistantReply?: () => void;
}) {
  const transport = useMemo(
    () => new AssistantChatTransport({ api: "/api/chat", body: { threadId } }),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    []
  );

  const runtime = useChatRuntime({
    transport,
    ...(initialMessages ? { messages: initialMessages } : {}),
  });

  // 订阅 runtime 线程状态变化：当 AI 回复完成（status 从 running → idle）时
  // 主动调标题生成接口，拿到结果后再通知外层刷新侧边栏，确保标题已就绪
  const voiceMode = useSyncExternalStore(
    voiceModeStore.subscribe,
    voiceModeStore.getSnapshot,
    voiceModeStore.getServerSnapshot,
  );

  const prevRunning = useRef(false);
  useEffect(() => {
    return runtime.thread.subscribe(() => {
      const isRunning = runtime.thread.getState().isRunning;
      const wasRunning = prevRunning.current;
      prevRunning.current = isRunning;

      if (!wasRunning || isRunning) return; // 只处理 running→idle 的边沿

      // 触发标题生成 + 外层回调
      if (onAssistantReply) {
        fetch(`/api/threads/${threadId}/title`, { method: "POST" })
          .finally(() => onAssistantReply());
      }

      // ── 外放模式：将最新 assistant 消息全文发送到 Platform TTS ──
      console.log("[VoiceMode] 回复完成，voiceMode =", voiceMode);
      if (!voiceMode) return;

      const threadState = runtime.thread.getState();
      const messages = threadState.messages as Array<{
        role: string;
        content?: Array<{ type: string; text?: string }>;
        // assistant-ui 不同版本可能用 parts 而非 content
        parts?: Array<{ type: string; text?: string }>;
      }>;
      console.log("[VoiceMode] 消息总数：", messages.length);

      const lastMsg = [...messages].reverse().find((m) => m.role === "assistant");
      console.log("[VoiceMode] 最新 assistant 消息：", JSON.stringify(lastMsg, null, 2));

      if (!lastMsg) {
        console.warn("[VoiceMode] 未找到 assistant 消息，跳过 speak");
        return;
      }

      // 兼容 content 和 parts 两种结构
      const parts = lastMsg.content ?? lastMsg.parts ?? [];
      const text = parts
        .filter((p) => p.type === "text" && typeof p.text === "string")
        .map((p) => p.text as string)
        .join("")
        .trim();

      console.log("[VoiceMode] 提取到文字：", JSON.stringify(text));

      if (!text) {
        console.warn("[VoiceMode] 提取文字为空，跳过 speak");
        return;
      }

      fetch("/api/speak", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ text }),
      })
        .then((r) => {
          console.log("[VoiceMode] /api/speak 响应：", r.status);
          return r.json();
        })
        .then((data) => console.log("[VoiceMode] /api/speak 结果：", data))
        .catch((err) => console.error("[VoiceMode] speak fetch 失败：", err));
    });
  }, [runtime, threadId, onAssistantReply, voiceMode]);

  return (
    <AssistantRuntimeProvider runtime={runtime}>{children}</AssistantRuntimeProvider>
  );
}
