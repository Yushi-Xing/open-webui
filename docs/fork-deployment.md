# Custom Open WebUI deployment

All compilation and Docker image builds run in GitHub Actions. Do not run
`npm run build`, Vite compilation, or `docker build` locally.

The `Fork tests and Docker image` workflow tests prompt defaults, real TipTap
pastes and Markdown serialization, JSON handoff between prompt editors, and
read-only code rendering. It then builds standard images on native amd64 and
arm64 runners. Each image must pass startup, health, build fingerprint, and
stylesheet checks before publication. Failed test results and container logs
are retained as Actions artifacts.

Images are published under `ghcr.io/yushi-xing/open-webui`:

- `git-<first 12 characters of commit SHA>` identifies a tested source revision.
- `custom` follows the latest successfully tested custom build.

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
- `static/custom.css` is the default Markdown stylesheet. It was copied unchanged
  from the supplied CSS file; its SHA-256 is
  `ca0357aa7883dfb2c10e0dc620925f966f2b85b3d00c6dea74705e3e5b67f103`.
  CSS controls rendering styles; Markdown syntax parsing remains unchanged.
- Assistant reply code blocks and execution details are read-only. Copy,
  highlighting, download, and preview remain available where previously enabled.
- Each chat request appends the following rules after the existing system prompt,
  without adding another copy when they are already present:

```text
Additional rules for code output
1. Generated program code, HTML, CSS, JavaScript, Python, Shell, SQL, configuration files, text templates, and similar content must be returned directly in the chat reply. Use Markdown code blocks with the correct language identifier.
2. If the user asks you to "generate a file", create the file and also print its contents in the terminal.
```

The file/terminal instruction requires an available tool to execute those actions;
the prompt does not itself provide terminal access or guarantee model compliance.

## Update the existing deployment

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
