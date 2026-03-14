"use client";

import { makeAssistantToolUI } from "@assistant-ui/react";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import {
  BatteryChargingIcon,
  CheckCircle2Icon,
  CalendarPlusIcon,
  ListTodoIcon,
  MapIcon,
  PauseCircleIcon,
  PlayCircleIcon,
  HomeIcon,
  ClockIcon,
  AlertCircleIcon,
  Loader2Icon,
  CameraIcon,
  ScanEyeIcon,
  NavigationIcon,
  SquareIcon,
  RefreshCwIcon,
} from "lucide-react";
import { useState, useCallback, useEffect } from "react";

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

export const TakeRobotPhotoToolUI = makeAssistantToolUI<
  Record<string, never>,
  { success: boolean; image_url?: string; timestamp?: number; error?: string; hint?: string }
>({
  toolName: "takeRobotPhoto",
  render({ result, status: execStatus }) {
    const loading = execStatus.type === "running";
    if (loading) return <ToolCard icon={CameraIcon} title="拍照中..." loading />;
    if (!result?.success) {
      return (
        <ToolCard icon={CameraIcon} title="拍照失败">
          <p className="text-xs text-red-400">{result?.error}</p>
          {result?.hint && <p className="text-[11px] text-muted-foreground mt-1">{result.hint}</p>}
        </ToolCard>
      );
    }
    return (
      <ToolCard icon={CameraIcon} title="机器车视角">
        {result.image_url && (
          <img
            src={result.image_url}
            alt="机器车摄像头截图"
            className="w-full rounded-md object-cover"
          />
        )}
        {result.timestamp && (
          <p className="text-[10px] text-muted-foreground mt-1">
            {new Date(result.timestamp).toLocaleTimeString("zh-CN")}
          </p>
        )}
      </ToolCard>
    );
  },
});

export const MoveCameraMountToolUI = makeAssistantToolUI<
  { pan?: number; tilt?: number },
  { success: boolean; pan?: number; tilt?: number; error?: string }
>({
  toolName: "moveCameraMount",
  render({ args, result, status: execStatus }) {
    const loading = execStatus.type === "running";
    return (
      <ToolCard icon={ScanEyeIcon} title="调整云台" loading={loading}>
        <div className="flex gap-2 text-xs flex-wrap">
          {args.pan !== undefined && (
            <Badge variant="outline">水平 {args.pan}°</Badge>
          )}
          {args.tilt !== undefined && (
            <Badge variant="outline">俯仰 {args.tilt}°</Badge>
          )}
        </div>
        {!loading && result && !result.success && (
          <p className="text-xs text-red-400 mt-1">{result.error}</p>
        )}
      </ToolCard>
    );
  },
});

/**
 * GetMapImageToolUI
 * -----------------
 * 工具只传元数据（scan_count / pose / fetch_ts），图片完全由前端自主拉取渲染。
 * 这样图片二进制数据永远不进入对话上下文，不占用 LLM token。
 *
 * 渲染流程：
 *   result 到达 → useEffect 触发 → fetch(/api/slam/map) → blob URL → <img>
 */
export const GetMapImageToolUI = makeAssistantToolUI<
  Record<string, never>,
  {
    success: boolean;
    scan_count?: number;
    exploring?: boolean;
    fetch_ts?: number;
    pose?: { x_mm: number; y_mm: number; theta_deg: number };
    error?: string;
  }
>({
  toolName: "getMapImage",
  render({ result, status: execStatus }) {
    const loading = execStatus.type === "running";

    // eslint-disable-next-line react-hooks/rules-of-hooks
    const [blobUrl, setBlobUrl] = useState<string | null>(null);
    // eslint-disable-next-line react-hooks/rules-of-hooks
    const [fetchState, setFetchState] = useState<"idle" | "loading" | "ok" | "error">("idle");
    // eslint-disable-next-line react-hooks/rules-of-hooks
    const [fetchTs, setFetchTs] = useState<number>(result?.fetch_ts ?? Date.now());

    // 当工具结果到达后，由前端自主拉取地图图片
    // eslint-disable-next-line react-hooks/rules-of-hooks
    useEffect(() => {
      if (!result?.success) return;
      let cancelled = false;

      const prev = blobUrl;
      setFetchState("loading");

      fetch(`/api/slam/map?t=${fetchTs}`)
        .then(async (res) => {
          if (!res.ok) throw new Error(`HTTP ${res.status}`);
          const blob = await res.blob();
          if (cancelled) return;
          const url = URL.createObjectURL(blob);
          setBlobUrl(url);
          setFetchState("ok");
          // 释放上一张图的 blob URL，避免内存泄漏
          if (prev) URL.revokeObjectURL(prev);
        })
        .catch(() => {
          if (!cancelled) setFetchState("error");
        });

      return () => { cancelled = true; };
    // fetchTs 变化时重新拉取（刷新按钮触发）
    // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [result?.success, fetchTs]);

    // eslint-disable-next-line react-hooks/rules-of-hooks
    const refresh = useCallback(() => setFetchTs(Date.now()), []);

    if (loading) return <ToolCard icon={MapIcon} title="查询地图状态..." loading />;

    if (!result?.success) {
      return (
        <ToolCard icon={MapIcon} title="地图">
          <p className="text-xs text-red-400">{result?.error}</p>
        </ToolCard>
      );
    }

    return (
      <ToolCard icon={MapIcon} title="当前地图">
        {/* ── 图片区域 ── */}
        <div className="relative w-full rounded-md overflow-hidden bg-muted min-h-[140px] flex items-center justify-center">
          {fetchState === "loading" && (
            <div className="flex flex-col items-center gap-2 text-muted-foreground">
              <Loader2Icon className="w-6 h-6 animate-spin" />
              <span className="text-xs">正在加载地图...</span>
            </div>
          )}

          {fetchState === "error" && (
            <div className="flex flex-col items-center gap-2 p-4">
              <AlertCircleIcon className="w-5 h-5 text-red-400" />
              <p className="text-xs text-red-400 text-center">
                地图加载失败
                <br />
                <span className="text-muted-foreground text-[10px]">请确认 Platform 正在运行</span>
              </p>
              <button
                onClick={refresh}
                className="flex items-center gap-1 text-xs text-primary hover:underline mt-1"
              >
                <RefreshCwIcon className="w-3 h-3" /> 重试
              </button>
            </div>
          )}

          {fetchState === "ok" && blobUrl && (
            <img
              src={blobUrl}
              alt="SLAM 地图"
              className="w-full rounded-md"
              style={{ imageRendering: "pixelated" }}
            />
          )}
        </div>

        {/* ── 状态徽章 ── */}
        <div className="flex flex-wrap items-center gap-2 mt-2 text-xs">
          <Badge variant={result.exploring ? "default" : "secondary"}>
            {result.exploring ? "🔍 建图中" : "已停止"}
          </Badge>
          {result.scan_count !== undefined && (
            <Badge variant="outline">已扫 {result.scan_count} 圈</Badge>
          )}
          {result.pose && (
            <Badge variant="outline" className="font-mono text-[10px]">
              x:{(result.pose.x_mm / 1000).toFixed(1)}m&nbsp;
              y:{(result.pose.y_mm / 1000).toFixed(1)}m&nbsp;
              {Math.round(result.pose.theta_deg)}°
            </Badge>
          )}
          {fetchState === "ok" && (
            <button
              onClick={refresh}
              className="flex items-center gap-1 text-[10px] text-muted-foreground hover:text-primary ml-auto"
            >
              <RefreshCwIcon className="w-3 h-3" /> 刷新地图
            </button>
          )}
        </div>
      </ToolCard>
    );
  },
});

export const StartExploringToolUI = makeAssistantToolUI<
  Record<string, never>,
  { success: boolean; message?: string; error?: string }
>({
  toolName: "startExploring",
  render({ result, status: execStatus }) {
    const loading = execStatus.type === "running";
    return (
      <ToolCard icon={NavigationIcon} title="自主建图" loading={loading}>
        {!loading && result && (
          <p className={`text-xs ${result.success ? "text-emerald-400" : "text-red-400"}`}>
            {result.success ? result.message : result.error}
          </p>
        )}
      </ToolCard>
    );
  },
});

export const StopExploringToolUI = makeAssistantToolUI<
  { save_name?: string },
  { success: boolean; message?: string; map_name?: string; error?: string }
>({
  toolName: "stopExploring",
  render({ result, status: execStatus }) {
    const loading = execStatus.type === "running";
    return (
      <ToolCard icon={SquareIcon} title="停止建图" loading={loading}>
        {!loading && result && (
          <div className="space-y-1">
            <p className={`text-xs ${result.success ? "text-emerald-400" : "text-red-400"}`}>
              {result.success ? result.message : result.error}
            </p>
            {result.map_name && (
              <Badge variant="outline" className="text-[10px]">
                📦 {result.map_name}
              </Badge>
            )}
          </div>
        )}
      </ToolCard>
    );
  },
});

export const GetMapStatusToolUI = makeAssistantToolUI<
  Record<string, never>,
  { success: boolean; is_mapping?: boolean; scan_count?: number; exploring?: boolean; pose?: { x_mm: number; y_mm: number; theta_deg: number }; error?: string }
>({
  toolName: "getMapStatus",
  render({ result, status: execStatus }) {
    const loading = execStatus.type === "running";
    if (loading) return <ToolCard icon={MapIcon} title="查询地图状态..." loading />;
    if (!result?.success) {
      return (
        <ToolCard icon={MapIcon} title="地图状态">
          <p className="text-xs text-red-400">{result?.error}</p>
        </ToolCard>
      );
    }
    return (
      <ToolCard icon={MapIcon} title="地图状态">
        <div className="flex flex-wrap gap-2 text-xs">
          <Badge variant={result.exploring ? "success" : "secondary"}>
            {result.exploring ? "🔍 探索中" : "已停止"}
          </Badge>
          {result.scan_count !== undefined && (
            <Badge variant="outline">已扫 {result.scan_count} 圈</Badge>
          )}
          {result.pose && (
            <Badge variant="outline" className="font-mono text-[10px]">
              x:{Math.round(result.pose.x_mm)} y:{Math.round(result.pose.y_mm)}
            </Badge>
          )}
        </div>
      </ToolCard>
    );
  },
});

export const CenterCameraMountToolUI = makeAssistantToolUI<
  Record<string, never>,
  { success: boolean; status?: { pan: number; tilt: number }; error?: string }
>({
  toolName: "centerCameraMount",
  render({ result, status: execStatus }) {
    const loading = execStatus.type === "running";
    return (
      <ToolCard icon={ScanEyeIcon} title="云台归中" loading={loading}>
        {!loading && result?.success && result.status && (
          <p className="text-xs text-muted-foreground">
            Pan {result.status.pan}° · Tilt {result.status.tilt}°
          </p>
        )}
        {!loading && result && !result.success && (
          <p className="text-xs text-red-400">{result.error}</p>
        )}
      </ToolCard>
    );
  },
});
