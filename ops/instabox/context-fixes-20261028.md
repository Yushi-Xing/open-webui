# Context compaction regressions — 2026-10-28

Source fixes follow commit `595c8f7946393520cc868cb426652c4307609c4b`.
The exact source file hashes and final commit are preserved in the delivery
manifest under `artifacts/context-fixes/20261028-six/` (ignored local artifacts).
The existing remote packaged base is `eaa86dca74d2`; only seven explicitly checked
source/test/script files and `PATCH.json` were uploaded. No `.git`, credentials,
real environment files or authentication configuration were uploaded.

The existing `build-open-webui` container and cached dependencies were reused.
No environment packages, host configuration or Docker images were changed.
The control machine used Ruff 0.15.5 from a temporary directory for formatting;
the container used its existing Prettier 3.8.1 for the two new TypeScript files.

Run under the shared host lock, with a 1200-second timeout inside the container:

```bash
flock -n -E 75 /data/workspace/.build.lock docker exec build-open-webui \
  timeout --signal=TERM --kill-after=60s 1200 \
  bash /work/src/ops/instabox/test-context.sh 20261028-context-six-01
```

Machine run began at `2026-10-28T12:06:24.592962Z`, with a 480-minute platform
limit. At submission 25967 seconds remained after reserving 30 minutes for
cleanup. Job `93d84d1b` exited 0:

- 54 backend tests passed, including actual tiktoken; DB/model/network boundaries
  use isolated test doubles, while serializers, payload merging and budget guards
  execute production code.
- 20 frontend tests passed in five files, including four active-context usage
  cases and existing link-paste, scale, read-only code and diff regressions.
- `Chat.svelte` preprocessed and compiled in memory. One unchanged non-void
  self-closing HTML warning and the existing virtual-list export warning remain.
- After the final serializer refactor/import cleanup, job `87431ceb` reran only
  the 54 backend tests; all passed and the actual exit code was 0. Frontend
  source hashes were unchanged from the full passing run.

Local backend checks: 54 tests, 53 passed and one optional tiktoken test skipped.
Ruff formatting and `git diff --check` passed. Whole-file Ruff lint still reports
pre-existing complexity/unused-import categories in the modified backend files;
this is not a clean full-repository lint run. New helper/test code has no new
lint findings.

Remote reports: `artifacts/20261028-context-six-01/fork-tests.xml` and
`logs/20261028-context-six-01/`. Control-side copies and SHA-256 checks are kept
in `artifacts/context-fixes/20261028-six/`.

No production frontend build, Docker image build, Actions run, registry
publication or production deployment was performed. Published
`git-eaa86dca74d2` images do not contain these source fixes. Final disk usage,
process checks and verified stopped state are saved with the delivery records.
