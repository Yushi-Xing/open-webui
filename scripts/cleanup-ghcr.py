"""Inspect GHCR versions and remove only versions outside retained manifests."""

import json
import os
import re
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import Request, urlopen


REPO = os.environ['GITHUB_REPOSITORY']
OWNER, PACKAGE = REPO.split('/')
TOKEN = os.environ['GH_TOKEN']
IMAGE = REPO.lower()
KEEP = set(os.environ['KEEP_IMAGE_REVISIONS'].split(','))
APPLY = os.environ.get('CLEANUP_APPLY') == 'true'
OUT = Path('artifacts')
OUT.mkdir(exist_ok=True)
API = 'https://api.github.com'
ACCEPT = ', '.join([
    'application/vnd.oci.image.index.v1+json',
    'application/vnd.docker.distribution.manifest.list.v2+json',
    'application/vnd.oci.image.manifest.v1+json',
    'application/vnd.docker.distribution.manifest.v2+json',
])


def api(path, method='GET'):
    request = Request(API + path, method=method, headers={
        'Authorization': 'Bearer ' + TOKEN,
        'Accept': 'application/vnd.github+json',
        'X-GitHub-Api-Version': '2022-11-28',
        'User-Agent': 'open-webui-ghcr-cleanup',
    })
    try:
        with urlopen(request, timeout=40) as response:
            body = response.read()
            return json.loads(body) if body else None
    except HTTPError as error:
        raise RuntimeError(f'{method} {path}: HTTP {error.code}: {error.read().decode()[:500]}') from None


def no_builds_running():
    for status in ('queued', 'in_progress', 'waiting', 'pending'):
        runs = api(f'/repos/{REPO}/actions/runs?status={status}&per_page=100')['workflow_runs']
        if any(run['name'] == 'Fork tests and Docker image' for run in runs):
            raise RuntimeError('A release build is active. Wait for it to finish before cleanup.')


no_builds_running()
owner_type = api('/users/' + OWNER)['type']
package_path = f'/{"orgs" if owner_type == "Organization" else "users"}/{OWNER}/packages/container/{quote(PACKAGE, safe="")}'
versions = []
page = 1
while True:
    batch = api(f'{package_path}/versions?per_page=100&page={page}')
    versions.extend(batch)
    if len(batch) < 100:
        break
    page += 1

with urlopen(f'https://ghcr.io/token?service=ghcr.io&scope=repository:{IMAGE}:pull', timeout=30) as response:
    registry_token = json.load(response)['token']


def manifest(reference):
    request = Request(f'https://ghcr.io/v2/{IMAGE}/manifests/{reference}', headers={
        'Authorization': 'Bearer ' + registry_token,
        'Accept': ACCEPT,
    })
    with urlopen(request, timeout=40) as response:
        return json.load(response), response.headers['Docker-Content-Digest']


def tags(version):
    return version.get('metadata', {}).get('container', {}).get('tags', [])


def obsolete_tag(tag):
    match = re.fullmatch(r'git-([a-f0-9]{12})(?:-(amd64|arm64))?', tag)
    return bool(match and match.group(1) not in KEEP)


for revision in KEEP:
    if not any('git-' + revision in tags(version) for version in versions):
        raise RuntimeError(f'Required retained image git-{revision} is missing.')

# Keep unknown tags and current build caches as well as both retained releases.
roots = [version for version in versions if tags(version) and not all(obsolete_tag(tag) for tag in tags(version))]
protected = set()
stack = [version['name'] for version in roots]
while stack:
    digest = stack.pop()
    if digest in protected:
        continue
    data, actual = manifest(digest)
    if digest != actual:
        raise RuntimeError(f'Registry digest mismatch: {digest} != {actual}')
    protected.add(digest)
    stack.extend(item['digest'] for item in data.get('manifests', []))

current, current_digest = manifest('custom')
if current_digest not in protected:
    raise RuntimeError('The custom tag is not protected.')
current_arches = {item.get('platform', {}).get('architecture') for item in current.get('manifests', [])}
if not {'amd64', 'arm64'} <= current_arches:
    raise RuntimeError('The current custom image does not have both architectures.')

candidates = [version for version in versions if version['name'] not in protected]
if any(tags(version) and not all(obsolete_tag(tag) for tag in tags(version)) for version in candidates):
    raise RuntimeError('The deletion plan contains a retained or unknown tag.')

audit = {
    'apply': APPLY,
    'package': IMAGE,
    'keep_revisions': sorted(KEEP),
    'current_digest': current_digest,
    'versions_before': versions,
    'protected_digests': sorted(protected),
    'delete': [{'id': version['id'], 'digest': version['name'], 'tags': tags(version)} for version in candidates],
    'deleted_ids': [],
}
path = OUT / 'ghcr-cleanup-audit.json'


def save():
    path.write_text(json.dumps(audit, indent=2) + '\n')


save()
print(json.dumps({key: audit[key] for key in ('apply', 'package', 'keep_revisions', 'current_digest', 'delete')}, indent=2))
print(f'Inventory: {len(versions)} versions, {len(protected)} protected manifests, {len(candidates)} deletion candidates.')
if APPLY:
    no_builds_running()
    for version in candidates:
        fresh = api(f'{package_path}/versions/{version["id"]}')
        if fresh['name'] != version['name'] or sorted(tags(fresh)) != sorted(tags(version)):
            raise RuntimeError(f'Version {version["id"]} changed since inspection; refusing deletion.')
        api(f'{package_path}/versions/{version["id"]}', method='DELETE')
        audit['deleted_ids'].append(version['id'])
        save()
        print(f'Deleted {version["id"]}: {tags(version) or version["name"]}')

    # Each retained release must still resolve, including its platform manifests.
    for revision in KEEP:
        data, digest = manifest('git-' + revision)
        if digest not in protected:
            raise RuntimeError('Retained tag digest changed during cleanup.')
        for child in data.get('manifests', []):
            manifest(child['digest'])
    _, latest_digest = manifest('custom')
    if latest_digest != current_digest:
        raise RuntimeError('The custom tag changed during cleanup.')
    for digest in protected:
        manifest(digest)
    audit['retained_manifests_verified'] = True
    save()

summary = os.environ.get('GITHUB_STEP_SUMMARY')
if summary:
    with open(summary, 'a') as output:
        output.write(f'GHCR cleanup: {len(audit["deleted_ids"])} deleted; {len(candidates)} candidates.\n\n')
        output.write('Retained releases: ' + ', '.join('`git-' + revision + '`' for revision in sorted(KEEP)) + '.\n')
        output.write('Retained all platform children, current build caches, and unknown tags.\n')
