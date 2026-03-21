/**
 * PM2 进程配置：托管 Next.js 生产 HTTPS + Python Bridge（端口 8001）
 *
 * 用法：
 *   pnpm setup:https     # 首次：生成 certs/*.pem（与 dev:https 共用）
 *   pnpm build
 *   pm2 start ecosystem.config.cjs
 *
 * 访问：https://<设备IP>:3443（HTTPS_PORT）；HTTP :3000 会 301 跳到 HTTPS。
 *
 * 开机自启：pm2 save && pm2 startup
 *
 */

const path = require('path');
const projectRoot = path.resolve(__dirname);

module.exports = {
  apps: [
    {
      name: 'home-agent',
      cwd: projectRoot,
      script: path.join(projectRoot, 'server-https.mjs'),
      interpreter: 'node',
      autorestart: true,
      max_restarts: 10,
      min_uptime: '10s',
      env: {
        NODE_ENV: 'production',
        /** HTTP：仅做跳转到 HTTPS，与 server-https.mjs 一致 */
        PORT: 3000,
        /** HTTPS：实际站点端口（防火墙需放行 TCP 3000 + 本端口） */
        HTTPS_PORT: 3443,
      },
      error_file: path.join(projectRoot, 'logs', 'home-agent-err.log'),
      out_file: path.join(projectRoot, 'logs', 'home-agent-out.log'),
      merge_logs: true,
      time: true,
    },
    {
      name: 'platform',
      cwd: path.join(projectRoot, 'platform'),
      script: path.join(projectRoot, 'platform', 'start.sh'),
      interpreter: 'sh',
      autorestart: true,
      max_restarts: 10,
      min_uptime: '10s',
      env: { PYTHONUNBUFFERED: '1' },
      error_file: path.join(projectRoot, 'logs', 'platform-err.log'),
      out_file: path.join(projectRoot, 'logs', 'platform-out.log'),
      merge_logs: true,
      time: true,
    },
  ],
};
