# docker build -t lexflow src/
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    DATABASE_URL=sqlite:////data/lexflow.db \
    LEXFLOW_LLM_CACHE=/data/llm-cache \
    PORT=8080

# Optional reasoning layer: the Claude Code CLI (`claude -p`), authenticated with CLAUDE_KEY at runtime.
ARG WITH_CLAUDE=1
RUN if [ "$WITH_CLAUDE" = "1" ]; then \
      apt-get update && apt-get install -y --no-install-recommends nodejs npm && \
      npm install -g @anthropic-ai/claude-code && apt-get purge -y npm && apt-get autoremove -y && \
      rm -rf /var/lib/apt/lists/*; fi

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY lexflow ./lexflow

RUN useradd -r -u 10001 -m lexflow && mkdir -p /data && chown lexflow /data
USER lexflow
VOLUME ["/data"]
EXPOSE 8080
HEALTHCHECK --interval=15s --timeout=3s CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/healthz')" || exit 1
CMD ["python", "-m", "lexflow.cli", "serve"]
