#!/usr/bin/env bash
# Focused source regressions; reuse the installed toolchain, no production build.
set -euo pipefail
ulimit -c 0
cd /work/src
run_id=${1:?run id required}
mkdir -p /work/artifacts/"$run_id" /work/logs/"$run_id"
exec > >(tee /work/logs/"$run_id"/context-tests.log) 2>&1
trap 'code=$?; printf "%s\n" "$code" > /work/logs/"$run_id"/context-tests.exit; exit "$code"' EXIT
date -u
python --version
node --version
PYTHONPATH=/work/cache/test-python python -m unittest discover -s tests/fork -p 'test_context_*.py' -v
npx --no-install vitest run --config vitest.fork.config.ts --reporter=verbose --reporter=junit --outputFile=/work/artifacts/"$run_id"/fork-tests.xml
node --input-type=module <<'JS'
import { readFile } from 'node:fs/promises';
import { preprocess, compile } from 'svelte/compiler';
import { vitePreprocess } from '@sveltejs/vite-plugin-svelte';
const filename = 'src/lib/components/chat/Chat.svelte';
const source = await readFile(filename, 'utf8');
const processed = await preprocess(source, vitePreprocess(), { filename });
const result = compile(processed.code, { filename, generate: 'client' });
console.log(`Chat.svelte compiled in memory; ${result.warnings.length} compiler warnings`);
for (const warning of result.warnings) console.log(`${warning.code}: ${warning.message}`);
JS
date -u
