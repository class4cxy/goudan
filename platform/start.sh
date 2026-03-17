#!/bin/sh
# PM2 启动入口，与 package.json 的 platform 脚本一致（不含 --reload）
cd "$(dirname "$0")"
exec python3 -m uvicorn main:app --host 0.0.0.0 --port 8001
