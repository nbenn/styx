FROM debian:bookworm-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    bash \
    curl \
    ca-certificates \
    git \
    python3 \
    python3-coverage \
    socat \
    openssh-client \
    procps \
    && rm -rf /var/lib/apt/lists/*

# Install bats (Bash Automated Testing System)
RUN git clone --depth 1 https://github.com/bats-core/bats-core.git /tmp/bats-core \
    && /tmp/bats-core/install.sh /usr/local \
    && rm -rf /tmp/bats-core

# Install bats helper libraries
RUN git clone --depth 1 https://github.com/bats-core/bats-support.git /usr/local/lib/bats-support \
    && git clone --depth 1 https://github.com/bats-core/bats-assert.git /usr/local/lib/bats-assert

# Install GitHub CLI
RUN curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
      | dd of=/usr/share/keyrings/githubcli-archive-keyring.gpg \
    && chmod go+r /usr/share/keyrings/githubcli-archive-keyring.gpg \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
      > /etc/apt/sources.list.d/github-cli.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends gh \
    && rm -rf /var/lib/apt/lists/*

# Install Node.js
RUN curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/*

# Install Claude Code
ARG USERNAME=dev
ARG USER_UID=1000
ARG USER_GID=$USER_UID
RUN groupadd --gid $USER_GID $USERNAME \
    && useradd --uid $USER_UID --gid $USER_GID -m -s /bin/bash $USERNAME

USER $USERNAME
RUN curl -fsSL https://claude.ai/install.sh | bash
ENV PATH="/home/dev/.local/bin:${PATH}"

ENV SHELL=/bin/bash
SHELL ["/bin/bash", "-c"]

RUN cat >> /home/dev/.bashrc <<'EOF'

if [ -f /workspace/.devcontainer/.bashrc ]; then
  . /workspace/.devcontainer/.bashrc
fi
EOF
