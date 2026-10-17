#!/usr/bin/env bash
# 一次性部署脚本（在服务器上以 deploy 用户运行，需要 sudo）。
# 幂等：重复执行只更新代码与依赖，不动 data/ 和 /etc/slowtrans/env。
set -euo pipefail

APP=/srv/slowtrans
REPO=${1:-$(cd "$(dirname "$0")/.." && pwd)}
PIP_MIRROR=${PIP_MIRROR:-https://mirrors.aliyun.com/pypi/simple/}

echo "==> 目录"
sudo mkdir -p "$APP" "$APP/data" /etc/slowtrans
sudo chown -R "$(id -un):$(id -gn)" "$APP"

echo "==> 代码"
rsync -a --delete --exclude '.venv' --exclude 'data' --exclude '__pycache__' \
  "$REPO/src" "$APP/"
cp "$REPO/pyproject.toml" "$APP/"

echo "==> venv"
if [ ! -x "$APP/venv/bin/python" ]; then
  python3 -m venv "$APP/venv"
fi
"$APP/venv/bin/pip" install -q -i "$PIP_MIRROR" --upgrade pip
"$APP/venv/bin/pip" install -q -i "$PIP_MIRROR" -e "$APP[api]"

echo "==> 环境变量"
if [ ! -f /etc/slowtrans/env ]; then
  sudo install -m 600 "$REPO/deploy/env.example" /etc/slowtrans/env
  echo "!! 请先编辑 /etc/slowtrans/env 填入 API key 与 token，再启动服务"
fi

echo "==> systemd"
sudo install -m 644 "$REPO/deploy/slowtrans.service" /etc/systemd/system/slowtrans.service
sudo systemctl daemon-reload
sudo systemctl enable slowtrans

cat <<'EOF'

完成。检查实际生效值（不要相信文件内容）：
  sudo systemctl start slowtrans
  systemctl status slowtrans --no-pager
  systemctl show slowtrans -p MemoryMax -p CPUQuotaPerSecUSec
  curl -s http://127.0.0.1:8790/api/v1/health
  systemctl show slowtrans -p MemoryCurrent   # 运行几天后看峰值

本地面板/插件走 SSH 隧道，不开公网：
  ssh -N -L 8790:127.0.0.1:8790 aliyun
  浏览器打开 http://127.0.0.1:8790/
EOF
