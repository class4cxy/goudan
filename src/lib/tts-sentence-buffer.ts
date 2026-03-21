/**
 * TtsSentenceBuffer — 流式 TTS 句子边界检测缓冲区
 * ==================================================
 * 将 LLM 逐 token 输出累积成完整句子，再批量送入 TTS 队列，
 * 实现"边生成边播放"的低延迟语音输出。
 *
 * 句子边界规则（按优先级）：
 *  1. 中文结束标点：。！？…（含连续多个）
 *  2. 双换行（段落分隔）
 *  3. 英文结束标点 .!? 后接空白或行尾，且前一字符非数字（避免 3.14 误分）
 *
 * 使用方式：
 *  const buf = new TtsSentenceBuffer();
 *  for await (const chunk of stream) {
 *    const sentences = buf.push(chunk);
 *    for (const s of sentences) sendToTTS(s);
 *  }
 *  const last = buf.flush();
 *  if (last) sendToTTS(last);
 */

import { stripMarkdown } from "@/lib/strip-markdown";

// 中文句末标点集合（单字符匹配，快速）
const ZH_ENDS = new Set(["。", "！", "？", "…"]);

// 最短句子长度（字符数），避免单字或极短片段被单独 TTS
const MIN_SENTENCE_LENGTH = 5;

export class TtsSentenceBuffer {
  private _buf = "";

  /**
   * 向缓冲区追加新 token，返回当前已完整的句子列表（已去除 Markdown）。
   */
  push(chunk: string): string[] {
    this._buf += chunk;
    return this._extract();
  }

  /**
   * 将缓冲区剩余内容作为最后一句输出并清空。
   * 返回 null 表示没有剩余有效文字。
   */
  flush(): string | null {
    const raw = this._buf.trim();
    this._buf = "";
    if (!raw) return null;
    const plain = stripMarkdown(raw).trim();
    return plain.length >= MIN_SENTENCE_LENGTH ? plain : null;
  }

  /** 清空缓冲区（中断时使用）。 */
  clear(): void {
    this._buf = "";
  }

  // ─── 内部实现 ──────────────────────────────────────────────────

  private _extract(): string[] {
    const results: string[] = [];
    let start = 0;
    const text = this._buf;
    let i = 0;

    while (i < text.length) {
      const ch = text[i];

      // ① 中文句末标点
      if (ZH_ENDS.has(ch)) {
        // 吞掉连续标点（如 ！！、……）
        let end = i + 1;
        while (end < text.length && ZH_ENDS.has(text[end])) end++;
        const seg = text.slice(start, end).trim();
        if (seg.length >= MIN_SENTENCE_LENGTH) {
          const plain = stripMarkdown(seg).trim();
          if (plain.length >= MIN_SENTENCE_LENGTH) results.push(plain);
        }
        start = end;
        i = end;
        continue;
      }

      // ② 段落分隔（双换行）
      if (ch === "\n" && i + 1 < text.length && text[i + 1] === "\n") {
        const seg = text.slice(start, i).trim();
        if (seg.length >= MIN_SENTENCE_LENGTH) {
          const plain = stripMarkdown(seg).trim();
          if (plain.length >= MIN_SENTENCE_LENGTH) results.push(plain);
        }
        // 跳过连续空行
        let end = i + 2;
        while (end < text.length && text[end] === "\n") end++;
        start = end;
        i = end;
        continue;
      }

      // ③ 英文句末标点（.!?），避免小数点误分
      if ((ch === "!" || ch === "?") && i > start) {
        const next = text[i + 1];
        if (!next || next === " " || next === "\n") {
          const seg = text.slice(start, i + 1).trim();
          if (seg.length >= MIN_SENTENCE_LENGTH) {
            const plain = stripMarkdown(seg).trim();
            if (plain.length >= MIN_SENTENCE_LENGTH) results.push(plain);
          }
          start = i + 1;
          i = i + 1;
          continue;
        }
      }
      if (ch === "." && i > start) {
        const prev = text[i - 1];
        const next = text[i + 1];
        const isDecimal = /\d/.test(prev);
        const isSentenceEnd = !next || next === " " || next === "\n";
        if (!isDecimal && isSentenceEnd) {
          const seg = text.slice(start, i + 1).trim();
          if (seg.length >= MIN_SENTENCE_LENGTH) {
            const plain = stripMarkdown(seg).trim();
            if (plain.length >= MIN_SENTENCE_LENGTH) results.push(plain);
          }
          start = i + 1;
          i = i + 1;
          continue;
        }
      }

      i++;
    }

    // 保留未完整的尾部
    this._buf = text.slice(start);
    return results;
  }
}
