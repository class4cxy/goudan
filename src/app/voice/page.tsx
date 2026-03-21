"use client";

import { useState, useCallback, useEffect } from "react";
import { Providers } from "@/components/providers";
import { VoiceThread } from "@/components/chat/voice-thread";
import { voiceModeStore } from "@/components/chat/voice-mode-store";

function genId(): string {
  return Date.now().toString(36) + Math.random().toString(36).slice(2, 9);
}

export default function VoicePage() {
  const [threadId] = useState<string>(genId);
  const [threadRefresh, setThreadRefresh] = useState(0);

  // 语音页默认开启外放模式
  useEffect(() => {
    voiceModeStore.set(true);
  }, []);

  const handleAssistantReply = useCallback(() => {
    setThreadRefresh((n) => n + 1);
  }, []);

  return (
    <div className="h-screen overflow-hidden">
      <Providers threadId={threadId} onAssistantReply={handleAssistantReply}>
        <VoiceThread />
      </Providers>
    </div>
  );
}
