#!/usr/bin/env bash
# setup-https.sh — 为局域网 HTTPS 生成本地受信任证书
# =======================================================
# 依赖：mkcert（https://github.com/FiloSottile/mkcert）
# 树莓派安装：sudo apt install libnss3-tools && curl -Lo /usr/local/bin/mkcert \
#   https://github.com/FiloSottile/mkcert/releases/latest/download/mkcert-v*-linux-arm64 \
#   && chmod +x /usr/local/bin/mkcert
#
# 其他设备（手机/平板）要信任此证书，需将 mkcert -CAROOT 目录下的
# rootCA.pem 安装为受信任 CA：
#   iOS:  AirDrop rootCA.pem → 设置 → 通用 → VPN与设备管理 → 安装 → 信任
#   Android: 设置 → 安全 → 安装证书 → CA 证书

set -euo pipefail

CERT_DIR="$(cd "$(dirname "$0")/.." && pwd)/certs"
mkdir -p "$CERT_DIR"

# 自动获取本机所有局域网 IP 和 hostname
HOSTNAME=$(hostname)
HOSTNAME_LOCAL="${HOSTNAME}.local"
LAN_IPS=$(hostname -I 2>/dev/null | tr ' ' '\n' | grep -v '^$' | head -5 | tr '\n' ' ')

echo "===================================================="
echo "  生成本地 HTTPS 证书"
echo "  覆盖地址：localhost 127.0.0.1 ${HOSTNAME} ${HOSTNAME_LOCAL} ${LAN_IPS}"
echo "===================================================="

# 安装本地 CA 到系统信任库（当前机器）
mkcert -install

# 生成证书（localhost + 本机 hostname + 所有 LAN IP）
cd "$CERT_DIR"
mkcert \
  localhost \
  127.0.0.1 \
  "${HOSTNAME}" \
  "${HOSTNAME_LOCAL}" \
  ${LAN_IPS}

# 统一命名为 cert.pem / key.pem
CERT_FILE=$(ls "$CERT_DIR"/*.pem 2>/dev/null | grep -v key | head -1)
KEY_FILE=$(ls "$CERT_DIR"/*-key.pem 2>/dev/null | head -1)

if [ -n "$CERT_FILE" ] && [ -n "$KEY_FILE" ]; then
  mv "$CERT_FILE" "$CERT_DIR/cert.pem"
  mv "$KEY_FILE"  "$CERT_DIR/key.pem"
  echo ""
  echo "✅ 证书已生成："
  echo "   $CERT_DIR/cert.pem"
  echo "   $CERT_DIR/key.pem"
  echo ""
  echo "📱 让其他设备（手机/平板）信任此 CA："
  echo "   CA 文件：$(mkcert -CAROOT)/rootCA.pem"
  echo "   iOS:     AirDrop 发送 → 设置 → 安装证书 → 通用 → 关于本机 → 证书信任设置 → 启用"
  echo "   Android: 设置 → 安全 → 安装证书 → CA 证书"
  echo ""
  echo "🚀 启动 HTTPS 开发服务器："
  echo "   npm run dev:https"
  echo ""
  echo "🚀 启动 HTTPS 生产服务器："
  echo "   npm run build && npm run start:https"
else
  echo "❌ 证书文件生成失败，请检查 mkcert 安装"
  exit 1
fi
