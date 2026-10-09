FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    VELOCITY_STORE=/data \
    VELOCITY_PORT=8097 \
    VELOCITY_ADMIN_PORT=8098 \
    TZ=America/New_York

WORKDIR /app
COPY pyproject.toml ./
COPY src ./src
RUN pip install .

# /data/raw holds the nflverse parquet cache (~550 MB for 1999-now), /data/output the CSVs,
# /data/settings.json the scoring rules and model settings edited in the admin UI.
VOLUME /data
# Public site and rules admin; change with VELOCITY_PORT / VELOCITY_ADMIN_PORT.
EXPOSE 8097 8098

HEALTHCHECK --interval=60s --timeout=5s --start-period=10m \
  CMD python -c "import os, urllib.request; urllib.request.urlopen(f\"http://localhost:{os.environ.get('VELOCITY_PORT', '8097')}/api/status\", timeout=4)"

# Run unprivileged; compose sets the uid:gid (VELOCITY_USER) to match the data folder's owner.
USER 1000:1000
CMD ["velocity", "serve", "--host", "0.0.0.0"]
