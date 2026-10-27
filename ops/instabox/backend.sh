#!/usr/bin/env bash
set -euo pipefail
cd /work/src
run_id=${1:?run id required}
exec > >(tee -a /work/logs/"$run_id"/backend.log) 2>&1
trap 'code=$?; printf "%s\n" "$code" > /work/logs/"$run_id"/backend.exit; exit "$code"' EXIT
# Keep dependencies on /data, with the same absolute path in the runtime image.
uv venv --python /usr/local/bin/python /work/cache/runtime-venv
uv pip install --python /work/cache/runtime-venv/bin/python --index https://download.pytorch.org/whl/cpu 'torch<=2.9.1' torchvision torchaudio
uv pip install --python /work/cache/runtime-venv/bin/python -r backend/requirements.txt
/work/cache/runtime-venv/bin/python -c 'import tiktoken; tiktoken.get_encoding("cl100k_base"); print("Runtime tokenizer ready")'
/work/cache/runtime-venv/bin/python - <<'PY'
import importlib.metadata as m
import json
from pathlib import Path
packages={d.metadata['Name']: d.version for d in m.distributions() if d.metadata.get('Name')}
Path('/work/cache/runtime-packages.json').write_text(json.dumps(packages,indent=2,sort_keys=True))
print('Installed runtime packages:',len(packages))
PY
cp /work/cache/runtime-packages.json /work/artifacts/"$run_id"/runtime-packages.json
