# OwnAI backend (API + worker share this image)
FROM python:3.12-slim AS base
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
# Optional corporate proxy / custom CA: docker build --secret id=ca,src=ca.crt --build-arg HTTPS_PROXY=...
ARG HTTPS_PROXY
# INSTALL_GIT=0 builds without git (Git features then report "git is not installed").
ARG INSTALL_GIT=1
RUN --mount=type=secret,id=ca,required=false \
    if [ "$INSTALL_GIT" = "1" ] && [ -f /run/secrets/ca ]; then \
      sed -i 's|http://deb.debian.org|https://deb.debian.org|g' /etc/apt/sources.list.d/debian.sources; \
      printf 'Acquire::https::CAInfo "/run/secrets/ca";\n' > /etc/apt/apt.conf.d/99ca; \
      [ -n "$HTTPS_PROXY" ] && printf 'Acquire::https::Proxy "%s";\n' "$HTTPS_PROXY" >> /etc/apt/apt.conf.d/99ca; \
    fi; \
    if [ "$INSTALL_GIT" = "1" ]; then \
      apt-get update && apt-get install -y --no-install-recommends git ca-certificates \
      && rm -rf /var/lib/apt/lists/* /etc/apt/apt.conf.d/99ca; \
    fi \
    && useradd --uid 10001 --create-home --shell /usr/sbin/nologin ownai
WORKDIR /srv/backend
COPY backend/pyproject.toml ./
RUN --mount=type=secret,id=ca,required=false \
    if [ -f /run/secrets/ca ]; then export PIP_CERT=/run/secrets/ca; fi; \
    mkdir -p app && touch app/__init__.py && pip install . && rm -rf app
COPY backend/app ./app
COPY backend/migrations ./migrations
COPY backend/alembic.ini ./
COPY config /srv/config
COPY evaluation /srv/evaluation
RUN pip install --no-deps . && mkdir -p /srv/data/projects && chown -R ownai /srv/data
USER ownai
ENV OWNAI_PROJECTS_ROOT=/srv/data/projects
EXPOSE 8000
CMD ["uvicorn", "app.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers"]
