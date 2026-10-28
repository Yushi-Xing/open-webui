"""Inspect GHCR versions and remove only versions outside retained manifests."""

import json
import os
import re
from datetime import datetime, timedelta, timezone
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
RECENT_HOURS = int(os.environ.get('KEEP_RECENT_HOURS', '24'))
if RECENT_HOURS < 24:
    raise RuntimeError('Cleanup must retain at least the last 24 hours.')
CUTOFF = datetime.now(timezone.utc) - timedelta(hours=RECENT_HOURS)
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
        'Cache-Control': 'no-cache',
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
def list_versions():
    result = []
    page = 1
    while True:
        batch = api(f'{package_path}/versions?per_page=100&page={page}')
        result.extend(batch)
        if len(batch) < 100:
            return result
        page += 1


versions = list_versions()

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


def is_recent(version, cutoff):
    values = [version.get('created_at'), version.get('updated_at')]
    if not all(values):
        return True  # Unknown age is not evidence of expiration.
    return any(datetime.fromisoformat(value.replace('Z', '+00:00')) >= cutoff for value in values)


def retained_roots(rows, cutoff):
    return [version for version in rows if (
        (tags(version) and not all(obsolete_tag(tag) for tag in tags(version)))
        or is_recent(version, cutoff)
    )]


def manifest_closure(roots, fetch):
    protected = set()
    stack = [version['name'] for version in roots]
    while stack:
        digest = stack.pop()
        if digest in protected:
            continue
        data, actual = fetch(digest)
        if digest != actual:
            raise RuntimeError(f'Registry digest mismatch: {digest} != {actual}')
        protected.add(digest)
        stack.extend(item['digest'] for item in data.get('manifests', []))
        if data.get('subject', {}).get('digest'):
            stack.append(data['subject']['digest'])
    return protected


def snapshot(rows):
    return {version['id']: (version['name'], tuple(sorted(tags(version)))) for version in rows}


expected_retained = json.loads(os.environ['EXPECTED_RETAINED_DIGESTS'])
if set(expected_retained) != KEEP:
    raise RuntimeError('Every retained release must have a pinned digest.')
release_versions = [version for version in versions if any(re.fullmatch(r'git-[a-f0-9]{12}', tag) for tag in tags(version))]
newest_release = max(release_versions, key=lambda version: version['created_at'])
if not any('git-' + revision in tags(newest_release) for revision in KEEP):
    raise RuntimeError('Retention configuration omits the newest published release; review before cleanup.')
for revision in KEEP:
    if not any('git-' + revision in tags(version) for version in versions):
        raise RuntimeError(f'Required retained image git-{revision} is missing.')
    if manifest('git-' + revision)[1] != expected_retained[revision]:
        raise RuntimeError(f'Retained release git-{revision} differs from its pinned digest.')

# Keep unknown tags and current build caches as well as both retained releases.
roots = retained_roots(versions, CUTOFF)
protected = manifest_closure(roots, manifest)

current, current_digest = manifest('custom')
expected_tag = os.environ.get('EXPECTED_CURRENT_TAG')
if expected_tag and manifest(expected_tag)[1] != current_digest:
    raise RuntimeError('The custom tag does not match its expected retained release.')
expected_digest = os.environ.get('EXPECTED_CURRENT_DIGEST')
if expected_digest and expected_digest != current_digest:
    raise RuntimeError('The custom tag changed since the cleanup plan was generated.')
if current_digest not in protected:
    raise RuntimeError('The custom tag is not protected.')
current_arches = {item.get('platform', {}).get('architecture') for item in current.get('manifests', [])}
if not {'amd64', 'arm64'} <= current_arches:
    raise RuntimeError('The current custom image does not have both architectures.')

candidates = [version for version in versions if version['name'] not in protected]
if any(tags(version) and not all(obsolete_tag(tag) for tag in tags(version)) for version in candidates):
    raise RuntimeError('The deletion plan contains a retained or unknown tag.')
if APPLY:
    reviewed_ids = {int(value) for value in os.environ['REVIEWED_DELETE_IDS'].split(',') if value.strip()}
    if {version['id'] for version in candidates} != reviewed_ids:
        raise RuntimeError('The deletion candidates differ from the reviewed inventory. Inspect again before applying.')

audit = {
    'apply': APPLY,
    'package': IMAGE,
    'keep_revisions': sorted(KEEP),
    'keep_recent_hours': RECENT_HOURS,
    'cutoff_utc': CUTOFF.isoformat(),
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
    expected_inventory = snapshot(versions)
    for version in candidates:
        if snapshot(list_versions()) != expected_inventory:
            raise RuntimeError('Package inventory changed during cleanup; stopping before further deletion.')
        fresh = api(f'{package_path}/versions/{version["id"]}')
        if fresh['name'] != version['name'] or sorted(tags(fresh)) != sorted(tags(version)):
            raise RuntimeError(f'Version {version["id"]} changed since inspection; refusing deletion.')
        api(f'{package_path}/versions/{version["id"]}', method='DELETE')
        audit['deleted_ids'].append(version['id'])
        expected_inventory.pop(version['id'])
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
    remaining = list_versions()
    if snapshot(remaining) != expected_inventory:
        raise RuntimeError('Final active versions do not match the exact deletion plan.')
    if {item['id'] for item in remaining} & set(audit['deleted_ids']):
        raise RuntimeError('Deleted version IDs are still present in the active inventory.')
    audit['versions_after'] = remaining
    audit['retained_manifests_verified'] = True
    save()

summary = os.environ.get('GITHUB_STEP_SUMMARY')
if summary:
    with open(summary, 'a') as output:
        output.write(f'GHCR cleanup: {len(audit["deleted_ids"])} deleted; {len(candidates)} candidates.\n\n')
        output.write('Retained releases: ' + ', '.join('`git-' + revision + '`' for revision in sorted(KEEP)) + '.\n')
        output.write('Retained all platform children, current build caches, and unknown tags.\n')
