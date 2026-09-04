FROM python:3.13-slim

WORKDIR /app

# curl is only used by the HEALTHCHECK below.
RUN apt-get update && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

# Layer-cache dependency install separately from the source copy below.
COPY requirements.txt .
# xgboost unconditionally depends on nvidia-nccl-cu13 (a ~250MB GPU
# communication library) even for CPU-only inference -- this pipeline never
# does GPU training/scoring, so it's installed and immediately dropped.
RUN pip install --no-cache-dir -r requirements.txt \
    && pip uninstall -y nvidia-nccl-cu13

COPY . .

RUN useradd --create-home --uid 1000 appuser \
    && mkdir -p logs data models/artifacts models/custom_runs demo \
    && chown -R appuser:appuser /app
USER appuser

EXPOSE 5555

HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD curl -fs http://localhost:5555/health || exit 1

CMD ["gunicorn", "-c", "gunicorn.conf.py", "wsgi:app"]
