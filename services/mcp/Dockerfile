FROM oven/bun:1.3.14-slim
WORKDIR /app
COPY package.json bun.lock ./
RUN bun install --frozen-lockfile --production --ignore-scripts
COPY server.ts ./
USER bun
ENV HOST=0.0.0.0 PORT=8788
EXPOSE 8788
CMD ["bun", "run", "server.ts"]
