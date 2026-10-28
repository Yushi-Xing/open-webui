"""Validate a Docker archive and exercise its exact rootfs inside the build container.

Runs as container root, with only project mounts and default Docker capabilities.
No host mount, Docker socket, registry credential or privileged container is used.
"""
import asyncio
import hashlib
import http.client
import io
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import stat
import sys
import subprocess
import tarfile
import time

PROJECT = Path('/work')
RUN = next((arg for arg in sys.argv[1:] if not arg.startswith('--')), '20261028-instabox-01')
if Path(RUN).name != RUN or RUN in ('.', '..'):
    raise ValueError('Invalid run ID')
ART = PROJECT / 'artifacts' / RUN
LOG = PROJECT / 'logs' / RUN
ROOT = PROJECT / 'cache' / 'image-rootfs'
ARCHIVE = ART / 'open-webui-amd64.docker.tar.gz'


def parts(name):
    path = PurePosixPath(name)
    if path.is_absolute() or '..' in path.parts:
        raise ValueError('Unsafe archive entry path')
    return list(path.parts)


def guest(name, follow_final=False):
    """Resolve image symlinks as chroot paths, never as host absolute paths."""
    queue = list(PurePosixPath('/' + name.lstrip('/')).parts[1:])
    resolved = []
    hops = 0
    while queue:
        part = queue.pop(0)
        if part == '..':
            if resolved:
                resolved.pop()
            continue
        if part in ('', '.'):
            continue
        candidate = ROOT.joinpath(*resolved, part)
        if candidate.is_symlink() and (queue or follow_final):
            hops += 1
            if hops > 40:
                raise ValueError('Image symlink loop')
            link = PurePosixPath(os.readlink(candidate))
            if link.is_absolute():
                resolved = []
                queue = list(link.parts[1:]) + queue
            else:
                queue = list(link.parts) + queue
        else:
            resolved.append(part)
    return ROOT.joinpath(*resolved)


def remove(path):
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.is_dir():
        shutil.rmtree(path)


def first_layer(stream):
    # Bootstrap only regular objects before creating symlinks. Subsequent
    # extraction occurs inside chroot, so absolute image symlinks stay confined.
    links = []
    with tarfile.open(fileobj=stream, mode='r|*') as layer:
        for member in layer:
            name = parts(member.name)
            if not name:
                continue
            target = ROOT.joinpath(*name)
            if member.issym() or member.islnk():
                links.append(member)
                continue
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
            elif member.isfile():
                target.parent.mkdir(parents=True, exist_ok=True)
                with layer.extractfile(member) as src, target.open('wb') as out:
                    shutil.copyfileobj(src, out, 1024 * 1024)
                target.chmod(member.mode)
    for member in links:
        if member.islnk():
            target = ROOT.joinpath(*parts(member.name))
            source = ROOT.joinpath(*parts(member.linkname.lstrip('/')))
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists() or target.is_symlink():
                remove(target)
            os.link(source, target)
    for member in sorted(links, key=lambda m: len(parts(m.name)), reverse=True):
        if member.issym():
            target = ROOT.joinpath(*parts(member.name))
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists() or target.is_symlink():
                remove(target)
            target.symlink_to(member.linkname)


def extract(archive):
    if ROOT.exists():
        raise RuntimeError('Refusing to overwrite an existing image test root')
    ROOT.mkdir()
    manifests = json.load(archive.extractfile('manifest.json'))
    entry = manifests[0]
    config = json.load(archive.extractfile(entry['Config']))
    (ART / 'image-config-checked.json').write_text(json.dumps({
        'architecture': config['architecture'],
        'source_commit': config['config'].get('Labels', {}).get('org.opencontainers.image.revision'),
        'layers': len(entry['Layers']),
        'command': config['config'].get('Cmd'),
    }, indent=2))
    assert config['architecture'] == 'amd64'
    expected = json.loads((PROJECT / 'src/SOURCE.json').read_text())['commit']
    assert config['config']['Labels']['org.opencontainers.image.revision'] == expected
    # No credentials are expected in image config. Do not print values.
    environment = dict(item.split('=', 1) for item in config['config'].get('Env', []))
    assert not any(environment.get(k) for k in ('WEBUI_SECRET_KEY', 'OPENAI_API_KEY', 'GH_TOKEN', 'GITHUB_TOKEN', 'AWS_SECRET_ACCESS_KEY'))
    for index, name in enumerate(entry['Layers']):
        # Hash the actual uncompressed tar, as required by Docker diff_ids.
        member = archive.extractfile(name)
        magic = member.read(2)
        member.seek(0)
        import gzip
        stream = gzip.GzipFile(fileobj=member) if magic == b'\x1f\x8b' else member
        digest = hashlib.sha256()
        while block := stream.read(1024 * 1024):
            digest.update(block)
        assert 'sha256:' + digest.hexdigest() == config['rootfs']['diff_ids'][index]
        member = archive.extractfile(name)
        if index == 0:
            first_layer(member)
        else:
            whiteouts = []
            with tarfile.open(fileobj=member, mode='r|*') as layer:
                for item in layer:
                    item_parts = parts(item.name)
                    if item_parts and item_parts[-1].startswith('.wh.'):
                        whiteouts.append(item_parts)
            for item_parts in whiteouts:
                parent = guest('/'.join(item_parts[:-1]), follow_final=True)
                if item_parts[-1] == '.wh..wh..opq':
                    if parent.is_dir():
                        for child in parent.iterdir():
                            remove(child)
                else:
                    remove(parent / item_parts[-1][4:])
            member = archive.extractfile(name)
            process = subprocess.Popen(['chroot', str(ROOT), '/bin/tar', '-xzpf' if magic == b'\x1f\x8b' else '-xpf', '-'], stdin=subprocess.PIPE)
            try:
                shutil.copyfileobj(member, process.stdin, 1024 * 1024)
            finally:
                process.stdin.close()
            if process.wait() != 0:
                raise RuntimeError('Layer extraction failed')
            for item_parts in whiteouts:
                remove(guest('/'.join(item_parts)))
        print(f'Validated and extracted image layer {index + 1}/{len(entry["Layers"])}', flush=True)
    return config


def main():
    with tarfile.open(ARCHIVE, mode='r:gz') as archive:
        if '--reuse-rootfs' in sys.argv:
            entry = json.load(archive.extractfile('manifest.json'))[0]
            config = json.load(archive.extractfile(entry['Config']))
        else:
            config = extract(archive)
    for name in ('null', 'zero', 'random', 'urandom'):
        target = guest('dev/' + name)
        target.parent.mkdir(exist_ok=True)
        minor = {'null': 3, 'zero': 5, 'random': 8, 'urandom': 9}[name]
        if target.exists():
            continue
        os.mknod(target, stat.S_IFCHR | 0o666, os.makedev(1, minor))
    for name in ('cpuinfo', 'meminfo', 'stat', 'uptime'):
        target = guest('proc/' + name)
        target.parent.mkdir(exist_ok=True)
        shutil.copyfile('/proc/' + name, target)
    for name in ('resolv.conf', 'hosts'):
        target = guest('etc/' + name)
        if target.is_symlink():
            target.unlink()
        shutil.copyfile('/etc/' + name, target)
    shutil.copyfile(PROJECT / 'scripts/runtime-smoke.py', guest('work/runtime-smoke.py'))
    environment = dict(item.split('=', 1) for item in config['config'].get('Env', []))
    environment.update(WEBUI_SECRET_KEY='ci-smoke-only-not-for-deployment', ENABLE_OLLAMA_API='false',
                       ENABLE_CONTEXT_COMPACTION='true', CONTEXT_COMPACTION_TOKEN_THRESHOLD='80000',
                       CONTEXT_COMPACTION_TOKEN_CAP='80000', PORT='8080')
    def enter():
        os.chroot(ROOT)
        os.chdir(config['config'].get('WorkingDir') or '/')
    with (LOG / 'image-rootfs-runtime.log').open('wb') as log:
        server = subprocess.Popen(config['config']['Cmd'], env=environment, preexec_fn=enter, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
        try:
            for attempt in range(180):
                if server.poll() is not None:
                    raise RuntimeError(f'Image server exited with {server.returncode}')
                try:
                    connection = http.client.HTTPConnection('127.0.0.1', 8080, timeout=3)
                    connection.request('GET', '/health')
                    response = connection.getresponse()
                    data = response.read()
                    connection.close()
                    if response.status == 200 and json.loads(data).get('status') is True:
                        (ART / 'health.json').write_bytes(data)
                        break
                except (OSError, ValueError):
                    pass
                time.sleep(2)
            else:
                raise RuntimeError('Image runtime did not become healthy')
            for endpoint, name in [('/api/version', 'version.json'), ('/static/custom.css', 'served-custom.css')]:
                connection = http.client.HTTPConnection('127.0.0.1', 8080, timeout=5)
                connection.request('GET', endpoint)
                response = connection.getresponse()
                assert response.status == 200
                (ART / name).write_bytes(response.read())
                connection.close()
            assert json.loads((ART / 'version.json').read_text())['version'] == '0.12.4'
            assert (ART / 'served-custom.css').read_bytes() == (PROJECT / 'src/static/static/custom.css').read_bytes()
            subprocess.run(['/work/cache/runtime-venv/bin/python', '/work/runtime-smoke.py'], env=environment, preexec_fn=enter, check=True, timeout=300)
            (ART / 'image-rootfs-smoke.json').write_text(json.dumps({'health':True,'version':True,'original_css':True,'budget_guard':True,'all_layer_diff_ids_verified':True,'method':'image rootfs chroot inside build container; Docker engine load not verified'},indent=2))
            print('PASS: image rootfs startup, health, version, CSS and context guard', flush=True)
        finally:
            import signal
            try:
                os.killpg(server.pid, signal.SIGTERM)
                server.wait(timeout=30)
            except ProcessLookupError:
                pass
            except subprocess.TimeoutExpired:
                os.killpg(server.pid, signal.SIGKILL)
                server.wait()


main()
