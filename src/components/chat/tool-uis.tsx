"use client";

import { makeAssistantToolUI } from "@assistant-ui/react";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import {
  BatteryChargingIcon,
  CheckCircle2Icon,
  CameraIcon,
  EyeIcon,
  CalendarPlusIcon,
  ListTodoIcon,
  MapIcon,
  PauseCircleIcon,
  PlayCircleIcon,
  HomeIcon,
  ClockIcon,
  AlertCircleIcon,
  Loader2Icon,
} from "lucide-react";

// ── Helpers ──────────────────────────────────────────────────────

function ToolCard({
  icon: Icon,
  title,
  children,
  loading,
}: {
  icon: React.ElementType;
  title: string;
  children?: React.ReactNode;
  loading?: boolean;
}) {
  return (
    <Card className="my-2 max-w-sm animate-fade-in">
      <CardHeader className="pb-2">
        <CardTitle className="flex items-center gap-2 text-muted-foreground">
          {loading ? (
            <Loader2Icon className="h-4 w-4 animate-spin text-primary" />
          ) : (
            <Icon className="h-4 w-4 text-primary" />
          )}
          {title}
        </CardTitle>
      </CardHeader>
      {children && <CardContent>{children}</CardContent>}
    </Card>
  );
}

function ScoreBadge({ score }: { score: number }) {
  const variant =
    score <= 2 ? "success" : score === 3 ? "warning" : "danger";
  const label = ["", "非常干净", "较干净", "一般", "较脏", "非常脏"][score] ?? `${score}/5`;
  return <Badge variant={variant}>{label}</Badge>;
}

function StatusDot({ online }: { online: boolean }) {
  return (
    <span
      className={`inline-block h-2 w-2 rounded-full ${online ? "bg-emerald-500" : "bg-red-500"}`}
    />
  );
}

// ── Tool UIs ─────────────────────────────────────────────────────

export const RobotStatusToolUI = makeAssistantToolUI<
  Record<string, never>,
  { success: boolean; status?: Record<string, unknown>; error?: string }
>({
  toolName: "getRobotStatus",
  render({ result, status: execStatus }) {
    const loading = execStatus.type === "running";
    if (loading) return <ToolCard icon={BatteryChargingIcon} title="获取机器人状态..." loading />;

    if (!result?.success) {
      return (
        <ToolCard icon={AlertCircleIcon} title="机器人状态">
          <p className="text-xs text-red-400">{result?.error ?? "连接失败"}</p>
        </ToolCard>
      );
    }

    const s = result.status as Record<string, unknown> | undefined;
    const battery = s?.battery as number | undefined;
    const state = s?.state as string | undefined;

    return (
      <ToolCard icon={BatteryChargingIcon} title="机器人状态">
        <div className="flex flex-wrap gap-2 text-xs">
          {battery !== undefined && (
            <Badge variant="outline">🔋 {battery}%</Badge>
          )}
          {state && <Badge variant="secondary">{state}</Badge>}
        </div>
      </ToolCard>
    );
  },
});

export const CleanRoomsToolUI = makeAssistantToolUI<
  { rooms: string[]; repeat?: number },
  { success: boolean; message?: string; error?: string }
>({
  toolName: "cleanRooms",
  render({ args, result, status: execStatus }) {
    const loading = execStatus.type === "running";
    return (
      <ToolCard
        icon={loading ? Loader2Icon : CheckCircle2Icon}
        title={loading ? "正在启动清扫..." : "清扫指令"}
        loading={loading}
      >
        <div className="space-y-1.5">
          <div className="flex flex-wrap gap-1">
            {(args.rooms ?? []).map((r) => (
              <Badge key={r} variant="outline">
                {r}
              </Badge>
            ))}
          </div>
          {!loading && result && (
            <p className={`text-xs ${result.success ? "text-emerald-400" : "text-red-400"}`}>
              {result.success ? result.message : result.error}
            </p>
          )}
        </div>
      </ToolCard>
    );
  },
});

export const FullCleanToolUI = makeAssistantToolUI<
  Record<string, never>,
  { success: boolean; message?: string; error?: string }
>({
  toolName: "startFullCleaning",
  render({ result, status: execStatus }) {
    const loading = execStatus.type === "running";
    return (
      <ToolCard icon={CheckCircle2Icon} title="全屋清扫" loading={loading}>
        {!loading && result && (
          <p className={`text-xs ${result.success ? "text-emerald-400" : "text-red-400"}`}>
            {result.success ? result.message : result.error}
          </p>
        )}
      </ToolCard>
    );
  },
});

export const PauseToolUI = makeAssistantToolUI<
  Record<string, never>,
  { success: boolean; message?: string }
>({
  toolName: "pauseCleaning",
  render({ result, status: execStatus }) {
    return (
      <ToolCard icon={PauseCircleIcon} title="暂停清扫" loading={execStatus.type === "running"}>
        {result && (
          <p className="text-xs text-muted-foreground">{result.message}</p>
        )}
      </ToolCard>
    );
  },
});

export const ResumeToolUI = makeAssistantToolUI<
  Record<string, never>,
  { success: boolean; message?: string }
>({
  toolName: "resumeCleaning",
  render({ result, status: execStatus }) {
    return (
      <ToolCard icon={PlayCircleIcon} title="继续清扫" loading={execStatus.type === "running"}>
        {result && (
          <p className="text-xs text-muted-foreground">{result.message}</p>
        )}
      </ToolCard>
    );
  },
});

export const ReturnHomeToolUI = makeAssistantToolUI<
  Record<string, never>,
  { success: boolean; message?: string }
>({
  toolName: "returnHome",
  render({ result, status: execStatus }) {
    return (
      <ToolCard icon={HomeIcon} title="返回充电桩" loading={execStatus.type === "running"}>
        {result && (
          <p className="text-xs text-muted-foreground">{result.message}</p>
        )}
      </ToolCard>
    );
  },
});

export const GetRoomsToolUI = makeAssistantToolUI<
  Record<string, never>,
  { success: boolean; rooms?: Record<string, number>; error?: string }
>({
  toolName: "getRooms",
  render({ result, status: execStatus }) {
    const loading = execStatus.type === "running";
    return (
      <ToolCard icon={MapIcon} title="房间列表" loading={loading}>
        {!loading && result?.success && result.rooms && (
          <div className="flex flex-wrap gap-1">
            {Object.keys(result.rooms).map((name) => (
              <Badge key={name} variant="secondary">
                {name}
              </Badge>
            ))}
          </div>
        )}
        {!loading && !result?.success && (
          <p className="text-xs text-red-400">{result?.error}</p>
        )}
      </ToolCard>
    );
  },
});

export const TakePhotoToolUI = makeAssistantToolUI<
  { camera_id?: string },
  { success: boolean; camera_id?: string; message?: string; error?: string }
>({
  toolName: "takePhoto",
  render({ result, status: execStatus }) {
    const loading = execStatus.type === "running";
    return (
      <ToolCard icon={CameraIcon} title="拍摄截图" loading={loading}>
        {!loading && result && (
          <div className="flex items-center gap-2">
            <StatusDot online={result.success} />
            <p className={`text-xs ${result.success ? "text-muted-foreground" : "text-red-400"}`}>
              {result.success ? result.message : result.error}
            </p>
          </div>
        )}
      </ToolCard>
    );
  },
});

export const AnalyzeImageToolUI = makeAssistantToolUI<
  { camera_id?: string },
  {
    success: boolean;
    report?: {
      score: number;
      has_trash: boolean;
      dirty_zones: string[];
      recommendation: string;
      clean_mode: string;
    };
    summary?: string;
    error?: string;
  }
>({
  toolName: "analyzeImage",
  render({ result, status: execStatus }) {
    const loading = execStatus.type === "running";
    if (loading) return <ToolCard icon={EyeIcon} title="AI 卫生分析中..." loading />;

    if (!result?.success) {
      return (
        <ToolCard icon={EyeIcon} title="卫生分析">
          <p className="text-xs text-red-400">{result?.error}</p>
        </ToolCard>
      );
    }

    const r = result.report;
    if (!r) return null;

    return (
      <ToolCard icon={EyeIcon} title="卫生分析报告">
        <div className="space-y-2">
          <div className="flex items-center gap-2">
            <ScoreBadge score={r.score} />
            {r.has_trash && (
              <Badge variant="danger">发现垃圾</Badge>
            )}
          </div>
          {r.dirty_zones.length > 0 && (
            <div className="space-y-1">
              {r.dirty_zones.map((zone, i) => (
                <p key={i} className="text-xs text-muted-foreground">
                  · {zone}
                </p>
              ))}
            </div>
          )}
          <p className="text-xs text-zinc-400 border-t border-border pt-2">
            {r.recommendation}
          </p>
        </div>
      </ToolCard>
    );
  },
});

export const AddTaskToolUI = makeAssistantToolUI<
  { name: string; cron: string; task_type: string; rooms?: string[] },
  { success: boolean; task_id?: number; message?: string; error?: string }
>({
  toolName: "addScheduledTask",
  render({ args, result, status: execStatus }) {
    const loading = execStatus.type === "running";
    return (
      <ToolCard icon={CalendarPlusIcon} title="创建定时任务" loading={loading}>
        <div className="space-y-1.5">
          <div className="flex items-center gap-2">
            <p className="text-xs font-medium">{args.name}</p>
            <Badge variant="outline" className="font-mono text-[10px]">
              {args.cron}
            </Badge>
          </div>
          {!loading && result && (
            <p className={`text-xs ${result.success ? "text-emerald-400" : "text-red-400"}`}>
              {result.success ? result.message : result.error}
            </p>
          )}
        </div>
      </ToolCard>
    );
  },
});

export const ListTasksToolUI = makeAssistantToolUI<
  Record<string, never>,
  {
    success: boolean;
    count?: number;
    tasks?: { id: number; name: string; cron: string; enabled: boolean; last_run_at: string }[];
  }
>({
  toolName: "listScheduledTasks",
  render({ result, status: execStatus }) {
    const loading = execStatus.type === "running";
    if (loading) return <ToolCard icon={ListTodoIcon} title="加载任务列表..." loading />;

    const tasks = result?.tasks ?? [];
    return (
      <ToolCard icon={ListTodoIcon} title={`定时任务 (${result?.count ?? 0})`}>
        {tasks.length === 0 ? (
          <p className="text-xs text-muted-foreground">暂无定时任务</p>
        ) : (
          <div className="space-y-2">
            {tasks.map((t) => (
              <div key={t.id} className="flex items-start justify-between gap-2">
                <div>
                  <p className="text-xs font-medium">{t.name}</p>
                  <p className="text-[10px] font-mono text-muted-foreground">{t.cron}</p>
                </div>
                <Badge variant={t.enabled ? "success" : "secondary"}>
                  {t.enabled ? "启用" : "停用"}
                </Badge>
              </div>
            ))}
          </div>
        )}
      </ToolCard>
    );
  },
});

export const CleaningHistoryToolUI = makeAssistantToolUI<
  { limit?: number },
  { success: boolean; records?: { mode: string; rooms: string; created_at: number }[] }
>({
  toolName: "getCleaningHistory",
  render({ result, status: execStatus }) {
    const loading = execStatus.type === "running";
    if (loading) return <ToolCard icon={ClockIcon} title="加载清扫历史..." loading />;
    const records = result?.records ?? [];
    return (
      <ToolCard icon={ClockIcon} title={`清扫历史 (最近 ${records.length} 条)`}>
        {records.length === 0 ? (
          <p className="text-xs text-muted-foreground">暂无清扫记录</p>
        ) : (
          <div className="space-y-1.5">
            {records.map((r, i) => (
              <div key={i} className="flex items-center gap-2 text-xs">
                <Badge variant="secondary">{r.mode === "full" ? "全屋" : "指定"}</Badge>
                <span className="text-muted-foreground">
                  {new Date(r.created_at * 1000).toLocaleString("zh-CN", {
                    month: "numeric",
                    day: "numeric",
                    hour: "2-digit",
                    minute: "2-digit",
                  })}
                </span>
              </div>
            ))}
          </div>
        )}
      </ToolCard>
    );
  },
});
