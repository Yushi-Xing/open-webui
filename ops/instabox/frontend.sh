#!/usr/bin/env bash
set -euo pipefail
ulimit -c 0
cd /work/src
run_id=${1:?run id required}
mkdir -p /work/cache/home /work/cache/test-python /work/artifacts/"$run_id" /work/logs/"$run_id"
exec > >(tee -a /work/logs/"$run_id"/frontend.log) 2>&1
trap 'code=$?; printf "%s\n" "$code" > /work/logs/"$run_id"/frontend.exit; exit "$code"' EXIT
export APP_BUILD_HASH=$(python -c 'import json; print(json.load(open("SOURCE.json"))["commit"])')
export APP_BUILD_CHANNEL=main
printf 'Source %s\n' "$APP_BUILD_HASH"
node --version
python --version
npm ci --force --no-audit --no-fund
ln -sfn /opt/test-tools/node_modules/jsdom node_modules/jsdom
ln -sfn /opt/test-tools/node_modules/playwright node_modules/playwright
uv pip install --target /work/cache/test-python tiktoken==0.13.0
PYTHONPATH=/work/cache/test-python python -m unittest discover -s tests/fork -p 'test_context_*.py' -v
node tests/fork/chat-defaults.node.mjs
npx svelte-kit sync
mkdir -p artifacts
npx vitest run --config vitest.fork.config.ts --reporter=verbose --reporter=junit --outputFile=artifacts/fork-tests.xml
/opt/test-tools/node_modules/.bin/playwright install chromium
node tests/fork/browser-regression.mjs
# Match npm run build's prebuild/fetch/vite stages, with a heap that fits 6 GiB.
npm run prebuild
npm run pyodide:fetch
cp /work/scripts/vite-build.mjs .instabox-build.mjs
node --max-old-space-size=4608 .instabox-build.mjs
cmp static/static/custom.css build/static/custom.css
python - <<'PY'
import json
from pathlib import Path
source=json.loads(Path('SOURCE.json').read_text())
assert Path('build/index.html').is_file()
assert any(source['commit'] in p.read_text(errors='ignore') for p in Path('build/_app').rglob('*.js'))
print('Production frontend, source hash and original stylesheet verified')
PY
cp -a artifacts /work/artifacts/"$run_id"/tests
cp SOURCE.json /work/artifacts/"$run_id"/SOURCE.json
tar -czf /work/artifacts/"$run_id"/frontend.tar.gz build
