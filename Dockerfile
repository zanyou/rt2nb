# RackTables -> NetBox migration tool.
# Self-contained image so it runs the same on any Docker host regardless of the
# system Python.  Build once, run the three phases (export/import/verify).
FROM python:3.11-slim

LABEL org.opencontainers.image.title="rt2nb" \
      org.opencontainers.image.description="RackTables 0.20.x -> NetBox 4.6 migration"

WORKDIR /app

# Dependencies first for layer caching.
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Application.
COPY rt2nb ./rt2nb
COPY config.example.yml ./

# A non-root user; the tool only needs to read config and write export JSON.
RUN useradd --create-home --uid 10001 rt2nb \
    && mkdir -p /data \
    && chown -R rt2nb:rt2nb /app /data
USER rt2nb

# Intermediate JSON and logs live under /data by default (mount a volume here).
VOLUME ["/data"]

ENTRYPOINT ["python", "-m", "rt2nb"]
CMD ["--help"]
