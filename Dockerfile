FROM ubuntu:26.04

ENV DEBIAN_FRONTEND=noninteractive \
    LANG=C.UTF-8 \
    LC_ALL=C.UTF-8

# --- System packages & tools (root) ---
RUN for f in /etc/apt/sources.list /etc/apt/sources.list.d/ubuntu.sources; do \
      [ -f "$f" ] && sed -i 's|http://archive.ubuntu.com/ubuntu|http://mirrors.tuna.tsinghua.edu.cn/ubuntu|g; s|http://security.ubuntu.com/ubuntu|http://mirrors.tuna.tsinghua.edu.cn/ubuntu|g' "$f"; \
    done \
    && apt-get update && apt-get install -y --no-install-recommends \
    git \
    curl \
    wget \
    ripgrep \
    fd-find \
    ca-certificates \
    python3 \
    python3-pip \
    python3-venv \
    python-is-python3 \
    vim \
    file \
    busybox-static \
    sudo \
    unzip \
    zip \
    xz-utils \
    jq \
    openssh-client \
    locales \
    && curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && curl -LsSf https://astral.sh/uv/install.sh | sh \
    && mv /root/.local/bin/uv /root/.local/bin/uvx /usr/local/bin/ \
    && LAZYGIT_VERSION=$(curl -s "https://api.github.com/repos/jesseduffield/lazygit/releases/latest" | jq -r .tag_name | sed 's/^v//') \
    && curl -sLo /tmp/lazygit.tar.gz "https://github.com/jesseduffield/lazygit/releases/download/v${LAZYGIT_VERSION}/lazygit_${LAZYGIT_VERSION}_Linux_x86_64.tar.gz" \
    && tar -xzf /tmp/lazygit.tar.gz -C /usr/local/bin lazygit \
    && YAZI_VERSION=$(curl -s "https://api.github.com/repos/sxyazi/yazi/releases/latest" | jq -r .tag_name | sed 's/^v//') \
    && curl -sLo /tmp/yazi.zip "https://github.com/sxyazi/yazi/releases/download/v${YAZI_VERSION}/yazi-x86_64-unknown-linux-gnu.zip" \
    && unzip -j /tmp/yazi.zip -d /usr/local/bin/ \
    && rm -rf /var/lib/apt/lists/* /tmp/lazygit.tar.gz /tmp/yazi.zip \
    && useradd -m -s /bin/bash -G sudo bot \
    && echo 'bot ALL=(ALL) NOPASSWD:ALL' > /etc/sudoers.d/bot \
    && chmod 0440 /etc/sudoers.d/bot

# --- AI CLI tools (bot user) ---
USER bot
ENV PATH="/home/bot/.local/bin:${PATH}"
RUN npm config set registry https://registry.npmmirror.com \
    && sudo npm install -g @openai/codex \
    && sudo npm install -g @anthropic-ai/claude-code \
    && sudo npm install -g @github/copilot \
    && curl -fsSL https://qoder.com/install | bash \
    && sudo npm install -g @tencent-ai/codebuddy-code \
    && sudo npm install -g --ignore-scripts @earendil-works/pi-coding-agent \
    && sudo npm install -g @larksuite/cli \
    && sudo npm cache clean --force \
    && pi install npm:pi-web-access \
    && pi install npm:pi-subagents \
    && pi install npm:@ff-labs/pi-fff

# --- Clone repo + Python venv ---
RUN git clone https://github.com/wddjwk/feishu-bot /home/bot/feishu-bot \
    && mkdir -p \
    /home/bot/feishu-bot/feishu-server/data \
    /home/bot/feishu-bot/feishu-server/logs \
    /home/bot/feishu-bot/agent-workspace/workfolder \
    /home/bot/feishu-bot/agent-workspace/scheduler \
    /home/bot/feishu-bot/agent-workspace/memory \
    && cd /home/bot/feishu-bot \
    && uv venv .venv \
    && uv pip install -r feishu-server/requirements.txt \
    -i https://mirrors.tuna.tsinghua.edu.cn/pypi/web/simple

WORKDIR /home/bot/feishu-bot
CMD ["/bin/bash"]
