#!/usr/bin/env bash
# 一次性部署脚本（在服务器上运行，需要 sudo）。
# 幂等：重复执行只更新代码与依赖，不动 data/ 和 /etc/slowtrans/env。
set -euo pipefail

APP=/srv/slowtrans
REPO=${1:-$(cd "$(dirname "$0")/.." && pwd)}
PIP_MIRROR=${PIP_MIRROR:-https://mirrors.aliyun.com/pypi/simple/}

echo "==> 目录"
sudo mkdir -p "$APP" "$APP/data" "$APP/books" "$APP/outputs" /etc/slowtrans
sudo chown -R "$(id -un):$(id -gn)" "$APP"

echo "==> 代码"
# 统一用 tar：最小化 Debian 镜像没有 rsync，多一条分支就多一种出错方式。
# 源码目录和部署目录重合时（直接 git clone 到 /srv/slowtrans）跳过，避免自己 tar 自己。
REPO_ABS=$(cd "$REPO" && pwd)
APP_ABS=$(cd "$APP" 2>/dev/null && pwd || echo "")
if [ -n "$APP_ABS" ] && [ "$REPO_ABS" = "$APP_ABS" ]; then
  echo "    源码就在部署目录，跳过同步"
else
  tar -C "$REPO" -cf - \
    --exclude='.venv' --exclude='data' --exclude='__pycache__' \
    --exclude='.git' --exclude='dist' --exclude='*.egg-info' --exclude='.pytest_cache' \
    src pyproject.toml README.md README.en.md CHANGELOG.md CONTRIBUTING.md \
    LICENSE config.example.toml docs deploy clients \
    | tar -C "$APP" -xf -
fi

echo "==> venv"
if [ ! -x "$APP/venv/bin/python" ]; then
  python3 -m venv "$APP/venv"
fi
"$APP/venv/bin/pip" install -q -i "$PIP_MIRROR" --upgrade pip
# 必须 cd 进去再 -e ".[api]"。
# 写成 -e "$APP[api]" 会被 pip 当成一个叫 "/srv/slowtrans[api]" 的目录而失败。
( cd "$APP" && "$APP/venv/bin/pip" install -q -i "$PIP_MIRROR" -e ".[api]" )

echo "==> 环境变量"
if [ ! -f /etc/slowtrans/env ]; then
  sudo install -m 600 "$REPO/deploy/env.example" /etc/slowtrans/env
  echo "!! 编辑 /etc/slowtrans/env 填入 API key 与 SLOWTRANS_TOKEN，再启动服务"
  echo "!! token 生成：python3 -c 'import secrets;print(secrets.token_urlsafe(32))'"
fi
echo "!! 这个文件必须是 LF 换行。Windows 上用 write_text 生成会带 \\r，"
echo "!! systemd 读进去后 key 末尾有 \\r，所有 API 调用报 Invalid header value。"

echo "==> systemd"
sudo install -m 644 "$REPO/deploy/slowtrans.service" /etc/systemd/system/slowtrans.service
sudo systemctl daemon-reload
sudo systemctl enable slowtrans

cat <<'EOF'

完成。验证实际生效值（不要只看文件）：
  sudo systemctl start slowtrans
  systemctl status slowtrans --no-pager
  systemctl show slowtrans -p MemoryMax -p CPUQuotaPerSecUSec -p MemoryCurrent
  curl -s http://127.0.0.1:8790/api/v1/health

本地面板/插件走 SSH 隧道，不开公网：
  ssh -N -L 8790:127.0.0.1:8790 <你的 ssh 别名>
  浏览器打开 http://127.0.0.1:8790/ ，token 见 /etc/slowtrans/env
EOF
