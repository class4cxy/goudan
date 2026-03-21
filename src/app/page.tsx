"use client";

import { useState, useCallback } from "react";
import { Sidebar } from "@/components/layout/sidebar";
import { ChatThread } from "@/components/chat/thread";
import { Providers } from "@/components/providers";
import { VoiceLogsPanel } from "@/components/voice-logs/panel";

function genId(): string {
  return Date.now().toString(36) + Math.random().toString(36).slice(2, 9);
}

export default function HomePage() {
  const [activeThreadId, setActiveThreadId] = useState<string>(genId);
  const [threadRefresh, setThreadRefresh] = useState(0);
  const [showVoiceLogs, setShowVoiceLogs] = useState(false);

  const handleNewThread = useCallback(() => {
    setActiveThreadId(genId());
    setThreadRefresh((n) => n + 1);
    setShowVoiceLogs(false);
  }, []);

  const handleSelectThread = useCallback((id: string) => {
    setActiveThreadId(id);
    setShowVoiceLogs(false);
  }, []);

  const handleAssistantReply = useCallback(() => {
    setThreadRefresh((n) => n + 1);
  }, []);

  return (
    <div className="flex h-screen overflow-hidden">
      <Sidebar
        activeThreadId={activeThreadId}
        onSelectThread={handleSelectThread}
        onNewThread={handleNewThread}
        threadRefresh={threadRefresh}
        onOpenVoiceLogs={() => setShowVoiceLogs(true)}
        voiceLogsOpen={showVoiceLogs}
      />
      <main className="flex flex-1 flex-col overflow-hidden">
        {showVoiceLogs ? (
          <VoiceLogsPanel onClose={() => setShowVoiceLogs(false)} />
        ) : (
          <Providers threadId={activeThreadId} onAssistantReply={handleAssistantReply}>
            <ChatThread />
          </Providers>
        )}
      </main>
    </div>
  );
}
