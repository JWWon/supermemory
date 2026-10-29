# Temporary: official 0.0.9 with patch.py applied so disposable writes stop forcing
# full-store snapshots (supermemoryai/supermemory#1413) and snapshots run every 300 s
# instead of 10 s (supermemoryai/supermemory#1826). When a fixed release ships,
# delete this directory and point compose.yml back at the official image.
ARG BASE=docker.io/supermemory/supermemory:0.0.9@sha256:557781468b448da518a0d45eacc0ff894bd7847769aa8a20023db2d51d6cef43

FROM ${BASE} AS upstream

FROM python:3.13-alpine AS patch
COPY --from=upstream /usr/local/bin/supermemory-server /work/supermemory-server
COPY patch.py /work/patch.py
RUN python3 /work/patch.py /work/supermemory-server

FROM ${BASE}
COPY --from=patch --chmod=755 /work/supermemory-server /usr/local/bin/supermemory-server
