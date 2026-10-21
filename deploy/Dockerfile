# Image du runner cockpit : python, uv, git et le CLI Claude. Le code du cockpit n'est pas dans l'image :
# entrypoint.sh le clone dans le volume (et le worker se met à jour seul, COCKPIT_AUTO_UPDATE=1).
FROM python:3.13-slim-bookworm

RUN apt-get update \
 && apt-get install -y --no-install-recommends git curl ca-certificates openssh-client procps \
 && curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \
 && apt-get install -y --no-install-recommends nodejs \
 && npm install -g @anthropic-ai/claude-code \
 && pip install --no-cache-dir uv \
 && rm -rf /var/lib/apt/lists/*

RUN useradd -m -u 1000 -g users -s /bin/bash onyxia
USER onyxia
ENV HOME=/home/onyxia PATH=/home/onyxia/.local/bin:$PATH
WORKDIR /home/onyxia/work

COPY --chmod=755 deploy/entrypoint.sh /usr/local/bin/cockpit-entrypoint
ENTRYPOINT ["cockpit-entrypoint"]
