import { PlatformConnector } from "@/core/runtime/platform-connector";

export const runtime = "nodejs";

/**
 * POST /api/speak
 * 将文字发送到 Platform，通过蓝牙扬声器 TTS 朗读。
 * body: { text: string; interrupt?: boolean }
 */
export async function POST(req: Request) {
  const { text, interrupt = true } = (await req.json()) as {
    text: string;
    interrupt?: boolean;
  };

  if (!text?.trim()) {
    return Response.json({ ok: false, error: "text is required" }, { status: 400 });
  }

  const connected = PlatformConnector.isConnected;
  console.log(`[/api/speak] isConnected=${connected}，发送 TTS："${text.slice(0, 60)}..."`);

  PlatformConnector.send({
    type: "action.speak",
    payload: { text: text.trim(), interrupt_current: interrupt },
  });

  return Response.json({ ok: true, connected });
}
