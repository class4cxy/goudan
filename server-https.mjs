/**
 * server-https.mjs — 生产环境 HTTPS 服务器
 * ==========================================
 * 用 Node.js https 模块包装 Next.js（与 `next build` 产物配合，等同带 TLS 的 `next start`）。
 *
 * 证书：pnpm setup:https → certs/key.pem、certs/cert.pem（与 dev:https 共用）。
 *
 * 启动：
 *   - 手动：pnpm build && pnpm start:https
 *   - PM2：pnpm build && pm2 start ecosystem.config.cjs（home-agent 即本脚本）
 *
 * 端口：
 *   - HTTPS_PORT（默认 3000）：站点入口，与原先 `next start` 端口一致，防火墙一般已放行。
 *   - 可选 HTTP_REDIRECT_PORT：若设置（如 3080），该端口仅做 HTTP→HTTPS 301（方便只习惯点 http 的用户）。
 */

import https from "https";
import { readFileSync } from "fs";
import { createServer } from "http";
import { parse } from "url";
import path from "path";
import { fileURLToPath } from "url";
import next from "next";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const CERT_DIR = path.join(__dirname, "certs");
const HTTPS_PORT = parseInt(process.env.HTTPS_PORT ?? "3000", 10);
const rawRedirect = process.env.HTTP_REDIRECT_PORT?.trim();
const HTTP_REDIRECT_PORT =
  rawRedirect && rawRedirect.length > 0 ? parseInt(rawRedirect, 10) : NaN;

const dev = false;
const app = next({ dev });
const handle = app.getRequestHandler();

await app.prepare();

if (!Number.isNaN(HTTP_REDIRECT_PORT) && HTTP_REDIRECT_PORT > 0) {
  createServer((req, res) => {
    const host = req.headers.host?.replace(/:\d+$/, "") ?? "localhost";
    res.writeHead(301, {
      Location: `https://${host}:${HTTPS_PORT}${req.url ?? "/"}`,
    });
    res.end();
  }).listen(HTTP_REDIRECT_PORT, "0.0.0.0", () => {
    console.log(
      `[HTTPS Server] HTTP :${HTTP_REDIRECT_PORT} → HTTPS :${HTTPS_PORT} 重定向已启动`,
    );
  });
}

let sslOptions;
try {
  sslOptions = {
    key: readFileSync(path.join(CERT_DIR, "key.pem")),
    cert: readFileSync(path.join(CERT_DIR, "cert.pem")),
  };
} catch {
  console.error("[HTTPS Server] ❌ 找不到证书文件，请先运行：pnpm setup:https");
  process.exit(1);
}

const httpsServer = https.createServer(sslOptions, (req, res) => {
  const parsedUrl = parse(req.url, true);
  handle(req, res, parsedUrl);
});

httpsServer.on("error", (err) => {
  console.error("[HTTPS Server] ❌ HTTPS 监听失败：", err.message);
  process.exit(1);
});

httpsServer.listen(HTTPS_PORT, "0.0.0.0", () => {
  console.log(
    `[HTTPS Server] ✅ HTTPS 已启动：https://0.0.0.0:${HTTPS_PORT}（请用 https:// 访问）`,
  );
});
