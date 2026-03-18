/**
 * PM2 进程配置：托管 Next.js（端口 3000）+ Python Bridge（端口 8001）
 *
 * 用法：
 *   pnpm build          # 先构建 Next.js
 *   pm2 start ecosystem.config.cjs
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
      script: 'node_modules/next/dist/bin/next',
      args: 'start',
      interpreter: 'node',
      autorestart: true,
      max_restarts: 10,
      min_uptime: '10s',
      env: {
        NODE_ENV: 'production',
        PORT: 3000,
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
