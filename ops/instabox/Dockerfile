# Build toolchain only. Source, caches and outputs are mounted by compose.
ARG NODE_IMAGE=node:22-bookworm-slim@sha256:de170b39439eb0e5a3444ad7d60b015614a381e10fecae2e30ec9e735122f38d
ARG PYTHON_IMAGE=python:3.12-slim-trixie@sha256:12316debbfb86890f6b2d825820612741ba527ad5a44e3902759141d572bfe8f
FROM ${NODE_IMAGE} AS node
FROM ${PYTHON_IMAGE}
COPY --from=node /usr/local/bin/node /usr/local/bin/node
COPY --from=node /usr/local/lib/node_modules /usr/local/lib/node_modules
RUN ln -s ../lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
    && ln -s ../lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx \
    && apt-get update \
    && apt-get install -y --no-install-recommends bash curl jq ca-certificates git build-essential pkg-config libmariadb-dev pandoc ffmpeg libsm6 libxext6 \
    && rm -rf /var/lib/apt/lists/*
RUN npm install --prefix /opt/test-tools --no-package-lock --ignore-scripts --no-audit --no-fund jsdom@26.1.0 playwright@1.56.1 \
    && /opt/test-tools/node_modules/.bin/playwright install-deps chromium \
    && rm -rf /var/lib/apt/lists/* /root/.npm \
    && python -m pip install --no-cache-dir uv==0.12.10
WORKDIR /work/src
CMD ["sleep", "infinity"]
