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
  // voiceMode 用 ref 持有最新值，避免重建 transport
  const voiceMode = useSyncExternalStore(
    voiceModeStore.subscribe,
    voiceModeStore.getSnapshot,
    voiceModeStore.getServerSnapshot,
  );
  const voiceModeRef = useRef(voiceMode);
  useEffect(() => { voiceModeRef.current = voiceMode; }, [voiceMode]);

  // body 为函数形式（Resolvable），每次请求时动态读取最新 voiceMode
  const transport = useMemo(
    () => new AssistantChatTransport({
      api: "/api/chat",
      body: () => ({ threadId, voiceMode: voiceModeRef.current }),
    }),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [threadId]
  );

  const runtime = useChatRuntime({
    transport,
    ...(initialMessages ? { messages: initialMessages } : {}),
  });

  // 订阅 runtime 线程状态变化：当 AI 回复完成（running→idle）时触发标题生成
  const prevRunning = useRef(false);
  useEffect(() => {
    if (!onAssistantReply) return;
    return runtime.thread.subscribe(() => {
      const isRunning = runtime.thread.getState().isRunning;
      const wasRunning = prevRunning.current;
      prevRunning.current = isRunning;
      if (wasRunning && !isRunning) {
        fetch(`/api/threads/${threadId}/title`, { method: "POST" })
          .finally(() => onAssistantReply!());
      }
    });
  }, [runtime, threadId, onAssistantReply]);

  return (
    <AssistantRuntimeProvider runtime={runtime}>{children}</AssistantRuntimeProvider>
  );
}
