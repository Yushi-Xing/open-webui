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
- Production source maps and gzip-size reporting are disabled to fit memory.
  `compile.sh RUN_ID` reruns only compilation after tests have passed.
- Full frontend build follows the package's prebuild, pyodide fetch and Vite steps,
  with a smaller JS heap and without production source maps/gzip-size reporting.
- Dependencies: locked npm packages, jsdom 26.1.0, Playwright 1.56.1, uv 0.12.10;
  backend requirements.txt plus upstream's CPU torch/torchvision/torchaudio step.
- Browser downloads, npm modules/cache and runtime virtualenv persist in cache.
- Produce a native linux/amd64 image using the checked prebuilt frontend and
  runtime venv. Host Docker image assembly is separate from container compilation.
- On a host with enough Docker storage, `image.sh RUN_ID` assembles, starts,
  checks and exports the image. `image.sh RUN_ID verify-existing` skips assembly.
- This machine's system disk could not unpack the final image. Its Docker-saved
  archive was instead validated and started using `unpack-smoke.py RUN_ID` in
  the existing container as root, with default capabilities and project mounts.
  This does not verify Docker-engine load/start; preserve that limit in reports.
- Before reusing that fallback, budget space for both the archive and extracted
  rootfs on /data. Delete only `cache/image-rootfs` after its server has exited
  and results are saved. Never change Docker storage or globally prune caches.
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

See [the 2026-10-28 build record](build-20261028.md). Application source
`eaa86dca74d26955ddce9422649a0857f7ff417b` compiled successfully; later recipe-only
changes do not change that application's source identity. The runtime image is
linux/amd64 only. Its archive, frontend bundle, test reports and logs are saved.
Root Docker storage retained about 12 GiB (79%); targeted runtime cache removal
returned no reclaimed bytes. Do not repeat a Docker image unpack there without
first providing adequate space through an authorized, project-safe method.
