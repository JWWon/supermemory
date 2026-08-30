FROM debian:bookworm-slim
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates curl \
 && rm -rf /var/lib/apt/lists/*
ARG SM_VERSION=0.0.8
ARG SM_SHA256=eeb9e62a8bf59646bd799a05d1a2981b945413a03640c3a39f4f267ad2d2bf37
RUN curl -fsSL -o /usr/local/bin/supermemory-server \
      "https://github.com/supermemoryai/supermemory/releases/download/server-v${SM_VERSION}/supermemory-server-linux-arm64" \
 && echo "${SM_SHA256}  /usr/local/bin/supermemory-server" | sha256sum -c - \
 && chmod +x /usr/local/bin/supermemory-server
RUN useradd -m -u 10001 sm && mkdir -p /data && chown sm:sm /data
USER sm
ENV SUPERMEMORY_DATA_DIR=/data
EXPOSE 6767
ENTRYPOINT ["/usr/local/bin/supermemory-server"]
