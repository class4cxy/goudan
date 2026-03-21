/**
 * POST /api/transcribe
 * ====================
 * 接收前端录制的音频 Blob（multipart form-data，字段名 "audio"），完成两步处理：
 *   1. STT    — 调用 SPEECH_API_URL / SPEECH_API_KEY（Qwen ASR 风格网关）
 *   2. 润色   — 调用 typeless-sdk polish（DEEPSEEK_API_KEY），
 *               SDK prompt 已修复：明确禁止回答问题，只做文本整理
 *
 * 返回：{ text: string }
 */

import { type SttAdapter, VoiceTextSDK } from "typeless-sdk";

export const runtime = "nodejs";

const STT_MODEL = process.env.SPEECH_STT_MODEL ?? "qwen3-asr-flash";

// ── 音频 MIME 工具 ────────────────────────────────────────────────
function mimeToExt(mimeType: string): string {
  const m = mimeType.split(";")[0]?.trim().toLowerCase() ?? "";
  if (m.includes("webm")) return "webm";
  if (m.includes("ogg"))  return "ogg";
  if (m.includes("mp4") || m.includes("m4a")) return "m4a";
  if (m.includes("wav"))  return "wav";
  if (m.includes("mpeg") || m.includes("mp3")) return "mp3";
  return "webm";
}

function normMime(mimeType: string): string {
  const m = mimeType.split(";")[0]?.trim().toLowerCase() ?? "";
  if (m === "audio/mp3") return "audio/mpeg";
  if (m === "audio/m4a" || m === "audio/x-m4a") return "audio/mp4";
  if (m.startsWith("audio/")) return m;
  return "audio/webm";
}

async function toDataUri(blob: Blob, contentType: string): Promise<string> {
  const buf = await blob.arrayBuffer();
  const bytes = new Uint8Array(buf);
  let binary = "";
  for (let i = 0; i < bytes.byteLength; i++) binary += String.fromCharCode(bytes[i]);
  return `data:${contentType};base64,${btoa(binary)}`;
}

// ── SDK 单例 ──────────────────────────────────────────────────────
function buildSdk(): VoiceTextSDK {
  const speechApiUrl = process.env.SPEECH_API_URL;
  const speechApiKey = process.env.SPEECH_API_KEY;
  if (!speechApiUrl || !speechApiKey) {
    throw new Error("[transcribe] 缺少 SPEECH_API_URL 或 SPEECH_API_KEY");
  }

  // 自定义 STT adapter：Qwen ASR 风格（base64 via chat/completions）
  const stt: SttAdapter = async (audio, filename = "audio.webm") => {
    if (typeof audio === "string") throw new Error("file-path 不支持");

    const ext = filename.split(".").pop()?.toLowerCase() ?? "webm";
    const contentType = normMime(`audio/${ext}`);
    const blob = new Blob([new Uint8Array(audio)], { type: contentType });
    const dataUri = await toDataUri(blob, contentType);

    console.log(`[STT] → model=${STT_MODEL} size=${(dataUri.length / 1024).toFixed(1)}KB`);

    const res = await fetch(speechApiUrl, {
      method: "POST",
      headers: { Authorization: `Bearer ${speechApiKey}`, "Content-Type": "application/json" },
      signal: AbortSignal.timeout(60_000),
      body: JSON.stringify({
        model: STT_MODEL,
        messages: [{ role: "user", content: [{ type: "input_audio", input_audio: { data: dataUri } }] }],
        stream: false,
      }),
    });

    if (!res.ok) {
      const body = await res.text().catch(() => "");
      throw new Error(`STT API 失败：${res.status} ${body.slice(0, 200)}`);
    }

    const data = await res.json() as { choices?: Array<{ message?: { content?: string } }> };
    const result = (data.choices?.[0]?.message?.content ?? "").trim();
    console.log(`[STT] ← "${result}"`);
    return result;
  };

  return new VoiceTextSDK({
    stt,
    llm: {
      baseUrl: (process.env.DEEPSEEK_BASE_URL ?? "https://api.deepseek.com"),
      apiKey:  process.env.DEEPSEEK_API_KEY ?? "",
      model:   process.env.STT_LLM_MODEL ?? "deepseek-chat",
      // temperature 已在 SDK 层改为 0.1，此处无需重复设置
    },
  });
}

declare global {
  // eslint-disable-next-line no-var
  var __typelessSdk: VoiceTextSDK | undefined;
}
function getSdk(): VoiceTextSDK {
  if (!globalThis.__typelessSdk) globalThis.__typelessSdk = buildSdk();
  return globalThis.__typelessSdk;
}

// ── 请求处理 ─────────────────────────────────────────────────────
export async function POST(req: Request) {
  let sdk: VoiceTextSDK;
  try {
    sdk = getSdk();
  } catch (e) {
    const msg = e instanceof Error ? e.message : String(e);
    console.error(msg);
    return Response.json({ error: msg }, { status: 500 });
  }

  let formData: FormData;
  try {
    formData = await req.formData();
  } catch {
    return Response.json({ error: "需要 multipart/form-data" }, { status: 400 });
  }

  const audioField = formData.get("audio");
  if (!audioField || !(audioField instanceof Blob)) {
    return Response.json({ error: "缺少 audio 字段" }, { status: 400 });
  }

  const ext      = mimeToExt(audioField.type || "audio/webm");
  const filename = `recording.${ext}`;
  const buffer   = Buffer.from(await audioField.arrayBuffer());

  console.log(`[transcribe] ${(buffer.byteLength / 1024).toFixed(1)}KB (${audioField.type})`);

  try {
    // Step 1: STT
    const transcript = await sdk.transcribe(buffer, filename);
    if (!transcript) return Response.json({ error: "转录结果为空" }, { status: 422 });

    // Step 2: 润色（SDK prompt 已明确禁止回答问题，只整理口语）
    const text = await sdk.polish(transcript, { appType: "general" })
      .catch((err: unknown) => {
        console.warn("[polish] 失败，回退原始转录：", err);
        return transcript;
      });

    console.log(`[polish] "${transcript}" → "${text}"`);
    return Response.json({ text });
  } catch (e) {
    const msg = e instanceof Error ? e.message : String(e);
    console.error("[transcribe] 失败：", msg);
    return Response.json({ error: msg }, { status: 500 });
  }
}
