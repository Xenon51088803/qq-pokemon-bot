#!/usr/bin/env bash
# Prepare a dedicated Debian/Ubuntu service. Starting it is a separate step.
set -Eeuo pipefail
umask 022

APP_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"
SERVICE=qq-pokemon-bot.service
if [[ "$EUID" -ne 0 ]]; then
    echo '请用 root 或 sudo bash scripts/install_server.sh 安装。' >&2
    exit 1
fi
if [[ "$APP_DIR" != /YCX/qq-pokemon-bot ]]; then
    echo '请先将项目放到 /YCX/qq-pokemon-bot，再运行此脚本。' >&2
    exit 1
fi
if [[ ! -d /run/systemd/system ]] || ! command -v apt-get >/dev/null; then
    echo '此入口需要使用 systemd 的 Debian / Ubuntu 服务器。' >&2
    exit 1
fi
if systemctl is-active --quiet "$SERVICE"; then
    echo '服务正在运行，请先执行 systemctl stop qq-pokemon-bot，再重新安装。' >&2
    exit 1
fi
if [[ -L "$APP_DIR/runtime" || -L "$APP_DIR/runtime/qq" ]]; then
    echo 'runtime 必须是项目内的真实目录，不能是符号链接。' >&2
    exit 1
fi
if getent passwd pokemonbot >/dev/null; then
    if [[ "$(getent passwd pokemonbot | cut -d: -f6)" != "$APP_DIR" ]] ||
       [[ "$(id -gn pokemonbot)" != pokemonbot ]]; then
        echo '已有同名 pokemonbot 账号用途不同，请先检查，安装未继续。' >&2
        exit 1
    fi
fi

export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y ca-certificates git python3 python3-venv fonts-noto-cjk
cd -- "$APP_DIR"
chmod 0755 "$APP_DIR"

if [[ -e .venv && ! -x .venv/bin/python ]]; then
    echo '.venv 不是 Linux 虚拟环境。不要从 Windows 上传 .venv，请移走后重试。' >&2
    exit 1
fi
if [[ ! -x .venv/bin/python ]]; then
    if python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3,12) else 1)'; then
        PYTHON=python3
    else
        # Debian 12 provides 3.11. Keep the OS interpreter unchanged and install
        # the application runtime beneath its own directory (also readable by the service).
        python3 -m venv .bootstrap
        .bootstrap/bin/python -m pip install --disable-pip-version-check uv
        export UV_PYTHON_INSTALL_DIR="$APP_DIR/.python"
        export UV_PYTHON_BIN_DIR="$APP_DIR/.bootstrap/bin"
        export UV_CACHE_DIR="$APP_DIR/.cache/uv"
        .bootstrap/bin/uv python install 3.12
        PYTHON="$(.bootstrap/bin/uv python find --managed-python 3.12)"
    fi
    "$PYTHON" -m venv .venv
fi
.venv/bin/python -c 'import sys; assert sys.version_info >= (3,12), "需要 Python 3.12+，请先检查现有 .venv"'
.venv/bin/python -m pip install --disable-pip-version-check -r requirements.txt

if ! getent passwd pokemonbot >/dev/null; then
    useradd --system --user-group --no-create-home --home-dir "$APP_DIR" --shell /usr/sbin/nologin pokemonbot
fi
install -d -o pokemonbot -g pokemonbot -m 0700 "$APP_DIR/runtime" "$APP_DIR/runtime/qq"
chown -R pokemonbot:pokemonbot "$APP_DIR/runtime"
chmod -R go-rwx "$APP_DIR/runtime"
for config in qq.local.json config.local.json; do
    if [[ -f "$config" ]]; then
        chown root:pokemonbot "$config"
        chmod 0640 "$config"
    fi
done
# Validate the service user's interpreter, imports and font without loading QQ
# credentials, opening a collection database or connecting to the real gateway.
runuser -u pokemonbot -- .venv/bin/python -c 'import aiohttp; from pokebot.config import Config; from pokebot.render import find_font; print("中文字体：", find_font(Config.load().font_path))'
systemd-analyze verify "$APP_DIR/deploy/$SERVICE"
if [[ -f "/etc/systemd/system/$SERVICE" ]] && ! cmp -s "$APP_DIR/deploy/$SERVICE" "/etc/systemd/system/$SERVICE"; then
    cp -p -- "/etc/systemd/system/$SERVICE" "$APP_DIR/runtime/service-before-install-$(date +%Y%m%d-%H%M%S).bak"
fi
install -o root -g root -m 0644 "$APP_DIR/deploy/$SERVICE" "/etc/systemd/system/$SERVICE"
systemctl daemon-reload
echo
echo '环境与后台服务已安装；尚未连接 QQ。'
echo '用 WinSCP 上传 qq.local.json 和停机后的收藏数据，上传后再次运行此脚本修正权限。'
echo '确认电脑上的机器人已关闭，再执行：systemctl enable --now qq-pokemon-bot'
echo '查看运行状态：systemctl status qq-pokemon-bot --no-pager'
echo '查看最近日志：journalctl -u qq-pokemon-bot -n 50 --no-pager'
