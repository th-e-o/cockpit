# Image du runner cockpit : python, uv, git, le CLI Claude et le code du cockpit, figé à la construction.
# Le worker ne se met pas à jour seul : une nouvelle version = une nouvelle image, référencée dans deploy/k8s.
FROM python:3.13-slim-bookworm

RUN apt-get update \
 && apt-get install -y --no-install-recommends git curl ca-certificates openssh-client procps \
 && curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \
 && apt-get install -y --no-install-recommends nodejs \
 && npm install -g @anthropic-ai/claude-code \
 && pip install --no-cache-dir uv \
 && rm -rf /var/lib/apt/lists/*

RUN useradd -m -u 1000 -g users -s /bin/bash onyxia \
 && mkdir -p /opt/cockpit && chown onyxia:users /opt/cockpit
USER onyxia
ENV HOME=/home/onyxia PATH=/home/onyxia/.local/bin:$PATH

# Dépendances d'abord (couche réutilisée tant que requirements.txt ne change pas), puis le code
WORKDIR /opt/cockpit
COPY --chown=onyxia:users requirements.txt ./
RUN uv venv -q .venv && uv pip install -q --python .venv/bin/python -r requirements.txt
COPY --chown=onyxia:users . .

ARG COCKPIT_VERSION=dev
ENV COCKPIT_VERSION=$COCKPIT_VERSION
LABEL org.opencontainers.image.source=https://github.com/th-e-o/cockpit org.opencontainers.image.revision=$COCKPIT_VERSION

WORKDIR /home/onyxia/work
COPY --chmod=755 deploy/entrypoint.sh /usr/local/bin/cockpit-entrypoint
ENTRYPOINT ["cockpit-entrypoint"]
