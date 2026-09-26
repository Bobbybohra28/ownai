FROM python:3.12-slim
# Docker CLI only (talks to the host daemon through the mounted socket).
COPY --from=docker:27-cli /usr/local/bin/docker /usr/local/bin/docker
WORKDIR /srv
COPY requirements.txt .
RUN --mount=type=secret,id=ca,required=false \
    if [ -f /run/secrets/ca ]; then export PIP_CERT=/run/secrets/ca; fi; \
    pip install --no-cache-dir -r requirements.txt
COPY app ./app
EXPOSE 8090
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8090"]
