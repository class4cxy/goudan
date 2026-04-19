"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { AlertTriangleIcon } from "lucide-react";

type TeleopResponse = {
  ok: boolean;
  blocked?: boolean;
  reason?: string;
  message?: string;
  front_min_mm?: number | null;
  min_safe_mm?: number;
};

function clamp(v: number, min: number, max: number): number {
  return Math.max(min, Math.min(max, v));
}

export function TeleopPanel({ enabled }: { enabled: boolean }) {
  const [throttle, setThrottle] = useState(0);
  const [steer, setSteer] = useState(0);
  const [dragging, setDragging] = useState(false);
  const [blockedMessage, setBlockedMessage] = useState<string | null>(null);
  const padRef = useRef<HTMLDivElement | null>(null);

  const knobStyle = useMemo(() => {
    const x = steer * 36;
    const y = -throttle * 36;
    return { transform: `translate(${x}px, ${y}px)` };
  }, [steer, throttle]);

  const updateFromClientPoint = useCallback((clientX: number, clientY: number) => {
    const pad = padRef.current;
    if (!pad) return;
    const rect = pad.getBoundingClientRect();
    const cx = rect.left + rect.width / 2;
    const cy = rect.top + rect.height / 2;
    const dx = clientX - cx;
    const dy = clientY - cy;
    const radius = rect.width * 0.5;

    const ndx = clamp(dx / radius, -1, 1);
    const ndy = clamp(dy / radius, -1, 1);
    setSteer(Number(ndx.toFixed(3)));
    setThrottle(Number((-ndy).toFixed(3)));
  }, []);

  const resetAxes = useCallback(() => {
    setThrottle(0);
    setSteer(0);
  }, []);

  const stopNow = useCallback(async () => {
    resetAxes();
    try {
      await fetch("/api/teleop", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ action: "command", throttle: 0, steer: 0 }),
      });
    } catch {
      // ignore network jitter on emergency stop path
    }
  }, [resetAxes]);

  useEffect(() => {
    if (!enabled) {
      resetAxes();
      setBlockedMessage(null);
    }
  }, [enabled, resetAxes]);

  useEffect(() => {
    if (!enabled) return;
    const timer = window.setInterval(async () => {
      try {
        const res = await fetch("/api/teleop", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            action: "command",
            throttle,
            steer,
          }),
        });
        const json = (await res.json().catch(() => null)) as TeleopResponse | null;
        if (!res.ok) {
          setBlockedMessage(json?.message ?? "遥控命令发送失败");
          return;
        }
        if (json?.ok && !json.blocked) {
          setBlockedMessage(null);
        } else if (json?.blocked) {
          if (json.reason === "lidar_too_close") {
            setBlockedMessage(
              `前方障碍过近（${json.front_min_mm ?? "?"}mm < ${json.min_safe_mm ?? "?"}mm），已阻止前进`,
            );
          } else if (json.reason === "lidar_stale") {
            setBlockedMessage("LiDAR 数据不新鲜，已阻止前进");
          } else if (json.reason === "lidar_unavailable") {
            setBlockedMessage("LiDAR 不可用，已阻止前进");
          } else {
            setBlockedMessage(json.message ?? "安全限制触发，已阻止前进");
          }
        }
      } catch {
        setBlockedMessage("遥控链路暂时不可用");
      }
    }, 50);
    return () => window.clearInterval(timer);
  }, [enabled, throttle, steer]);

  useEffect(() => {
    if (!enabled) return;

    const onPointerMove = (e: PointerEvent) => {
      if (!dragging) return;
      updateFromClientPoint(e.clientX, e.clientY);
    };
    const onPointerUp = () => {
      if (!dragging) return;
      setDragging(false);
      resetAxes();
    };

    window.addEventListener("pointermove", onPointerMove);
    window.addEventListener("pointerup", onPointerUp);
    return () => {
      window.removeEventListener("pointermove", onPointerMove);
      window.removeEventListener("pointerup", onPointerUp);
    };
  }, [dragging, resetAxes, updateFromClientPoint, enabled]);

  if (!enabled) return null;

  return (
    <div className="mb-2 rounded-xl border border-border bg-zinc-900 p-3">
      <div className="flex items-start justify-between gap-3">
        <div>
          <p className="text-xs font-medium text-foreground">遥控模式</p>
          <p className="text-[11px] text-muted-foreground">
            摇杆控制底盘；前向运动自动受 LiDAR 最小安全距离约束
          </p>
        </div>
        <button
          onClick={stopNow}
          className="rounded-md border border-red-500/40 bg-red-500/10 px-2.5 py-1 text-[11px] text-red-300 hover:bg-red-500/20"
        >
          急停
        </button>
      </div>

      <div className="mt-3 flex items-center gap-4">
        <div
          ref={padRef}
          onPointerDown={(e) => {
            setDragging(true);
            updateFromClientPoint(e.clientX, e.clientY);
          }}
          className="relative h-28 w-28 touch-none rounded-full border border-zinc-700 bg-zinc-950"
        >
          <div className="absolute left-1/2 top-0 h-full w-px -translate-x-1/2 bg-zinc-800" />
          <div className="absolute left-0 top-1/2 h-px w-full -translate-y-1/2 bg-zinc-800" />
          <div
            style={knobStyle}
            className="absolute left-1/2 top-1/2 h-9 w-9 -translate-x-1/2 -translate-y-1/2 rounded-full bg-primary/80 shadow"
          />
        </div>

        <div className="space-y-1 text-[11px] text-muted-foreground">
          <p>前后（throttle）：{throttle.toFixed(2)}</p>
          <p>转向（steer）：{steer.toFixed(2)}</p>
          <p className="text-[10px]">松手自动回中；watchdog 超时会自动刹停</p>
        </div>
      </div>

      {blockedMessage && (
        <div className="mt-3 flex items-start gap-2 rounded-md border border-amber-500/30 bg-amber-500/10 px-2 py-1.5 text-[11px] text-amber-200">
          <AlertTriangleIcon className="mt-0.5 h-3.5 w-3.5 shrink-0" />
          <p>{blockedMessage}</p>
        </div>
      )}
    </div>
  );
}
