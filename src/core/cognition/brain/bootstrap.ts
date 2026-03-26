import { readFileSync, existsSync } from 'fs'
import { join } from 'path'
import { agentDisplayName } from '@/lib/agent-display'

// ─── 配置 ──────────────────────────────────────────────────────────────────

/** Bootstrap 文件列表，按顺序加载并拼接 */
const BOOTSTRAP_FILES = [
  'ARIA.md',
  'HOME.md',
  'FAMILY.md',
] as const

/** 单文件最大字符数（防止单个超大文件撑爆 token 预算）*/
const MAX_FILE_CHARS = 8_000

/** 所有 Bootstrap 文件总字符上限 */
const MAX_TOTAL_CHARS = 20_000

// ─── 工具函数 ───────────────────────────────────────────────────────────────

/**
 * 将内容里的 {VAR_NAME} 占位符替换为对应的环境变量值。
 * `AGENT_DISPLAY_NAME` 未单独在 env 中配置时，与同名的代码默认（见 agent-display.ts）一致。
 * 其他未设置变量替换为空字符串。
 */
function substituteEnvVars(content: string): string {
  return content
    .replace(
      /\{([A-Z_][A-Z0-9_]*)\}/g,
      (_, name: string) =>
        name === 'AGENT_DISPLAY_NAME' ? agentDisplayName() : (process.env[name] ?? ''),
    )
    .replace(/^\s*\n/gm, '\n')  // 压缩多余空行
    .trim()
}

// ─── BootstrapLoader ────────────────────────────────────────────────────────

/**
 * 负责加载根目录的 Bootstrap Markdown 文件，拼接为可注入 system prompt 的字符串。
 *
 * 设计原则：
 * - 进程级单例缓存：首次 load() 读磁盘，后续 0 I/O
 * - 文件不存在 → 静默跳过，不影响进程启动
 * - 支持 {ENV_VAR} 占位符替换（用于 FAMILY.md 等动态内容）
 * - 单文件超 8000 字符 / 总量超 20000 字符时截断并告警
 */
export class BootstrapLoader {
  private cache: string | null = null

  /**
   * 加载所有 Bootstrap 文件，返回拼接好的上下文字符串。
   * 进程首次调用时读文件并缓存，后续返回内存缓存。
   */
  load(): string {
    if (this.cache !== null) return this.cache

    const root = process.cwd()
    const sections: string[] = []
    let totalChars = 0

    for (const filename of BOOTSTRAP_FILES) {
      const filePath = join(root, filename)
      if (!existsSync(filePath)) continue

      try {
        let content = substituteEnvVars(readFileSync(filePath, 'utf-8'))

        if (content.length > MAX_FILE_CHARS) {
          console.warn(
            `[Bootstrap] ${filename} 超过单文件字符限制（${content.length} > ${MAX_FILE_CHARS}），已截断`,
          )
          content = content.slice(0, MAX_FILE_CHARS) + '\n…（内容已截断）'
        }

        const remaining = MAX_TOTAL_CHARS - totalChars
        if (remaining <= 0) {
          console.warn(`[Bootstrap] 已达总字符上限（${MAX_TOTAL_CHARS}），跳过 ${filename}`)
          break
        }

        if (content.length > remaining) {
          console.warn(`[Bootstrap] ${filename} 超出总量剩余预算（${remaining}），已截断`)
          content = content.slice(0, remaining) + '\n…（内容已截断）'
        }

        if (content) {
          sections.push(content)
          totalChars += content.length
        }
      } catch (err) {
        console.error(`[Bootstrap] 读取 ${filename} 失败：`, err)
      }
    }

    this.cache = sections.join('\n\n---\n\n')
    return this.cache
  }

  /** 清除缓存，下次 load() 时重新读取文件（重启/热更新场景使用）。 */
  invalidate(): void {
    this.cache = null
  }
}

export const bootstrapLoader = new BootstrapLoader()
