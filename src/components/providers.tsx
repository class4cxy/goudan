"use client";

import { useEffect, useMemo, useState } from "react";
import { AssistantRuntimeProvider } from "@assistant-ui/react";
import { useChatRuntime, AssistantChatTransport } from "@assistant-ui/react-ai-sdk";
import type { UIMessage } from "ai";

export interface ProvidersProps {
  children: React.ReactNode;
  threadId: string;
}

/**
 * Outer shell: fetches saved messages for the given threadId from the server,
 * then mounts the inner runtime once data is ready.
 * Using `key={threadId}` on ProvidersInner ensures a fresh runtime is created
 * whenever the active thread changes.
 */
export function Providers({ children, threadId }: ProvidersProps) {
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
    <ProvidersInner key={threadId} threadId={threadId} initialMessages={initialMessages}>
      {children}
    </ProvidersInner>
  );
}

function ProvidersInner({
  children,
  threadId,
  initialMessages,
}: {
  children: React.ReactNode;
  threadId: string;
  initialMessages?: UIMessage[];
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

  return (
    <AssistantRuntimeProvider runtime={runtime}>{children}</AssistantRuntimeProvider>
  );
}
