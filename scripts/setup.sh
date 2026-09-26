#!/usr/bin/env bash
# OwnAI first-time setup (Linux/macOS). Creates .env with fresh secrets and config/models.yaml.
set -euo pipefail
cd "$(dirname "$0")/.."

if [ ! -f .env ]; then
  cp .env.example .env
  secret=$(python3 -c "import secrets; print(secrets.token_urlsafe(48))")
  sandbox=$(python3 -c "import secrets; print(secrets.token_urlsafe(32))")
  fernet=$(python3 -c "import base64, os; print(base64.urlsafe_b64encode(os.urandom(32)).decode())")
  sed -i.bak "s|^OWNAI_SECRET_KEY=.*|OWNAI_SECRET_KEY=${secret}|; s|^OWNAI_ENCRYPTION_KEY=.*|OWNAI_ENCRYPTION_KEY=${fernet}|; s|^OWNAI_SANDBOX_TOKEN=.*|OWNAI_SANDBOX_TOKEN=${sandbox}|; s|^SANDBOX_TOKEN=.*|SANDBOX_TOKEN=${sandbox}|" .env
  rm -f .env.bak
  echo "Created .env with new secrets."
else
  echo ".env already exists — left unchanged."
fi

if [ ! -f config/models.yaml ]; then
  if [ "${1:-}" = "ollama" ]; then cp config/models.ollama.example.yaml config/models.yaml; else cp config/models.example.yaml config/models.yaml; fi
  echo "Created config/models.yaml — edit it (or the referenced variables in .env) to point at your models."
fi

echo "Next steps:"
echo "  Docker:  docker compose -f deploy/docker-compose.yml --env-file .env --profile build build"
echo "           docker compose -f deploy/docker-compose.yml --env-file .env up -d   ->  http://localhost:8080"
echo "  Local:   see docs/INSTALL.md (backend: uv/pip, frontend: npm)"
