"use client";

import { useEffect, useState, useCallback } from "react";
import {
  BotIcon,
  BatteryChargingIcon,
  WifiOffIcon,
  RefreshCwIcon,
  PlusIcon,
  Trash2Icon,
  MessageSquareIcon,
  MicIcon,
  Radio,
} from "lucide-react";
import Link from "next/link";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";

// ── Robot Status ──────────────────────────────────────────────────

interface RoborockStatus {
  state?: string;
  battery?: number;
  error_code?: number;
  in_cleaning?: boolean;
  in_returning?: boolean;
}

interface RobotStatus {
  power: {
    voltage_v: number | null;
    battery_pct: number | null;
    is_charging: boolean | null;
  };
  modules: {
    lidar: boolean;
    chassis: boolean;
  };
}

interface StatusData {
  platform_ok: boolean;
  roborock: RoborockStatus | null;
  robot: RobotStatus | null;
}

function useRobotStatus() {
  const [data, setData] = useState<StatusData | null>(null);
  const [loading, setLoading] = useState(true);

  const refresh = useCallback(async () => {
    setLoading(true);
    try {
      const res = await fetch("/api/status");
      const json = (await res.json()) as StatusData;
      setData(json);
    } catch {
      setData({ platform_ok: false, roborock: null, robot: null });
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void refresh();
    const interval = setInterval(refresh, 30_000);
    return () => clearInterval(interval);
  }, [refresh]);

  return { data, loading, refresh };
}

function BatteryBar({ level }: { level: number }) {
  const color =
    level > 60 ? "bg-emerald-500" : level > 20 ? "bg-yellow-500" : "bg-red-500";
  return (
    <div className="h-1.5 w-full rounded-full bg-zinc-800 overflow-hidden">
      <div className={cn("h-full rounded-full transition-all", color)} style={{ width: `${level}%` }} />
    </div>
  );
}

// ── Thread List ───────────────────────────────────────────────────

export interface Thread {
  id: string;
  title: string | null;
  created_at: number;
  updated_at: number;
}

interface ThreadListProps {
  activeThreadId: string;
  onSelect: (id: string) => void;
  onNew: () => void;
  refresh: number; // increment to force re-fetch
}

function useThreads(refreshSignal: number) {
  const [threads, setThreads] = useState<Thread[]>([]);

  const load = useCallback(() => {
    fetch("/api/threads")
      .then((r) => r.json())
      .then((data: Thread[]) => setThreads(data))
      .catch(() => {});
  }, []);

  useEffect(() => {
    load();
  }, [refreshSignal, load]);

  return threads;
}

async function deleteThread(id: string): Promise<void> {
  await fetch(`/api/threads/${id}`, { method: "DELETE" });
}

function ThreadList({ activeThreadId, onSelect, onNew, refresh }: ThreadListProps) {
  const [localRefresh, setLocalRefresh] = useState(0);
  const threads = useThreads(refresh + localRefresh);

  const handleDelete = async (e: React.MouseEvent, id: string) => {
    e.stopPropagation();
    await deleteThread(id);
    setLocalRefresh((n) => n + 1);
    if (id === activeThreadId) onNew();
  };

  return (
    <div className="flex-1 flex flex-col min-h-0">
      {/* Header */}
      <div className="flex items-center justify-between px-3 py-2">
        <span className="text-[10px] font-medium uppercase text-muted-foreground tracking-wider">
          历史会话
        </span>
        <Button
          variant="ghost"
          size="icon"
          className="h-5 w-5 text-muted-foreground hover:text-foreground"
          onClick={onNew}
          title="新对话"
        >
          <PlusIcon className="h-3.5 w-3.5" />
        </Button>
      </div>

      {/* Thread items */}
      <div className="flex-1 overflow-y-auto px-2 space-y-0.5 pb-2">
        {threads.length === 0 ? (
          <p className="text-[11px] text-muted-foreground text-center py-4">
            暂无历史会话
          </p>
        ) : (
          threads.map((t) => (
            <div
              key={t.id}
              onClick={() => onSelect(t.id)}
              className={cn(
                "group flex items-center gap-2 rounded-lg px-2 py-1.5 cursor-pointer transition-colors text-xs",
                t.id === activeThreadId
                  ? "bg-primary/10 text-primary"
                  : "text-muted-foreground hover:bg-zinc-800 hover:text-foreground"
              )}
            >
              <MessageSquareIcon className="h-3 w-3 shrink-0" />
              <span className="flex-1 truncate">
                {t.title ?? "新对话"}
              </span>
              <button
                onClick={(e) => void handleDelete(e, t.id)}
                className="opacity-0 group-hover:opacity-100 transition-opacity p-0.5 rounded hover:text-red-400"
              >
                <Trash2Icon className="h-3 w-3" />
              </button>
            </div>
          ))
        )}
      </div>
    </div>
  );
}

// ── Sidebar ───────────────────────────────────────────────────────

interface SidebarProps {
  activeThreadId: string;
  onSelectThread: (id: string) => void;
  onNewThread: () => void;
  threadRefresh: number;
  onOpenVoiceLogs: () => void;
  voiceLogsOpen: boolean;
}

export function Sidebar({
  activeThreadId,
  onSelectThread,
  onNewThread,
  threadRefresh,
  onOpenVoiceLogs,
  voiceLogsOpen,
}: SidebarProps) {
  const { data, loading, refresh } = useRobotStatus();
  const platformOk = data?.platform_ok ?? false;
  const roborock = data?.roborock;
  const robot    = data?.robot;

  const roborockStateLabel: Record<string, string> = {
    charging: "充电中", idle: "待机中", cleaning: "清扫中",
    paused: "已暂停", returning: "返回中", error: "故障",
  };

  return (
    <aside className="flex h-full w-56 flex-col border-r border-border bg-zinc-950">
      {/* Logo */}
      <div className="flex items-center gap-2.5 px-4 py-5 border-b border-border shrink-0">
        <div className="flex h-8 w-8 items-center justify-center rounded-lg bg-primary/20">
          <BotIcon className="h-4 w-4 text-primary" />
        </div>
        <div>
          <p className="text-sm font-semibold leading-none">Home Agent</p>
          <p className="text-[10px] text-muted-foreground mt-0.5">智能家居管家</p>
        </div>
      </div>

      {/* Nav Tabs — 文字对话 / 语音记录 */}
      <div className="flex shrink-0 border-b border-border">
        <button
          onClick={() => onSelectThread(activeThreadId)}
          className={cn(
            "flex flex-1 items-center justify-center gap-1.5 py-2 text-[11px] font-medium transition-colors border-b-2",
            !voiceLogsOpen
              ? "border-primary text-primary"
              : "border-transparent text-muted-foreground hover:text-foreground"
          )}
        >
          <MessageSquareIcon className="h-3.5 w-3.5" />
          文字对话
        </button>
        <button
          onClick={onOpenVoiceLogs}
          className={cn(
            "flex flex-1 items-center justify-center gap-1.5 py-2 text-[11px] font-medium transition-colors border-b-2",
            voiceLogsOpen
              ? "border-primary text-primary"
              : "border-transparent text-muted-foreground hover:text-foreground"
          )}
        >
          <MicIcon className="h-3.5 w-3.5" />
          语音记录
        </button>
        <Link
          href="/voice"
          className="flex flex-1 items-center justify-center gap-1.5 py-2 text-[11px] font-medium border-b-2 border-transparent text-muted-foreground hover:text-foreground transition-colors"
        >
          <Radio className="h-3.5 w-3.5" />
          语音模式
        </Link>
      </div>

      {/* Thread List */}
      {!voiceLogsOpen && (
        <ThreadList
          activeThreadId={activeThreadId}
          onSelect={onSelectThread}
          onNew={onNewThread}
          refresh={threadRefresh}
        />
      )}

      {/* Status Panel */}
      <div className="border-t border-border p-3 space-y-3 shrink-0">
        {/* Header */}
        <div className="flex items-center justify-between">
          <span className="text-[10px] font-medium uppercase text-muted-foreground tracking-wider">
            设备状态
          </span>
          <Button variant="ghost" size="icon" className="h-5 w-5" onClick={refresh} disabled={loading}>
            <RefreshCwIcon className={cn("h-3 w-3", loading && "animate-spin")} />
          </Button>
        </div>

        {/* Platform 连接指示 */}
        <div className="flex items-center gap-1.5 text-xs">
          {platformOk
            ? <span className="h-1.5 w-1.5 rounded-full bg-emerald-500 shrink-0" />
            : <WifiOffIcon className="h-3 w-3 text-red-400 shrink-0" />}
          <span className={platformOk ? "text-muted-foreground" : "text-red-400"}>
            {platformOk ? "平台已连接" : "平台未连接"}
          </span>
        </div>

        {/* 石头扫地机 */}
        <div className="space-y-1">
          <p className="text-[10px] text-muted-foreground">石头扫地机</p>
          {roborock ? (
            <div className="space-y-1.5 rounded-lg border border-border p-2">
              {roborock.battery !== undefined && (
                <div className="space-y-1">
                  <div className="flex items-center justify-between text-xs">
                    <span className="flex items-center gap-1 text-muted-foreground">
                      <BatteryChargingIcon className="h-3 w-3" /> 电量
                    </span>
                    <span className="font-mono font-medium">{roborock.battery}%</span>
                  </div>
                  <BatteryBar level={roborock.battery} />
                </div>
              )}
              {roborock.state && (
                <div className="flex items-center justify-between text-xs">
                  <span className="text-muted-foreground">状态</span>
                  <Badge variant="secondary" className="text-[10px] px-1.5 py-0">
                    {roborockStateLabel[roborock.state] ?? roborock.state}
                  </Badge>
                </div>
              )}
            </div>
          ) : (
            <div className="rounded-lg border border-border p-2">
              <p className="text-[11px] text-muted-foreground text-center">
                {loading ? "获取中..." : "未连接"}
              </p>
            </div>
          )}
        </div>

        {/* 树莓派机器人 */}
        <div className="space-y-1">
          <p className="text-[10px] text-muted-foreground">树莓派机器人</p>
          {robot ? (
            <div className="space-y-1.5 rounded-lg border border-border p-2">
              {robot.power.battery_pct !== null && (
                <div className="space-y-1">
                  <div className="flex items-center justify-between text-xs">
                    <span className="flex items-center gap-1 text-muted-foreground">
                      <BatteryChargingIcon className="h-3 w-3" />
                      {robot.power.is_charging ? "充电中" : "电量"}
                    </span>
                    <span className="font-mono font-medium">{robot.power.battery_pct}%</span>
                  </div>
                  <BatteryBar level={robot.power.battery_pct} />
                </div>
              )}
              {robot.power.voltage_v !== null && (
                <div className="flex items-center justify-between text-xs">
                  <span className="text-muted-foreground">电压</span>
                  <span className="font-mono">{robot.power.voltage_v} V</span>
                </div>
              )}
              <div className="flex gap-1 flex-wrap pt-0.5">
                <Badge variant={robot.modules.lidar   ? "default" : "secondary"} className="text-[10px] px-1.5 py-0">
                  激光雷达{robot.modules.lidar ? "" : "（模拟）"}
                </Badge>
                <Badge variant={robot.modules.chassis ? "default" : "secondary"} className="text-[10px] px-1.5 py-0">
                  底盘{robot.modules.chassis ? "" : "（模拟）"}
                </Badge>
              </div>
            </div>
          ) : (
            <div className="rounded-lg border border-border p-2">
              <p className="text-[11px] text-muted-foreground text-center">
                {loading ? "获取中..." : "未连接"}
              </p>
            </div>
          )}
        </div>
      </div>
    </aside>
  );
}
