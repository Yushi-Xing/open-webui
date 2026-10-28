"""Create publication metadata on the builder, preserving upstream base blobs.

The complete Docker archive stays alongside this smaller transfer bundle. The
bundle contains the final registry manifest/config and new application blob;
base blobs are referenced by verified digest, not repackaged or relabelled.
"""
import hashlib
import json
from pathlib import Path
import shutil
import sys
import tarfile

WORK = Path('/work')
RUN = sys.argv[1]
assert Path(RUN).name == RUN and RUN not in ('.', '..')
ART = WORK / 'artifacts' / RUN
LOG = WORK / 'logs' / RUN
smoke = json.loads((ART / 'arm64-smoke.json').read_text())
assert smoke['architecture'] == 'arm64' and smoke['budget_guard'] and smoke['health']
assert (LOG / 'arm64-smoke.exit').read_text().strip() == '0'
inputs = json.loads((ART / 'arm64-inputs.json').read_text())
base_raw = (WORK / 'scripts/arm64-base-manifest.json').read_bytes()
assert 'sha256:' + hashlib.sha256(base_raw).hexdigest() == inputs['arm64_base'].split('@')[1]
base = json.loads(base_raw)
bundle = ART / 'publication'
blobs = bundle / 'blobs/sha256'
blobs.mkdir(parents=True, exist_ok=True)


def write_blob(data):
    digest = hashlib.sha256(data).hexdigest()
    (blobs / digest).write_bytes(data)
    return 'sha256:' + digest


with tarfile.open(ART / 'open-webui-arm64.docker.tar') as archive:
    saved = json.load(archive.extractfile('manifest.json'))[0]
    config_raw = archive.extractfile(saved['Config']).read()
    config = json.loads(config_raw)
    assert config['architecture'] == 'arm64'
    assert config['config']['Labels']['org.opencontainers.image.revision'] == inputs['commit']
    environment = dict(item.split('=', 1) for item in config['config']['Env'])
    assert not any(environment.get(key) for key in ('WEBUI_SECRET_KEY', 'OPENAI_API_KEY', 'GITHUB_TOKEN', 'GH_TOKEN'))
    config_digest = write_blob(config_raw)
    assert len(saved['Layers']) == len(base['layers']) + 1
    descriptors = []
    for index, name in enumerate(saved['Layers']):
        size = archive.getmember(name).size
        digest = hashlib.sha256()
        target = None
        if index == len(base['layers']):
            target = (bundle / 'new-layer.tmp').open('wb')
        with archive.extractfile(name) as source:
            while block := source.read(1024 * 1024):
                digest.update(block)
                if target is not None:
                    target.write(block)
        value = 'sha256:' + digest.hexdigest()
        if target is not None:
            target.close()
            (bundle / 'new-layer.tmp').rename(blobs / digest.hexdigest())
            descriptors.append({'mediaType': base['layers'][-1]['mediaType'], 'digest': value, 'size': size})
        else:
            assert value == base['layers'][index]['digest'] and size == base['layers'][index]['size']
            descriptors.append(base['layers'][index])
        print('Verified publication blob', index + 1, 'of', len(saved['Layers']), flush=True)
    manifest = {
        'schemaVersion': 2,
        'mediaType': base['mediaType'],
        'config': {'mediaType': base['config']['mediaType'], 'digest': config_digest, 'size': len(config_raw)},
        'layers': descriptors,
    }
    raw = json.dumps(manifest, separators=(',', ':')).encode()
    digest = write_blob(raw)
    platform = {'os': config['os'], 'architecture': config['architecture']}
    if config.get('variant'):
        platform['variant'] = config['variant']
    index = {'schemaVersion': 2, 'mediaType': 'application/vnd.oci.image.index.v1+json', 'manifests': [
        {'mediaType': manifest['mediaType'], 'digest': digest, 'size': len(raw), 'platform': platform}
    ]}
    (bundle / 'oci-layout').write_text('{"imageLayoutVersion":"1.0.0"}')
    (bundle / 'index.json').write_text(json.dumps(index, separators=(',', ':')))
    record = {
        'image': 'ghcr.io/yushi-xing/open-webui:git-' + inputs['commit'][:12] + '-arm64',
        'manifest_digest': digest,
        'config_digest': config_digest,
        'source_commit': inputs['commit'],
        'platform': platform,
        'base_image': inputs['arm64_base'],
        'base_layers_verified_identical': True,
        'application_layer_digest': descriptors[-1]['digest'],
        'application_layer_bytes': descriptors[-1]['size'],
        'complete_docker_archive': 'open-webui-arm64.docker.tar',
        'complete_archive_sha256': (ART / 'image.sha256').read_text().split()[0],
        'bundle_requires_base_blobs': True,
        'verification': smoke,
    }
    (bundle / 'registry-record.json').write_text(json.dumps(record, indent=2))
    (ART / 'registry-record.json').write_text(json.dumps(record, indent=2))
    with tarfile.open(ART / 'arm64-publication.tar.gz', 'w:gz') as output:
        for path in sorted(bundle.rglob('*')):
            if path.is_file():
                output.add(path, arcname=str(path.relative_to(bundle)), recursive=False)
with tarfile.open(ART / 'arm64-metadata.tar.gz', 'w:gz') as output:
    for path in sorted(LOG.rglob('*')):
        if path.is_file():
            output.add(path, arcname='logs/' + path.name, recursive=False)
    for path in sorted(ART.iterdir()):
        if path.is_file() and path.suffix in ('.json', '.sha256', '.css'):
            output.add(path, arcname='results/' + path.name, recursive=False)
print(json.dumps(record), flush=True)
