#!/bin/sh
# 云托管入口:把密钥从环境变量(Space Secrets)注入为本地文件,其余与本地版一致。
# 需要在平台设置的 Secrets: QUARK_COOKIE(夸克 cookie 全文)、ADMIN_KEY(管理员口令)
# 可选: QUARK_TARGET_FID、RETENTION_HOURS
set -e
export HOME=/app
WS="$HOME/workspace"
APP="$WS/pan-search-app"

if [ -n "$QUARK_COOKIE" ]; then
  printf '%s' "$QUARK_COOKIE" > "$WS/quark-share-watch/cookie.txt"
  chmod 600 "$WS/quark-share-watch/cookie.txt"
fi
mkdir -p "$APP/data"
if [ -n "$ADMIN_KEY" ]; then
  printf '%s' "$ADMIN_KEY" > "$APP/data/admin_key.txt"
  chmod 600 "$APP/data/admin_key.txt"
fi
cat > "$APP/app_config.json" <<EOF
{
  "quark_target_fid": "${QUARK_TARGET_FID:-7851ec55784f4f9c906fb9ddf70ea22c}",
  "quark_target_name": "自动转存",
  "retention_hours": ${RETENTION_HOURS:-48},
  "guangya_target_fid": ""
}
EOF

cd "$APP"
# 到期清理:每 6 小时跑一次(与本地 systemd timer 等价)
(while true; do sleep 21600; python3 cleanup.py || true; done) &
export HOST=0.0.0.0
export PORT="${PORT:-7860}"
exec python3 app.py
