FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir '.[mysql,mcp]'
COPY SOUL.md ./
COPY skills ./skills
RUN useradd --create-home --uid 10001 evomesh && mkdir /data && chown evomesh /data
USER evomesh
WORKDIR /data
ENTRYPOINT ["evomesh"]
CMD ["serve", "--host", "0.0.0.0", "--repo", "/app"]
