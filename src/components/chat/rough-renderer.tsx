"use client";

import { useEffect, useRef } from "react";
import rough from "roughjs";
import type { Options as RoughOptions } from "roughjs/bin/core";

// ── Types ─────────────────────────────────────────────────────────

type RoughOpts = RoughOptions | undefined;

type DrawCommand =
  | { type: "rect"; x: number; y: number; w: number; h: number; opts?: RoughOpts }
  | { type: "circle"; x: number; y: number; d: number; opts?: RoughOpts }
  | { type: "ellipse"; x: number; y: number; w: number; h: number; opts?: RoughOpts }
  | { type: "line"; x1: number; y1: number; x2: number; y2: number; opts?: RoughOpts }
  | { type: "polygon"; points: [number, number][]; opts?: RoughOpts }
  | { type: "path"; d: string; opts?: RoughOpts }
  | { type: "text"; x: number; y: number; text: string; size?: number; color?: string; font?: string };

export type DrawScene = {
  width: number;
  height: number;
  background?: string;
  title?: string;
  commands: DrawCommand[];
};

// ── Component ─────────────────────────────────────────────────────

export function RoughRenderer({ scene }: { scene: DrawScene }) {
  const canvasRef = useRef<HTMLCanvasElement>(null);

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;

    const ctx = canvas.getContext("2d");
    if (!ctx) return;

    ctx.clearRect(0, 0, scene.width, scene.height);

    if (scene.background) {
      ctx.fillStyle = scene.background;
      ctx.fillRect(0, 0, scene.width, scene.height);
    }

    const rc = rough.canvas(canvas);

    for (const cmd of scene.commands) {
      switch (cmd.type) {
        case "rect":
          rc.rectangle(cmd.x, cmd.y, cmd.w, cmd.h, cmd.opts);
          break;
        case "circle":
          rc.circle(cmd.x, cmd.y, cmd.d, cmd.opts);
          break;
        case "ellipse":
          rc.ellipse(cmd.x, cmd.y, cmd.w, cmd.h, cmd.opts);
          break;
        case "line":
          rc.line(cmd.x1, cmd.y1, cmd.x2, cmd.y2, cmd.opts);
          break;
        case "polygon":
          rc.polygon(cmd.points, cmd.opts);
          break;
        case "path":
          rc.path(cmd.d, cmd.opts);
          break;
        case "text": {
          const size = cmd.size ?? 16;
          ctx.font = cmd.font ? cmd.font : `${size}px sans-serif`;
          ctx.fillStyle = cmd.color ?? "#333333";
          ctx.fillText(cmd.text, cmd.x, cmd.y);
          break;
        }
      }
    }
  }, [scene]);

  return (
    <canvas
      ref={canvasRef}
      width={scene.width}
      height={scene.height}
      className="block rounded-lg max-w-full"
    />
  );
}
