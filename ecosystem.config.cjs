/**
 * PM2 进程配置：托管 Python Bridge（端口 8001）
 *
 * 用法：pm2 start ecosystem.config.cjs
 * 开机自启：pm2 save && pm2 startup
 *
 */

const path = require('path');
const projectRoot = path.resolve(__dirname);

module.exports = {
  apps: [
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
