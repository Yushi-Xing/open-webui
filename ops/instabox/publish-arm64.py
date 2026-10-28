"""Control-side publication of builder-produced bytes; no image build occurs here.

Default mode verifies the transfer bundle only. Use --publish after configuring
a task token locally. Never upload this token or the temporary login directory.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import tempfile


def digest(path):
    value = hashlib.sha256()
    with path.open('rb') as stream:
        while block := stream.read(1024 * 1024):
            value.update(block)
    return 'sha256:' + value.hexdigest()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('bundle', type=Path, help='Extracted arm64 publication directory')
    parser.add_argument('--publish', action='store_true')
    parser.add_argument('--crane', default='/tmp/owui-registry-tools/crane')
    parser.add_argument('--token-file', type=Path, default=Path('/tmp/open-webui-ghcr-token'))
    args = parser.parse_args()
    bundle = args.bundle.resolve()
    record = json.loads((bundle / 'registry-record.json').read_text())
    assert record['image'] == 'ghcr.io/yushi-xing/open-webui:git-' + record['source_commit'][:12] + '-arm64'
    assert record['platform']['architecture'] == 'arm64'
    assert record['verification']['backend_regressions'] == 31
    assert all(record['verification'][key] for key in ('health', 'version', 'original_css', 'budget_guard', 'all_layer_diff_ids_verified'))
    blobs = bundle / 'blobs/sha256'
    for path in blobs.iterdir():
        assert path.is_file() and not path.is_symlink() and digest(path) == 'sha256:' + path.name
    manifest = json.loads((blobs / record['manifest_digest'].split(':')[1]).read_text())
    transfer_index = json.loads((bundle / 'index.json').read_text())
    assert len(transfer_index['manifests']) == 1
    assert transfer_index['manifests'][0]['digest'] == record['manifest_digest']
    assert transfer_index['manifests'][0]['platform'] == record['platform']
    config = json.loads((blobs / record['config_digest'].split(':')[1]).read_text())
    assert config['architecture'] == 'arm64' and config['os'] == 'linux'
    assert config['config']['Labels']['org.opencontainers.image.revision'] == record['source_commit']
    assert manifest['config']['digest'] == record['config_digest']
    assert manifest['layers'][-1]['digest'] == record['application_layer_digest']
    assert digest(blobs / record['application_layer_digest'].split(':')[1]) == record['application_layer_digest']
    print('Verified builder manifest/config/application blob and ARM64 smoke evidence', flush=True)
    if not args.publish:
        return
    token = args.token_file
    if not token.is_file() or token.is_symlink() or stat.S_IMODE(token.stat().st_mode) & 0o077:
        raise SystemExit('Configure a regular mode-600 task token file on the control machine')
    secret = token.read_bytes().strip()
    assert secret and len(secret) <= 4096 and b'\n' not in secret
    image = record['image']
    repository = image.rsplit(':', 1)[0]
    prefix = 'git-' + record['source_commit'][:12]
    amd64 = repository + ':' + prefix + '-amd64'
    combined = repository + ':' + prefix
    amd_digest = 'sha256:34602629b2e34fdb82055e8486b36168dc8ff3af9c10ded0a3dac27673fd4cfc'
    output = bundle.parent
    log_path = output / 'ghcr-arm64-publish.log'
    with tempfile.TemporaryDirectory(prefix='owui-arm64-publish-') as temporary:
        temporary = Path(temporary)
        auth = temporary / 'auth'
        auth.mkdir(mode=0o700)
        env = {**os.environ, 'DOCKER_CONFIG': str(auth)}

        def read(*command, required=True, anonymous=False):
            active_env = env
            if anonymous:
                home = temporary / 'anonymous'
                home.mkdir(exist_ok=True)
                active_env = {**os.environ, 'HOME': str(home), 'DOCKER_CONFIG': str(home)}
            result = subprocess.run([args.crane, *command], env=active_env, capture_output=True, text=True, timeout=120)
            if result.returncode and required:
                raise RuntimeError('Registry metadata operation failed: ' + command[0])
            return result.stdout.strip() if result.returncode == 0 else None

        def logged(*command):
            with log_path.open('a') as log:
                process = subprocess.Popen([args.crane, *command], env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
                for line in process.stdout:
                    line = re.sub(r'(?i)((?:token|_state|sig|signature|x-amz-signature)=)[^&\s]+', r'\1[redacted]', line)
                    log.write(line)
                    log.flush()
                return process.wait()

        login = subprocess.run([args.crane, 'auth', 'login', 'ghcr.io', '-u', 'Yushi-Xing', '--password-stdin'], env=env, input=secret + b'\n', capture_output=True, timeout=60)
        secret = b''
        if login.returncode:
            raise SystemExit('GHCR authentication failed; credential was not printed')
        assert read('digest', amd64) == amd_digest
        amd_config = json.loads(read('config', amd64))
        assert amd_config['architecture'] == 'amd64' and amd_config['config']['Labels']['org.opencontainers.image.revision'] == record['source_commit']
        existing = read('digest', image, required=False)
        if existing and existing != record['manifest_digest']:
            raise RuntimeError('Refusing to overwrite a different immutable arm64 tag')
        if not existing:
            # Copying existing base content is publication, not a local rebuild.
            # Registry clients can mount shared layers across repositories.
            base_digest = record['base_image'].split('@')[1]
            print('Making verified upstream base blobs available in the destination repository', flush=True)
            if logged('copy', record['base_image'], repository + '@' + base_digest):
                raise RuntimeError('Base blob copy failed; see sanitized publication log')
            final = temporary / 'image'
            shutil.copytree(bundle, final)
            print('Publishing builder-produced ARM64 manifest and application layer', flush=True)
            if logged('push', str(final), image):
                text = log_path.read_text()
                if 'no such file or directory' not in text:
                    raise RuntimeError('Image publication failed; see sanitized publication log')
                # Some client versions require local base blobs even when the
                # registry already has them. Fetch exact existing bytes only;
                # never synthesize or modify the builder manifest/config.
                base = temporary / 'base'
                if logged('pull', '--format', 'oci', record['base_image'], str(base)):
                    raise RuntimeError('Could not fetch verified base blobs')
                for descriptor in manifest['layers'][:-1]:
                    name = descriptor['digest'].split(':')[1]
                    source = base / 'blobs/sha256' / name
                    assert digest(source) == descriptor['digest']
                    target = final / 'blobs/sha256' / name
                    if not target.exists():
                        os.link(source, target)
                if logged('push', str(final), image):
                    raise RuntimeError('Image publication failed after base materialization')
        assert read('digest', image) == record['manifest_digest']
        assert json.loads(read('config', image)) == config
        if logged('validate', '--fast', '--remote', image):
            raise RuntimeError('Published layer presence validation failed')
        expected = {'amd64': amd_digest, 'arm64': record['manifest_digest']}
        existing_index = read('manifest', combined, required=False)
        if existing_index:
            index = json.loads(existing_index)
            assert {item['platform']['architecture']: item['digest'] for item in index['manifests']} == expected, 'Refusing to replace a different immutable multiarch tag'
        elif logged('index', 'append', '-m', amd64, '-m', image, '-t', combined):
            raise RuntimeError('Multiarch index publication failed')
        index = json.loads(read('manifest', combined))
        assert {item['platform']['architecture']: item['digest'] for item in index['manifests']} == expected
        for architecture, expected_digest in expected.items():
            assert read('digest', '--platform', 'linux/' + architecture, combined, anonymous=True) == expected_digest
        result = {**record, 'published': True, 'anonymous_platform_digests_verified': True, 'multiarch_image': combined, 'multiarch_digest': read('digest', combined), 'amd64_image': amd64, 'amd64_digest': amd_digest, 'custom_tag_modified': False, 'temporary_credentials_removed': True}
        (output / 'ghcr-arm64-publish-result.json').write_text(json.dumps(result, indent=2) + '\n')
    token.unlink()
    print(json.dumps({'image': image, 'digest': record['manifest_digest'], 'multiarch_image': result['multiarch_image'], 'multiarch_digest': result['multiarch_digest'], 'temporary_credentials_removed': True}), flush=True)


if __name__ == '__main__':
    main()
