"use client";

import { useState, useCallback } from "react";
import { Sidebar } from "@/components/layout/sidebar";
import { ChatThread } from "@/components/chat/thread";
import { Providers } from "@/components/providers";

function genId(): string {
  return Date.now().toString(36) + Math.random().toString(36).slice(2, 9);
}

export default function HomePage() {
  const [activeThreadId, setActiveThreadId] = useState<string>(genId);
  const [threadRefresh, setThreadRefresh] = useState(0);

  const handleNewThread = useCallback(() => {
    setActiveThreadId(genId());
    setThreadRefresh((n) => n + 1);
  }, []);

  const handleSelectThread = useCallback((id: string) => {
    setActiveThreadId(id);
  }, []);

  return (
    <div className="flex h-screen overflow-hidden">
      <Sidebar
        activeThreadId={activeThreadId}
        onSelectThread={handleSelectThread}
        onNewThread={handleNewThread}
        threadRefresh={threadRefresh}
      />
      <main className="flex flex-1 flex-col overflow-hidden">
        <Providers threadId={activeThreadId}>
          <ChatThread />
        </Providers>
      </main>
    </div>
  );
}
