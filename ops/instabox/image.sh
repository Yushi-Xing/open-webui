#!/usr/bin/env bash
# Host-side Docker assembly/export only; all compile/install/test work stays in containers.
set -euo pipefail
ulimit -c 0
cd /data/workspace/projects/open-webui
run_id=${1:?run id required}
mode=${2:-build}
source_sha=$(docker exec build-open-webui python -c 'import json; print(json.load(open("/work/src/SOURCE.json"))["commit"])')
image="ghcr.io/yushi-xing/open-webui:git-${source_sha:0:12}-amd64"
smoke="smoke-open-webui-${source_sha:0:12}"
exec > >(tee -a logs/"$run_id"/image.log) 2>&1
cleanup() {
    code=$?
    if docker inspect "$smoke" >/dev/null 2>&1; then
        docker logs "$smoke" > logs/"$run_id"/runtime.log 2>&1 || true
        docker rm -f "$smoke" >/dev/null || true
    fi
    printf '%s\n' "$code" > logs/"$run_id"/image.exit
    exit "$code"
}
trap cleanup EXIT
df -h / /data
docker system df
if test "$mode" = build; then
    docker build --progress=plain --build-arg BUILD_HASH="$source_sha" -f Dockerfile.runtime -t "$image" .
fi
mkdir -p cache/smoke-data
# No public port, real user credentials, registry login, local model server, or GPU.
docker run -d --name "$smoke" --memory=4g --cpus=2 \
    -e WEBUI_SECRET_KEY=ci-smoke-only-not-for-deployment \
    -e ENABLE_OLLAMA_API=false -e ENABLE_CONTEXT_COMPACTION=true \
    -e CONTEXT_COMPACTION_TOKEN_THRESHOLD=80000 -e CONTEXT_COMPACTION_TOKEN_CAP=80000 \
    -v "$PWD/cache/smoke-data:/app/backend/data" \
    -v "$PWD/scripts/runtime-smoke.py:/work/runtime-smoke.py:ro" "$image"
ready=false
for attempt in $(seq 1 180); do
    if docker exec "$smoke" curl --max-time 3 -fsS http://127.0.0.1:8080/health > artifacts/"$run_id"/health.json; then ready=true; break; fi
    sleep 2
done
test "$ready" = true
jq -e '.status == true' artifacts/"$run_id"/health.json
docker exec "$smoke" curl --max-time 5 -fsS http://127.0.0.1:8080/api/version > artifacts/"$run_id"/version.json
jq -e '.version == "0.12.4"' artifacts/"$run_id"/version.json
docker exec "$smoke" curl --max-time 5 -fsS http://127.0.0.1:8080/static/custom.css > artifacts/"$run_id"/served-custom.css
cmp src/static/static/custom.css artifacts/"$run_id"/served-custom.css
docker exec "$smoke" timeout --signal=TERM --kill-after=30s 300 python /work/runtime-smoke.py
docker image inspect "$image" --format '{{.Id}} {{.Architecture}} {{index .Config.Labels "org.opencontainers.image.revision"}}' > artifacts/"$run_id"/image-fingerprint.txt
printf '%s\n' "$image" > artifacts/"$run_id"/image-reference.txt
# Export the original image, never docker commit the smoke container or its data.
if ! test -s artifacts/"$run_id"/open-webui-amd64.docker.tar.gz; then
    docker save "$image" | gzip -1 > artifacts/"$run_id"/open-webui-amd64.docker.tar.gz
fi
sha256sum artifacts/"$run_id"/open-webui-amd64.docker.tar.gz > artifacts/"$run_id"/image.sha256
df -h / /data
docker system df
