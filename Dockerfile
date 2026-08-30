FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Fixed high UID on purpose. data/ and reports/ are bind-mounted from the host,
# so the owning id must be stable and must not collide with a host system
# account that a package upgrade could reassign.
RUN groupadd -g 10001 app && useradd -u 10001 -g 10001 -M -s /usr/sbin/nologin app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Bake the embedding model into the image rather than downloading it on first
# use. A container that reaches for a 130MB model the first time somebody
# opens the committee view is a container that looks broken for two minutes,
# and one on a restricted network is a container that silently falls back to
# lexical matching while still calling itself semantic.
ENV FASTEMBED_CACHE_PATH=/app/.fastembed
RUN python -c "from fastembed import TextEmbedding;       TextEmbedding(model_name='BAAI/bge-small-en-v1.5')"

COPY src/ ./src/
COPY api/ ./api/
COPY web/ ./web/
COPY config/ ./config/
# Reference tables, not runtime state, so they live outside data/ which
# .dockerignore excludes. Without these the CUSIP and sector lookups fall
# back to empty and every 13F book silently prices as zero positions.
COPY reference/ ./reference/
COPY prompts/ ./prompts/
# Every entry point. run_event.py was missing here once, and the news watcher
# failed silently every 20 minutes for 17 hours because the scheduler kept
# reporting a clean exit for a file that was not in the image.
COPY run_daily.py run_event.py run_committee.py refresh_trackers.py scheduler.py ./

# data/ and reports/ are bind-mounted at runtime so the audit trail outlives
# any container rebuild.
RUN mkdir -p data/snapshots data/runs data/rag data/smartmoney data/committee reports && chown -R app:app /app

USER app

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz',timeout=4).status==200 else 1)"

CMD ["uvicorn", "api.main:app", "--host", "0.0.0.0", "--port", "8000"]
