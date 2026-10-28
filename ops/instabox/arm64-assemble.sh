#!/usr/bin/env bash
# Run inside build-open-webui, under the host build lock and process timeout.
set -euo pipefail
ulimit -c 0
run_id=${1:?run id required}
mkdir -p /work/artifacts/"$run_id" /work/logs/"$run_id"
exec > >(tee -a /work/logs/"$run_id"/arm64-assemble.log) 2>&1
trap 'code=$?; printf "%s\n" "$code" > /work/logs/"$run_id"/arm64-assemble.exit; exit "$code"' EXIT
bash /work/scripts/arm64-tools.sh
python /work/scripts/arm64-layer.py "$run_id"
crane=/work/cache/arm64-tools/crane
base=$(python -c 'import json; print(json.load(open("/work/scripts/arm64-inputs.json"))["arm64_base"])')
source_sha=$(python -c 'import json; print(json.load(open("/work/src/SOURCE.json"))["commit"])')
archive=/work/artifacts/"$run_id"/open-webui-arm64.docker.tar
# --output saves a Docker archive. No registry login or push occurs here.
"$crane" mutate "$base" --platform linux/arm64 \
    --append /work/artifacts/"$run_id"/application-layer.tar \
    --env WEBUI_BUILD_HASH="$source_sha" \
    --label org.opencontainers.image.source=https://github.com/Yushi-Xing/open-webui \
    --label org.opencontainers.image.revision="$source_sha" \
    --tag "ghcr.io/yushi-xing/open-webui:git-${source_sha:0:12}-arm64" \
    --output "$archive"
"$crane" validate --fast --tarball "$archive"
sha256sum "$archive" > /work/artifacts/"$run_id"/image.sha256
python - "$run_id" <<'PY'
import hashlib, json, sys, tarfile
from pathlib import Path
root=Path('/work/artifacts')/sys.argv[1]
with tarfile.open(root/'open-webui-arm64.docker.tar') as archive:
    manifest=json.load(archive.extractfile('manifest.json'))[0]
    raw=archive.extractfile(manifest['Config']).read()
    config=json.loads(raw)
    assert config['architecture']=='arm64' and config['os']=='linux'
    assert config['config']['Labels']['org.opencontainers.image.revision']==json.loads((root/'arm64-inputs.json').read_text())['commit']
    (root/'image-config.json').write_bytes(raw)
    print('Actual arm64 config verified:',hashlib.sha256(raw).hexdigest())
PY
