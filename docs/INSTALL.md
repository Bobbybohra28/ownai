# Installation

OwnAI needs: **PostgreSQL 16**, **Redis 7**, **Qdrant 1.12+**, **Docker** (for the code sandbox) and at least one
model server (**vLLM** recommended with a GPU, **Ollama** for CPU/laptops, or any OpenAI-compatible server).

Two ways to run it:

* **A. Docker Compose** — everything in containers (recommended for servers and Windows).
* **B. Local development** — backend and frontend run from source; databases in Docker or native.

---

## A. Docker Compose (Linux, macOS, Windows with Docker Desktop)

### Linux / macOS

```bash
git clone <your-repo-url> ownai && cd ownai
./scripts/setup.sh              # creates .env (fresh secrets) and config/models.yaml (vLLM template)
# ./scripts/setup.sh ollama     # ... or the Ollama template

# edit config/models.yaml or the *_MODEL_* variables in .env (see "Models" below)

docker compose -f deploy/docker-compose.yml --env-file .env --profile build build   # includes the sandbox image
docker compose -f deploy/docker-compose.yml --env-file .env up -d
open http://localhost:8080      # register — the first account becomes the installation administrator
```

### Windows (PowerShell + Docker Desktop)

```powershell
git clone <your-repo-url> ownai; cd ownai
powershell -ExecutionPolicy Bypass -File scripts\setup.ps1          # or: -Ollama
docker compose -f deploy/docker-compose.yml --env-file .env --profile build build
docker compose -f deploy/docker-compose.yml --env-file .env up -d
Start-Process http://localhost:8080
```

> Keep the project **outside OneDrive-synced folders** (e.g. `C:\dev\ownai`): OneDrive locks files under
> `node_modules`, virtual environments and Docker volumes.

### With a GPU (vLLM)

Requires the NVIDIA driver and the NVIDIA Container Toolkit.

```bash
# .env
CODING_MODEL_NAME=Qwen/Qwen2.5-Coder-7B-Instruct
CODING_MODEL_URL=http://vllm-coding:8000/v1
EMBEDDING_MODEL_NAME=nomic-ai/nomic-embed-text-v1.5
EMBEDDING_MODEL_URL=http://vllm-embedding:8000/v1
EMBEDDING_DIMENSIONS=768
# optional second (fast) model:  FAST_MODEL_NAME=..., FAST_MODEL_URL=http://vllm-fast:8000/v1

docker compose -f deploy/docker-compose.yml -f deploy/docker-compose.gpu.yml --env-file .env up -d
docker compose -f deploy/docker-compose.yml -f deploy/docker-compose.gpu.yml --env-file .env --profile fast up -d   # + fast model
```

Size `--gpu-memory-utilization` (`CODING_GPU_UTIL`, `EMBEDDING_GPU_UTIL`, `FAST_GPU_UTIL`) so all servers fit
in VRAM. For fully offline operation download the weights once, then set `HF_HUB_OFFLINE=1`.

### With Ollama

```bash
ollama pull qwen2.5-coder:7b && ollama pull qwen2.5:3b && ollama pull nomic-embed-text
cp config/models.ollama.example.yaml config/models.yaml
# Ollama on the host: containers reach it via http://host.docker.internal:11434 — Ollama must listen on
# 0.0.0.0 (OLLAMA_HOST=0.0.0.0:11434). Or run Ollama in compose:
docker compose -f deploy/docker-compose.yml -f deploy/docker-compose.ollama.yml --env-file .env --profile ollama up -d
```

Use `provider: ollama` (native API). Ollama's OpenAI-compatible `/v1` endpoint ignores the configured context
window (it uses Ollama's default), which truncates or rejects long agent prompts.

---

## B. Local development

### Prerequisites

* Python **3.12+**, Node.js **20+**, Docker, Git
* PostgreSQL, Redis and Qdrant — easiest via Docker:

```bash
docker run -d --name ownai-postgres -e POSTGRES_USER=ownai -e POSTGRES_PASSWORD=ownai -e POSTGRES_DB=ownai -p 5432:5432 postgres:16-alpine
docker run -d --name ownai-redis -p 6379:6379 redis:7-alpine
docker run -d --name ownai-qdrant -p 6333:6333 -v ownai-qdrant:/qdrant/storage qdrant/qdrant:v1.19.1
```

### Backend (Linux/macOS)

```bash
./scripts/setup.sh
cd backend
python3.12 -m venv .venv && . .venv/bin/activate      # or: uv venv --python 3.12 && . .venv/bin/activate
pip install -e ".[dev]"
set -a; . ../.env; set +a                             # load settings into the shell
alembic upgrade head
uvicorn app.main:create_app --factory --reload --port 8000     # API
python -m app.worker                                            # worker (second terminal)
```

### Backend (Windows PowerShell)

```powershell
powershell -ExecutionPolicy Bypass -File scripts\setup.ps1
cd backend
py -3.12 -m venv .venv; .\.venv\Scripts\Activate.ps1
pip install -e ".[dev]"
# settings are read from ..\.env automatically (pydantic-settings); only OWNAI_* variables are needed
alembic upgrade head
uvicorn app.main:create_app --factory --reload --port 8000
python -m app.worker        # in a second PowerShell window
```

Single-process alternative (no Redis, no worker): set `OWNAI_EXECUTION_MODE=inline`.

### Sandbox runner

```bash
docker build -t ownai/sandbox-python:3.12 sandbox_runner/images/python
cd sandbox_runner && pip install -r requirements.txt
SANDBOX_TOKEN=<same as OWNAI_SANDBOX_TOKEN> uvicorn app.main:app --port 8090
```

PowerShell: `$env:SANDBOX_TOKEN="<token>"; uvicorn app.main:app --port 8090` (Docker Desktop must be running).

### Frontend

```bash
cd frontend
npm install
npm run dev            # http://localhost:5173 (proxies /api to http://localhost:8000)
```

---

## Models

Model names, endpoints and keys live only in configuration:

* `config/models.yaml` — full registry (roles `fast`, `coding`, `reasoning`, `vision`, `embedding`, `reranker`),
  e.g. copied from `config/models.example.yaml` (vLLM) or `config/models.ollama.example.yaml`.
* or the quick single-model variables `OWNAI_MODEL_URL`, `OWNAI_MODEL_NAME`, `OWNAI_MODEL_API_KEY`
  (+ `OWNAI_EMBEDDING_URL`, `OWNAI_EMBEDDING_MODEL`, `OWNAI_EMBEDDING_DIMENSIONS`).

After starting, open **Models** → *Re-check all*. A model is shown ONLINE only if the endpoint answered, the model
name is served and a real completion returned content. Then run **Admin → AI pipeline diagnostics**.

## Verify the installation

```bash
curl http://localhost:8080/health          # compose (or :8000 for local dev)
cd backend && pytest tests/unit            # unit tests (no services needed)
```

See `docs/TESTING.md` for integration tests and `docs/TROUBLESHOOTING.md` if something fails.
