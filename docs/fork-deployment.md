# Custom Open WebUI deployment

Do not run frontend or Docker builds on the local control machine. The current
authorized builder is the existing InstaBox `compile-server`, following
[its project recipe](../ops/instabox/AGENTS.md). Compilation, dependency installs
and tests run in `build-open-webui`; image assembly runs on that remote host.
Only clean source is uploaded, and publication uses the control machine.
The older manual Actions workflow remains available but is not used for this run.

The fork now includes upstream tag `v0.12.4` (commit
`33c1978d5bad52a31df75ea55143025dfad18d16`) while retaining the customizations
below. Image checks validate both the source build hash and package version.

The `Fork tests and Docker image` workflow tests prompt defaults, real TipTap
pastes and Markdown serialization, JSON handoff between prompt editors, and
read-only code rendering. It then builds standard images on native amd64 and
arm64 runners. Each image must pass startup, health, build fingerprint, and
stylesheet checks before publication. Failed test results and container logs
are retained as Actions artifacts.

The release workflow runs only through `workflow_dispatch`; pushing commits does
not start another image build. Both native architecture builds reuse their GHCR
build caches. Test artifacts and Docker build records are retained for seven days.

Images are published under `ghcr.io/yushi-xing/open-webui`:

- `git-<first 12 characters of commit SHA>` identifies a tested source revision.
- `custom` follows the last multi-architecture Actions release. The separate
  amd64-only InstaBox publication does not move this tag.

Use the commit tag for deployment and keep the previous image for rollback.
Private GHCR packages require a registry login on the VPS. Package visibility
is controlled in GitHub package settings.

## Changes in this fork

- Rich text prompt editors preserve titled HTML links as Markdown links. This
  includes the main prompt, expanded input, and channel input. Automatic URL
  linking and click navigation are disabled for prompt editors. Plain text paste
  continues to use the clipboard's plain text.
- The default interface scale is 1.2. Existing personal or administrator overrides
  take precedence. Resetting the personal scale returns to the effective default.
- `static/static/custom.css` is the default Markdown stylesheet. It was copied unchanged
  from the supplied CSS file; its SHA-256 is
  `ca0357aa7883dfb2c10e0dc620925f966f2b85b3d00c6dea74705e3e5b67f103`.
  CSS controls rendering styles; Markdown syntax parsing remains unchanged.
- Assistant reply code blocks and execution details use read-only CodeMirror,
  preserving the original line numbers, folding, light/dark highlighting, and
  layout. Copy, download, and preview remain available where previously enabled.
- The custom stylesheet is loaded after app styles and remains last when styles
  are added during navigation. Its 16px body size and 1.95 line height therefore
  override the scaled app defaults. The supplied CSS file remains unchanged.
- Each chat request appends the following rules after the existing system prompt,
  without adding another copy when they are already present:

```text
Additional rules for code output
1. Generated program code, HTML, CSS, JavaScript, Python, Shell, SQL, configuration files, text templates, and similar content must be returned directly in the chat reply. Use Markdown code blocks with the correct language identifier.
2. If the user asks you to "generate a file", create the file and also print its contents in the terminal.
Additional rules for knowledge-based questions and fact-checking
1、For factual, technical, or academic questions, analyze the issue thoroughly and rigorously before responding. Do not rush to an answer.
2、For questions that are complex, specialized, time-sensitive, or involve uncertainty, proactively search the web. Prioritize authoritative primary sources, such as official documentation, academic papers, and technical standards, and cross-check them against your own analysis. Revise your conclusions when you encounter conflicting evidence, rather than looking only for evidence that supports your initial view.
3、For basic questions with well-established answers, web searches are not mandatory. Balance answer quality with efficiency.
4、Your final answer should be accurate, clear, and well-supported, with references where appropriate. Clearly distinguish verified facts, reasonable inferences, and uncertain information. Do not fabricate conclusions or citations.
```

The file/terminal instruction requires an available tool to execute those actions;
the prompt does not itself provide terminal access or guarantee model compliance.

## Upstream fix review (v0.12.4)

The October 27, 2026 review retains both fork behavior changes:

- `RichTextInput.svelte` still defaults links off and the chat input does not
  enable them. Keep `PromptLink` and the Markdown URL preservation regressions.
- Assistant code blocks still default to editable upstream. Upstream now uses
  read-only CodeMirror when remote Markdown content is disabled; the fork uses
  that renderer for all ordinary read-only code blocks, preserving its earlier
  behavior. The one merge conflict in this renderer was resolved accordingly.
- Scale 1.2, the supplied Markdown CSS and appended system rules remain intact.
- Test store fixtures now include upstream's new `visibleControls` store.
- Upstream's image base changed to Python 3.12 / Debian trixie. Both native
  architecture images must pass the existing startup and HTTP checks in Actions.

See [context-compaction-analysis.md](context-compaction-analysis.md) for the
source-backed analysis and fork fix for search-result context overflows. With
context compaction enabled, the fork checks rendered requests, summarizes large
source/tool text in bounded chunks, and stops requests that cannot fit the
configured budget. User input, tool pairing and source identifiers are retained.
Provider token counts remain estimates; use a threshold below the actual API
limit. The manual release workflow includes focused backend regressions.

## Update the existing deployment

### InstaBox GHCR image (2026-10-28, amd64 only)

The tested InstaBox archive was published from the control machine without
rebuilding it. It supports **linux/amd64 only**:

```yaml
image: ghcr.io/yushi-xing/open-webui:git-eaa86dca74d2-amd64
```

Manifest digest:
`sha256:34602629b2e34fdb82055e8486b36168dc8ff3af9c10ded0a3dac27673fd4cfc`.
The published config matches the tested archive, including its source revision
and layer diff IDs. Existing `custom` amd64/arm64 manifests were left intact.

Keep the existing Compose project, data volume, secret and other settings. Remove
`pull_policy: never` if present, back up the data, then deploy:

```bash
docker compose pull open-webui
docker compose up -d --no-deps open-webui
docker compose logs --tail=100 open-webui
curl -fsS http://127.0.0.1:3000/health
```

The build and validation limits are recorded in
[the build record](../ops/instabox/build-20261028.md). Docker engine load/start
was not verified on the builder because its system disk was too small; the exact
saved rootfs passed startup, HTTP and installed-backend smoke checks inside the
project container. Registry validation does not replace deployment acceptance.

### Offline alternative for the same image

The saved archive remains available. After transferring
`open-webui-amd64.docker.tar.gz` to the VPS, check its SHA-256:

```text
86d77536985967bf8182b7f0761d5e8d92fd2a3a478167937a523b81c6aedcb4
```

Load it on an amd64 Docker host with enough free image storage:

```bash
sha256sum open-webui-amd64.docker.tar.gz
docker load -i open-webui-amd64.docker.tar.gz
```

Keep the existing Compose project, secret, data volume and settings. Set:

```yaml
image: ghcr.io/yushi-xing/open-webui:git-eaa86dca74d2-amd64
pull_policy: never
```

Back up the data before updating. Then run:

```bash
docker compose up -d --no-deps --pull never open-webui
docker compose logs --tail=100 open-webui
curl -fsS http://127.0.0.1:3000/health
```

Using `pull_policy: never` and `--pull never` selects the locally loaded image.
The mounted `./theme/custom.css` still overrides the built-in CSS; keep or remove
it according to which stylesheet you want.

### Published GHCR images

Back up the stopped service's `/app/backend/data` directory before updating. Keep
the current Compose directory/project name, data volume, and `WEBUI_SECRET_KEY`.
Changing the Compose project name can attach a different named volume.

In the existing Compose file, change only the image to the published commit tag:

```yaml
image: ghcr.io/yushi-xing/open-webui:git-<commit-prefix>
```

Remove `pull_policy: never` if present. To use the stylesheet built into the image,
remove this optional override:

```yaml
- ./theme/custom.css:/app/build/static/custom.css:ro
```

Keeping that mount deliberately overrides the bundled stylesheet with the VPS file.
Keep the data volume mount and all existing environment values.

```bash
docker compose pull open-webui
docker compose up -d --no-deps open-webui
docker compose logs --tail=100 open-webui
curl -fsS http://127.0.0.1:3000/health
```

Refresh the browser, paste a titled link, expand and close the input, and check
that the submitted message contains the URL. Check 1.2 as the default scale,
Markdown typography in both themes, and code selection/copy without editing.

`docker-compose.fork.yaml` provides an equivalent configuration for a new
deployment; existing deployments should retain their original Compose project
and volume. Never use `docker compose down -v` when updating.

Rollback by restoring the previous image reference. If the underlying upstream
version changes and migrates the database, restoring the pre-update data backup
may also be necessary.
