#!/usr/bin/env node
/**
 * 问题：TypeError: e.findLast is not a function (messages.findLast 报错)
 *
 * 原因：@assistant-ui/react-ai-sdk 使用 ES2023 的 Array.findLast。
 * 运行时传入的 messages 并非原生 Array（如 Immer/Proxy），不继承 Array.prototype，
 * 因此 findLast polyfill 无效。需在调用点改为兼容写法。
 *
 * 修改：对 useExternalHistory、useStreamingTiming、usage 三处做兼容，
 * 当 findLast 不存在时用 [...msgs].reverse().find() 替代。
 *
 * 执行：postinstall 时自动运行
 */
const fs = require("fs");
const path = require("path");

const PATCHES = [
  {
    file: "dist/ui/use-chat/useExternalHistory.js",
    old: `if (boundaries.length === 1 && durationMs != null) {
                    const lastAssistant = latest.messages.findLast((m) => m.role === "assistant");`,
    new: `if (boundaries.length === 1 && durationMs != null) {
                    const msgs = latest.messages || [];
                    const lastAssistant = (Array.isArray(msgs) && typeof msgs.findLast === "function")
                        ? msgs.findLast((m) => m.role === "assistant")
                        : [...msgs].reverse().find((m) => m.role === "assistant");`,
    done: "const msgs = latest.messages || []",
  },
  {
    file: "dist/ui/use-chat/useStreamingTiming.js",
    old: `const lastAssistant = messages.findLast((m) => m.role === "assistant");`,
    new: `const msgs = messages || [];
        const lastAssistant = (Array.isArray(msgs) && typeof msgs.findLast === "function")
            ? msgs.findLast((m) => m.role === "assistant")
            : [...msgs].reverse().find((m) => m.role === "assistant");`,
    done: "const msgs = messages || []",
  },
  {
    file: "dist/usage.js",
    old: `const lastAssistant = useAuiState((s) => s.thread.messages.findLast((m) => m.role === "assistant"));`,
    new: `const lastAssistant = useAuiState((s) => {
        const msgs = s.thread.messages || [];
        return (Array.isArray(msgs) && typeof msgs.findLast === "function")
            ? msgs.findLast((m) => m.role === "assistant")
            : [...msgs].reverse().find((m) => m.role === "assistant");
    });`,
    done: "const msgs = s.thread.messages || []",
  },
];

function patchFile(pkgDir) {
  let patchedAny = false;
  for (const { file, old, new: newCode, done } of PATCHES) {
    const target = path.join(pkgDir, file);
    if (!fs.existsSync(target)) continue;
    let content = fs.readFileSync(target, "utf8");
    if (content.includes(done)) continue; // 已修补
    if (!content.includes(old.split("\n")[0].trim())) continue;
    content = content.replace(old, newCode);
    fs.writeFileSync(target, content);
    patchedAny = true;
    console.log("[patch-assistant-ui-findlast] 已修补:", path.relative(process.cwd(), target));
  }
  return patchedAny;
}

const nodeModules = path.join(process.cwd(), "node_modules");
const candidates = [path.join(nodeModules, "@assistant-ui/react-ai-sdk")];
const pnpmDir = path.join(nodeModules, ".pnpm");
if (fs.existsSync(pnpmDir)) {
  const dirs = fs.readdirSync(pnpmDir, { withFileTypes: true });
  for (const d of dirs) {
    if (d.isDirectory() && d.name.startsWith("@assistant-ui+react-ai-sdk@")) {
      const pkg = path.join(pnpmDir, d.name, "node_modules/@assistant-ui/react-ai-sdk");
      if (fs.existsSync(pkg)) candidates.push(pkg);
    }
  }
}

let patched = false;
for (const pkg of candidates) {
  if (patchFile(pkg)) patched = true;
}
if (!patched) {
  console.warn("[patch-assistant-ui-findlast] 未找到需修补的文件");
}
