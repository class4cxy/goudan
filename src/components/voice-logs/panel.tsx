"use client";

import { useEffect, useState, useCallback } from "react";
import {
  XIcon,
  MicIcon,
  ChevronDownIcon,
  ChevronRightIcon,
  BotIcon,
  UserIcon,
  CalendarDaysIcon,
  LoaderIcon,
} from "lucide-react";
import { Button } from "@/components/ui/button";
import { ScrollArea } from "@/components/ui/scroll-area";
import { cn } from "@/lib/utils";

// ── Types ──────────────────────────────────────────────────────────────────

interface VoiceLogDay {
  day: string;
  session_count: number;
  message_count: number;
  first_ts: number;
  last_ts: number;
}

interface VoiceLogSession {
  session_id: string;
  started_at: number;
  ended_at: number;
  message_count: number;
}

interface VoiceLogMessage {
  id: number;
  session_id: string;
  role: "user" | "assistant";
  content: string;
  created_at: number;
}

// ── Helpers ────────────────────────────────────────────────────────────────

function formatDay(day: string): string {
  const date = new Date(day + "T00:00:00+08:00");
  const now = new Date();
  const todayStr = now.toLocaleDateString("zh-CN", {
    year: "numeric", month: "2-digit", day: "2-digit", timeZone: "Asia/Shanghai",
  }).replace(/\//g, "-");

  if (day === todayStr) return "今天";
  const yesterday = new Date(now);
  yesterday.setDate(now.getDate() - 1);
  const yStr = yesterday.toLocaleDateString("zh-CN", {
    year: "numeric", month: "2-digit", day: "2-digit", timeZone: "Asia/Shanghai",
  }).replace(/\//g, "-");
  if (day === yStr) return "昨天";

  return date.toLocaleDateString("zh-CN", { month: "long", day: "numeric", weekday: "short" });
}

function formatTime(ts: number): string {
  return new Date(ts * 1000).toLocaleTimeString("zh-CN", {
    hour: "2-digit", minute: "2-digit", timeZone: "Asia/Shanghai",
  });
}

function formatDuration(startTs: number, endTs: number): string {
  const secs = endTs - startTs;
  if (secs < 60) return `${secs}秒`;
  return `${Math.floor(secs / 60)}分${secs % 60}秒`;
}

// ── Session Detail ─────────────────────────────────────────────────────────

function SessionDetail({ sessionId }: { sessionId: string }) {
  const [messages, setMessages] = useState<VoiceLogMessage[] | null>(null);

  useEffect(() => {
    fetch(`/api/voice-logs/sessions?session_id=${sessionId}`)
      .then((r) => r.json())
      .then((data: VoiceLogMessage[]) => setMessages(data))
      .catch(() => setMessages([]));
  }, [sessionId]);

  if (!messages) {
    return (
      <div className="flex items-center justify-center py-4">
        <LoaderIcon className="h-3.5 w-3.5 animate-spin text-muted-foreground" />
      </div>
    );
  }

  if (messages.length === 0) {
    return <p className="text-xs text-muted-foreground py-2 pl-2">暂无消息记录</p>;
  }

  return (
    <div className="mt-2 space-y-2">
      {messages.map((msg) => (
        <div
          key={msg.id}
          className={cn(
            "flex gap-2 text-xs",
            msg.role === "user" ? "flex-row" : "flex-row-reverse"
          )}
        >
          <div
            className={cn(
              "flex h-5 w-5 shrink-0 items-center justify-center rounded-full",
              msg.role === "user" ? "bg-zinc-700" : "bg-primary/20"
            )}
          >
            {msg.role === "user"
              ? <UserIcon className="h-3 w-3 text-zinc-300" />
              : <BotIcon className="h-3 w-3 text-primary" />
            }
          </div>
          <div
            className={cn(
              "max-w-[85%] rounded-lg px-2.5 py-1.5 leading-relaxed",
              msg.role === "user"
                ? "bg-zinc-800 text-zinc-200"
                : "bg-primary/10 text-primary"
            )}
          >
            {msg.content}
          </div>
        </div>
      ))}
    </div>
  );
}

// ── Session Item ───────────────────────────────────────────────────────────

function SessionItem({ session, idx }: { session: VoiceLogSession; idx: number }) {
  const [open, setOpen] = useState(false);

  return (
    <div className="rounded-lg border border-border overflow-hidden">
      <button
        onClick={() => setOpen((v) => !v)}
        className="flex w-full items-center gap-2 px-3 py-2 text-left hover:bg-zinc-800/60 transition-colors"
      >
        {open
          ? <ChevronDownIcon className="h-3.5 w-3.5 text-muted-foreground shrink-0" />
          : <ChevronRightIcon className="h-3.5 w-3.5 text-muted-foreground shrink-0" />
        }
        <MicIcon className="h-3 w-3 text-primary shrink-0" />
        <span className="flex-1 text-xs font-medium">
          对话 {idx + 1}
          <span className="ml-2 text-muted-foreground font-normal">
            {formatTime(session.started_at)} · {formatDuration(session.started_at, session.ended_at)} · {session.message_count} 条消息
          </span>
        </span>
      </button>
      {open && (
        <div className="px-3 pb-3">
          <SessionDetail sessionId={session.session_id} />
        </div>
      )}
    </div>
  );
}

// ── Day Section ────────────────────────────────────────────────────────────

function DaySection({ day }: { day: VoiceLogDay }) {
  const [open, setOpen] = useState(false);
  const [sessions, setSessions] = useState<VoiceLogSession[] | null>(null);
  const [loading, setLoading] = useState(false);

  const loadSessions = useCallback(async () => {
    if (sessions !== null) return;
    setLoading(true);
    try {
      const res = await fetch(`/api/voice-logs/sessions?day=${day.day}`);
      const data = (await res.json()) as VoiceLogSession[];
      setSessions(data);
    } catch {
      setSessions([]);
    } finally {
      setLoading(false);
    }
  }, [day.day, sessions]);

  const handleToggle = () => {
    setOpen((v) => {
      if (!v) void loadSessions();
      return !v;
    });
  };

  return (
    <div className="space-y-1">
      {/* Day header */}
      <button
        onClick={handleToggle}
        className="flex w-full items-center gap-2 rounded-lg px-3 py-2 hover:bg-zinc-800/60 transition-colors"
      >
        {open
          ? <ChevronDownIcon className="h-3.5 w-3.5 text-muted-foreground shrink-0" />
          : <ChevronRightIcon className="h-3.5 w-3.5 text-muted-foreground shrink-0" />
        }
        <CalendarDaysIcon className="h-3.5 w-3.5 text-muted-foreground shrink-0" />
        <span className="flex-1 text-left text-sm font-semibold">{formatDay(day.day)}</span>
        <span className="text-[11px] text-muted-foreground">
          {day.session_count} 次对话 · {day.message_count} 条消息
        </span>
      </button>

      {/* Sessions */}
      {open && (
        <div className="ml-3 space-y-1.5 pb-1">
          {loading && (
            <div className="flex items-center justify-center py-4">
              <LoaderIcon className="h-4 w-4 animate-spin text-muted-foreground" />
            </div>
          )}
          {sessions?.map((s, i) => (
            <SessionItem key={s.session_id} session={s} idx={i} />
          ))}
          {sessions?.length === 0 && !loading && (
            <p className="text-xs text-muted-foreground py-2">暂无记录</p>
          )}
        </div>
      )}
    </div>
  );
}

// ── VoiceLogsPanel ─────────────────────────────────────────────────────────

interface VoiceLogsPanelProps {
  onClose: () => void;
}

export function VoiceLogsPanel({ onClose }: VoiceLogsPanelProps) {
  const [days, setDays] = useState<VoiceLogDay[] | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    fetch("/api/voice-logs")
      .then((r) => r.json())
      .then((data: VoiceLogDay[]) => setDays(data))
      .catch(() => setDays([]))
      .finally(() => setLoading(false));
  }, []);

  return (
    <div className="flex h-full flex-col bg-zinc-950">
      {/* Header */}
      <div className="flex items-center justify-between border-b border-border px-4 py-3 shrink-0">
        <div className="flex items-center gap-2">
          <MicIcon className="h-4 w-4 text-primary" />
          <span className="text-sm font-semibold">语音对话记录</span>
        </div>
        <Button variant="ghost" size="icon" className="h-7 w-7" onClick={onClose}>
          <XIcon className="h-4 w-4" />
        </Button>
      </div>

      {/* Content */}
      <ScrollArea className="flex-1 min-h-0">
        <div className="p-3 space-y-1">
          {loading && (
            <div className="flex items-center justify-center py-16">
              <LoaderIcon className="h-5 w-5 animate-spin text-muted-foreground" />
            </div>
          )}
          {!loading && days?.length === 0 && (
            <div className="flex flex-col items-center justify-center py-16 gap-2 text-muted-foreground">
              <MicIcon className="h-8 w-8 opacity-30" />
              <p className="text-sm">暂无语音对话记录</p>
              <p className="text-xs">与机器人进行语音对话后，记录会出现在这里</p>
            </div>
          )}
          {days?.map((day) => (
            <DaySection key={day.day} day={day} />
          ))}
        </div>
      </ScrollArea>
    </div>
  );
}
