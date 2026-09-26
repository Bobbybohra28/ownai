# Code execution sandbox

`sandbox_runner/` is a small FastAPI service. The backend sends it a workspace archive (project files minus VCS,
dependency folders and credential files, optionally with staged changes overlaid) and an argv list. The runner
streams the archive into a fresh container over stdin — no host paths are mounted — and returns exit code,
stdout/stderr (truncated), duration, timeout and OOM flags. The container is always removed.

## Container policy

`--network none`, `--read-only`, `/workspace` and `/tmp` on size-limited tmpfs, `--user 1000:1000`,
`--cap-drop ALL`, `--security-opt no-new-privileges`, `--pids-limit`, `--memory` = `--memory-swap`, `--cpus`,
wall-clock timeout (container killed), optional `--runtime runsc` (gVisor).

Allowed executables per profile (`python`: python/pytest/ruff/pip — `pip install` refused; `node`: node/npm —
installs refused; `go`, `rust`). Profiles/images are configurable with `SANDBOX_PROFILES_JSON`.

## Build the Python image

```bash
docker build -t ownai/sandbox-python:3.12 sandbox_runner/images/python
# behind a proxy with a custom CA:
docker build --secret id=ca,src=ca.crt --build-arg HTTPS_PROXY=$HTTPS_PROXY -t ownai/sandbox-python:3.12 sandbox_runner/images/python
```

The image contains pytest, pytest-asyncio and ruff. Projects that need more packages at test time need a custom
image (extend the Dockerfile) — the sandbox has no network, by design.

## Run

```bash
cd sandbox_runner && pip install -r requirements.txt
SANDBOX_TOKEN=change-me uvicorn app.main:app --host 127.0.0.1 --port 8090
curl localhost:8090/health
```

The runner needs access to a Docker daemon (socket). It is the only OwnAI component with that access; keep its
port internal and always set `SANDBOX_TOKEN`.

## Verified isolation (automated: `backend/tests/integration/test_sandbox.py`)

* network unreachable from inside the container
* long-running code is killed at the timeout
* root filesystem is read-only, code runs as UID 1000
* memory limit enforced (OOM detected)
* credential files (`.env`) are not shipped into the sandbox
* disallowed commands (`bash`, `pip install`) are rejected
* fork bombs are contained by the PID limit (verified manually)
