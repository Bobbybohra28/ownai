# Sandbox image for Python code execution, tests and linting.
# Runs as an unprivileged user; the runner adds --network none, --read-only,
# --cap-drop ALL and resource limits at run time.
FROM python:3.12-slim

# Optional: pass a corporate/proxy CA with
#   docker build --secret id=ca,src=/path/to/ca.crt ...
RUN --mount=type=secret,id=ca,required=false \
    if [ -f /run/secrets/ca ]; then export PIP_CERT=/run/secrets/ca; fi; \
    pip install --no-cache-dir "pytest>=8.3,<9" "pytest-asyncio>=0.24" "ruff>=0.6" \
    && useradd --uid 1000 --create-home --shell /usr/sbin/nologin sandbox

USER 1000:1000
WORKDIR /workspace
