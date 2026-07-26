#!/bin/bash
set -e

APP_DIR="/home/bot/feishu-bot"
ENV_FILE="$APP_DIR/.env"
ENV_EXAMPLE="$APP_DIR/.env.example"

generate_env() {
    if [ -f "$ENV_FILE" ]; then
        return
    fi

    if [ -n "$FEISHU_APP_ID" ] || [ -n "$FEISHU_APP_SECRET" ] || [ -n "$MAINTAINER_OPEN_ID" ]; then
        cat > "$ENV_FILE" <<EOF
FEISHU_APP_ID=${FEISHU_APP_ID:-cli_xxx}
FEISHU_APP_SECRET=${FEISHU_APP_SECRET:-your_app_secret}
MAINTAINER_OPEN_ID=${MAINTAINER_OPEN_ID:-ou_xxx}
EOF
        if [ -n "${FEISHU_BOT_OPEN_ID:-}" ]; then
            echo "FEISHU_BOT_OPEN_ID=$FEISHU_BOT_OPEN_ID" >> "$ENV_FILE"
        fi
        echo "[entrypoint] Generated .env from environment variables"
    elif [ -f "$ENV_EXAMPLE" ]; then
        cp "$ENV_EXAMPLE" "$ENV_FILE"
        echo "[entrypoint] Copied .env.example -> .env (edit or pass -e vars to configure)"
    fi
}

setup_venv() {
    if [ ! -d "$APP_DIR/.venv" ]; then
        cd "$APP_DIR"
        uv venv .venv
        uv pip install -r feishu-server/requirements.txt \
            -i https://mirrors.tuna.tsinghua.edu.cn/pypi/web/simple
    fi
}

generate_env
setup_venv
mkdir -p "$APP_DIR/feishu-server/data" "$APP_DIR/feishu-server/logs"
cd "$APP_DIR/feishu-server"
exec "$APP_DIR/.venv/bin/python" main.py
