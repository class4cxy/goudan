/**
 * POST /api/transcribe
 * ====================
 * 接收前端录制的音频 Blob（multipart form-data，字段名 "audio"），
 * 通过 typeless-sdk 完成：
 *   1. STT  — 复用项目已有的 SPEECH_API_URL / SPEECH_API_KEY（Qwen ASR 风格）
 *   2. LLM  — 复用项目已有的 DEEPSEEK_API_KEY 进行润色
 *
 * 无需额外配置，所有凭据均来自项目已有环境变量。
 *
 * 返回：{ text: string; transcript: string; polishedText: string }
 */

import { type SttAdapter, VoiceTextSDK } from "typeless-sdk";

export const runtime = "nodejs";

// ── STT 模型（可通过 SPEECH_STT_MODEL 覆盖） ─────────────────────
const STT_MODEL = process.env.SPEECH_STT_MODEL ?? "qwen3-asr-flash";

// ── 音频 MIME → 扩展名 ────────────────────────────────────────────
function mimeToExt(mimeType: string): string {
  const m = mimeType.split(";")[0]?.trim().toLowerCase() ?? "";
  if (m.includes("webm")) return "webm";
  if (m.includes("ogg"))  return "ogg";
  if (m.includes("mp4") || m.includes("m4a")) return "m4a";
  if (m.includes("wav"))  return "wav";
  if (m.includes("mpeg") || m.includes("mp3")) return "mp3";
  return "webm"; // 浏览器默认 MediaRecorder 格式
}

function normMime(mimeType: string): string {
  const m = mimeType.split(";")[0]?.trim().toLowerCase() ?? "";
  if (m === "audio/mp3") return "audio/mpeg";
  if (m === "audio/m4a" || m === "audio/x-m4a") return "audio/mp4";
  if (m.startsWith("audio/")) return m;
  return "audio/webm";
}

// ── Blob → base64 data URI ────────────────────────────────────────
async function toDataUri(blob: Blob, contentType: string): Promise<string> {
  const buf = await blob.arrayBuffer();
  const bytes = new Uint8Array(buf);
  let binary = "";
  for (let i = 0; i < bytes.byteLength; i++) binary += String.fromCharCode(bytes[i]);
  return `data:${contentType};base64,${btoa(binary)}`;
}

// ── 单例 SDK ──────────────────────────────────────────────────────
function buildSdk(): VoiceTextSDK {
  const speechApiUrl = process.env.SPEECH_API_URL;
  const speechApiKey = process.env.SPEECH_API_KEY;
  const deepseekKey  = process.env.DEEPSEEK_API_KEY;
  const deepseekBase = process.env.DEEPSEEK_BASE_URL ?? "https://api.deepseek.com";

  if (!speechApiUrl || !speechApiKey) {
    throw new Error(
      "[/api/transcribe] 缺少 SPEECH_API_URL 或 SPEECH_API_KEY，请在 .env 中配置"
    );
  }
  if (!deepseekKey) {
    throw new Error(
      "[/api/transcribe] 缺少 DEEPSEEK_API_KEY，请在 .env 中配置"
    );
  }

  // 自定义 STT adapter：Qwen ASR 风格（base64 audio via chat/completions）
  const stt: SttAdapter = async (audio, filename = "audio.webm") => {
    if (typeof audio === "string") {
      throw new Error("file-path input is not supported in server context");
    }

    const ext = filename.split(".").pop()?.toLowerCase() ?? "webm";
    const contentType = normMime(`audio/${ext}`);
    const blob = new Blob([new Uint8Array(audio)], { type: contentType });
    const dataUri = await toDataUri(blob, contentType);

    const res = await fetch(speechApiUrl, {
      method: "POST",
      headers: {
        Authorization: `Bearer ${speechApiKey}`,
        "Content-Type": "application/json",
      },
      signal: AbortSignal.timeout(60_000),
      body: JSON.stringify({
        model: STT_MODEL,
        messages: [
          {
            role: "user",
            content: [{ type: "input_audio", input_audio: { data: dataUri } }],
          },
        ],
        stream: false,
      }),
    });

    if (!res.ok) {
      const body = await res.text().catch(() => "");
      throw new Error(`STT API 失败：${res.status} ${body.slice(0, 200)}`);
    }

    const data = await res.json() as {
      choices?: Array<{ message?: { content?: string } }>;
    };
    return (data.choices?.[0]?.message?.content ?? "").trim();
  };

  return new VoiceTextSDK({
    stt,
    llm: {
      baseUrl: deepseekBase,
      apiKey:  deepseekKey,
      model:   process.env.STT_LLM_MODEL ?? "deepseek-chat",
    },
  });
}

declare global {
  // eslint-disable-next-line no-var
  var __typelessSdk: VoiceTextSDK | undefined;
}
function getSdk(): VoiceTextSDK {
  if (!globalThis.__typelessSdk) {
    globalThis.__typelessSdk = buildSdk();
  }
  return globalThis.__typelessSdk;
}

// ── 词库 ─────────────────────────────────────────────────────────
function getVocabulary(): string[] {
  try {
    return JSON.parse(process.env.STT_VOCABULARY ?? "[]") as string[];
  } catch {
    return [];
  }
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
  const polish   = process.env.STT_POLISH !== "false";
  const vocabulary = getVocabulary();

  console.log(
    `[/api/transcribe] ${(buffer.byteLength / 1024).toFixed(1)}KB (${audioField.type}) polish=${polish} vocab=${vocabulary.length}`
  );

  try {
    const { transcript, polishedText } = await sdk.process(buffer, {
      vocabulary,
      appType: "chat",
      polish,
    } as Parameters<typeof sdk.process>[1] & { filename?: string });

    console.log(`[/api/transcribe] "${transcript}" → "${polishedText}"`);
    return Response.json({ text: polish ? polishedText : transcript, transcript, polishedText });
  } catch (e) {
    const msg = e instanceof Error ? e.message : String(e);
    console.error("[/api/transcribe] 失败：", msg);
    return Response.json({ error: msg }, { status: 500 });
  }
}
