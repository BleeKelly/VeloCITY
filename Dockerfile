FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    VELOCITY_HOME=/data \
    TZ=America/New_York

WORKDIR /app
COPY pyproject.toml ./
COPY src ./src
RUN pip install .

# /data/raw holds the nflverse parquet cache (~550 MB for 1999-now), /data/output the CSVs.
VOLUME /data
EXPOSE 8097

HEALTHCHECK --interval=60s --timeout=5s --start-period=10m \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8097/api/status', timeout=4)"

# Unraid convention: run as nobody:users (99:100) so appdata files stay editable from shares.
USER 99:100
CMD ["velocity", "serve", "--host", "0.0.0.0", "--port", "8097"]
