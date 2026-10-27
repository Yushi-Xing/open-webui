# Open WebUI InstaBox build project

Read `/data/workspace/AGENTS.md` and `/data/README.md` first. Explicit user
instructions authorize InstaBox for this build instead of GitHub Actions.

- Project root: `/data/workspace/projects/open-webui`.
- Source: `src/`, with a clean `SOURCE.json` and checked file hashes, no `.git` or credentials.
- Container: `build-open-webui`; environment image `local/build-open-webui:py312-node22-v1`.
- Recipe: `Dockerfile`, `compose.yaml`, `Dockerfile.runtime`, `scripts/*.sh`.
- `src`, `cache`, `artifacts`, `logs`, `scripts` are project-owned bind mounts.
- Start: `docker compose -f /data/workspace/projects/open-webui/compose.yaml up -d`.
- Stop: `docker stop build-open-webui`. No automatic restart or public ports.
- Build environment on the host under the shared flock, then install/compile/test
  inside this container. It has no Docker socket, host root/HOME, or credentials.
- Memory: 6 GiB, CPU: 4, frontend JS heap: 4608 MiB. No swap is assumed.
- Frontend/tests: `flock -n -E 75 /data/workspace/.build.lock docker exec build-open-webui timeout --signal=TERM --kill-after=60s SECONDS bash /work/scripts/frontend.sh RUN_ID`.
- Runtime dependencies: same lock/timeout pattern with `backend.sh RUN_ID`.
- Long operations: submit once via local `instabox exec --background`; preserve
  job ID, actual exit status, UTC timestamps and logs. Compute SECONDS from the
  machine's real runStartedAt/maxRunMinutes and retain 30 minutes for cleanup.
- Full frontend build follows the package's prebuild, pyodide fetch and Vite steps;
  it lowers only the heap flag from 12288 MiB to fit this machine.
- Dependencies: locked npm packages, jsdom 26.1.0, Playwright 1.56.1, uv 0.12.10;
  backend requirements.txt plus upstream's CPU torch/torchvision/torchaudio step.
- Browser downloads, npm modules/cache and runtime virtualenv persist in cache.
- Produce a native linux/amd64 image using the checked prebuilt frontend and
  runtime venv. Host Docker image assembly is separate from container compilation.
- Run image startup/API version/CSS/fingerprint checks before exporting it.
- Use `docker save` on the host into artifacts, pull to the control machine,
  and publish from the control machine only. Never authenticate remotely.
- Preserve artifact hashes, source commit and job records under artifacts/RUN_ID
  and logs/RUN_ID. Keep last three delivered builds; delete only verified
  regenerable data owned by this project when no task is using it.
- Check both filesystems and Docker usage before each large stage. At >=80%
  reclaim own regenerable data first; at >=90% or <2 GiB free start no build/pull.
- Finish by inspecting the global lock, all running containers' actual processes,
  host process names and jobs. If no real work remains, stop the VM locally and
  confirm state=stopped. Do not use session/login counts as a blocker.

## Successful builds

No successful InstaBox build is recorded yet. Append measured results only.
