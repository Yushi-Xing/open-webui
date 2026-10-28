"""Validate and run the saved arm64 image with explicit userspace QEMU.

Runs inside the existing project container as root. No host binfmt registration,
privileged container, Docker socket or host filesystem mount is required.
"""
import gzip
import hashlib
import http.client
import json
import os
import resource
from pathlib import Path, PurePosixPath
import shutil
import signal
import stat
import struct
import subprocess
import sys
import tarfile
import time

WORK = Path('/work')
RUN = sys.argv[1]
assert Path(RUN).name == RUN and RUN not in ('.', '..')
ART = WORK / 'artifacts' / RUN
LOG = WORK / 'logs' / RUN
ROOT = WORK / 'cache' / ('arm64-rootfs-' + RUN)
ARCHIVE = ART / 'open-webui-arm64.docker.tar'
resource.setrlimit(resource.RLIMIT_CORE, (0, 0))


def safe_parts(name):
    path = PurePosixPath(name)
    if path.is_absolute() or '..' in path.parts:
        raise ValueError('Unsafe image member path')
    return path.parts


def guest(name, follow_final=False):
    queue = list(PurePosixPath('/' + name.lstrip('/')).parts[1:])
    resolved = []
    hops = 0
    while queue:
        item = queue.pop(0)
        if item == '..':
            if resolved:
                resolved.pop()
            continue
        if item in ('', '.'):
            continue
        path = ROOT.joinpath(*resolved, item)
        if path.is_symlink() and (queue or follow_final):
            hops += 1
            if hops > 40:
                raise ValueError('Image symlink loop')
            link = PurePosixPath(os.readlink(path))
            if link.is_absolute():
                resolved = []
                queue = list(link.parts[1:]) + queue
            else:
                queue = list(link.parts) + queue
        else:
            resolved.append(item)
    return ROOT.joinpath(*resolved)


def remove(path):
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.is_dir():
        shutil.rmtree(path)


class HashReader:
    def __init__(self, stream):
        self.stream = stream
        self.digest = hashlib.sha256()
        self.size = 0

    def read(self, size=-1):
        data = self.stream.read(size)
        self.digest.update(data)
        self.size += len(data)
        return data


def stream_layer(archive, name):
    source = archive.extractfile(name)
    magic = source.read(2)
    source.seek(0)
    return gzip.GzipFile(fileobj=source) if magic == b'\x1f\x8b' else source


def extract(archive, manifest, config):
    if ROOT.exists():
        raise RuntimeError('Refusing to overwrite an existing smoke rootfs')
    ROOT.mkdir()
    for index, name in enumerate(manifest['Layers']):
        checked = HashReader(stream_layer(archive, name))
        whiteouts = []
        with tarfile.open(fileobj=checked, mode='r|') as layer:
            for member in layer:
                parts = safe_parts(member.name)
                if parts and parts[-1].startswith('.wh.'):
                    whiteouts.append(parts)
        while checked.read(1024 * 1024):
            pass
        assert 'sha256:' + checked.digest.hexdigest() == config['rootfs']['diff_ids'][index]
        if shutil.disk_usage(ROOT).free < checked.size + 2 * 1024**3:
            raise RuntimeError('Insufficient /data space for next unpacked layer plus 2 GiB reserve')
        for parts in whiteouts:
            parent = guest('/'.join(parts[:-1]), follow_final=True)
            if parts[-1] == '.wh..wh..opq':
                if parent.is_dir():
                    for child in parent.iterdir():
                        remove(child)
            else:
                remove(parent / parts[-1][4:])
        hardlinks = []
        with tarfile.open(fileobj=stream_layer(archive, name), mode='r|') as layer:
            for member in layer:
                parts = safe_parts(member.name)
                if not parts or parts[-1].startswith('.wh.'):
                    continue
                path = guest('/'.join(parts))
                path.parent.mkdir(parents=True, exist_ok=True)
                if member.isdir():
                    if path.is_symlink() or (path.exists() and not path.is_dir()):
                        remove(path)
                    path.mkdir(exist_ok=True)
                    path.chmod(member.mode)
                elif member.issym():
                    remove(path)
                    path.symlink_to(member.linkname)
                elif member.islnk():
                    safe_parts(member.linkname.lstrip('/'))
                    hardlinks.append((path, member.linkname))
                elif member.isfile():
                    if path.is_symlink() or path.is_dir():
                        remove(path)
                    with layer.extractfile(member) as src, path.open('wb') as out:
                        shutil.copyfileobj(src, out, 1024 * 1024)
                    path.chmod(member.mode)
                # Device nodes are created explicitly for this smoke below.
        for path, name in hardlinks:
            source = guest(name, follow_final=True)
            if path != source:
                remove(path)
                os.link(source, path)
        print(f'Validated and unpacked arm64 layer {index + 1}/{len(manifest["Layers"])}', flush=True)
    (ART / 'arm64-rootfs-complete.json').write_text(json.dumps({'all_diff_ids_verified': True}))


def enter():
    os.chroot(ROOT)
    os.chdir('/app/backend')


def main():
    with tarfile.open(ARCHIVE) as archive:
        manifest = json.load(archive.extractfile('manifest.json'))[0]
        config = json.load(archive.extractfile(manifest['Config']))
        assert config['architecture'] == 'arm64' and config['os'] == 'linux'
        if '--reuse-rootfs' in sys.argv:
            assert json.loads((ART / 'arm64-rootfs-complete.json').read_text())['all_diff_ids_verified']
        else:
            extract(archive, manifest, config)
    elf = guest('usr/local/bin/python3.12', follow_final=True).read_bytes()[:20]
    assert elf[:4] == b'\x7fELF' and struct.unpack('<H', elf[18:20])[0] == 183
    environment = dict(item.split('=', 1) for item in config['config']['Env'])
    assert not any(environment.get(k) for k in ('WEBUI_SECRET_KEY', 'OPENAI_API_KEY', 'GITHUB_TOKEN', 'GH_TOKEN'))
    environment.update(WEBUI_SECRET_KEY='ci-smoke-only-not-for-deployment', ENABLE_OLLAMA_API='false',
                       ENABLE_CONTEXT_COMPACTION='true', CONTEXT_COMPACTION_TOKEN_THRESHOLD='80000',
                       CONTEXT_COMPACTION_TOKEN_CAP='80000', OFFLINE_MODE='true', HF_HUB_OFFLINE='1')
    for name in ('cpuinfo', 'meminfo', 'stat', 'uptime'):
        path = guest('proc/' + name)
        path.parent.mkdir(exist_ok=True)
        shutil.copyfile('/proc/' + name, path)
    for name in ('resolv.conf', 'hosts'):
        path = guest('etc/' + name)
        if path.is_symlink():
            path.unlink()
        shutil.copyfile('/etc/' + name, path)
    for name, minor in {'null': 3, 'zero': 5, 'random': 8, 'urandom': 9}.items():
        path = guest('dev/' + name)
        path.parent.mkdir(exist_ok=True)
        if not path.exists():
            os.mknod(path, stat.S_IFCHR | 0o666, os.makedev(1, minor))
    qemu = guest('usr/local/bin/instabox-qemu-aarch64')
    shutil.copyfile(WORK / 'cache/arm64-tools/qemu-aarch64', qemu)
    qemu.chmod(0o755)
    guest('work').mkdir(exist_ok=True)
    shutil.copyfile(WORK / 'scripts/runtime-smoke.py', guest('work/runtime-smoke.py'))
    guest('work/tests/fork').mkdir(parents=True, exist_ok=True)
    for path in (WORK / 'src/tests/fork').glob('test_context_*.py'):
        shutil.copyfile(path, guest('work/tests/fork/' + path.name))
    backend = guest('work/backend')
    if not backend.exists() and not backend.is_symlink():
        backend.symlink_to('/app/backend')
    command = ['/usr/local/bin/instabox-qemu-aarch64', '-cpu', 'max', '/usr/local/bin/python3']
    with (LOG / 'arm64-tests.log').open('wb') as log:
        subprocess.run(command + ['-c', 'import platform,tiktoken,torch; assert platform.machine()=="aarch64"; print("Actual ARM64 Python, tiktoken and torch:",platform.machine(),torch.__version__,flush=True)'], env=environment, preexec_fn=enter, check=True, timeout=300, stdout=log, stderr=subprocess.STDOUT)
        subprocess.run(command + ['-m', 'unittest', 'discover', '-s', '/work/tests/fork', '-p', 'test_context_*.py', '-v'], env=environment, preexec_fn=enter, check=True, timeout=600, stdout=log, stderr=subprocess.STDOUT)
    with (LOG / 'arm64-runtime.log').open('wb') as log:
        server = subprocess.Popen(command + ['-m', 'uvicorn', 'open_webui.main:app', '--host', '127.0.0.1', '--port', '8080', '--workers', '1'], env=environment, preexec_fn=enter, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
        try:
            for attempt in range(450):
                if server.poll() is not None:
                    raise RuntimeError(f'ARM64 server exited: {server.returncode}')
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
                raise RuntimeError('ARM64 server health timeout')
            for endpoint, name in [('/api/version', 'version.json'), ('/static/custom.css', 'served-custom.css')]:
                connection = http.client.HTTPConnection('127.0.0.1', 8080, timeout=5)
                connection.request('GET', endpoint)
                response = connection.getresponse()
                assert response.status == 200
                (ART / name).write_bytes(response.read())
                connection.close()
            assert json.loads((ART / 'version.json').read_text())['version'] == '0.12.4'
            assert (ART / 'served-custom.css').read_bytes() == (WORK / 'src/static/static/custom.css').read_bytes()
            with (LOG / 'arm64-budget.log').open('wb') as budget:
                subprocess.run(command + ['/work/runtime-smoke.py'], env=environment, preexec_fn=enter, check=True, timeout=600, stdout=budget, stderr=subprocess.STDOUT)
            (ART / 'arm64-smoke.json').write_text(json.dumps({'architecture': 'arm64', 'elf_machine': 183, 'qemu_cpu': 'max', 'all_layer_diff_ids_verified': True, 'backend_regressions': 31, 'health': True, 'version': True, 'original_css': True, 'budget_guard': True, 'native_arm64_hardware_tested': False, 'docker_engine_start_tested': False}, indent=2))
            print('PASS: actual arm64 binaries, tokenizer/torch, regressions, startup/HTTP/CSS and context guard under QEMU', flush=True)
        finally:
            try:
                os.killpg(server.pid, signal.SIGTERM)
                server.wait(timeout=30)
            except ProcessLookupError:
                pass
            except subprocess.TimeoutExpired:
                os.killpg(server.pid, signal.SIGKILL)
                server.wait()


if __name__ == '__main__':
    main()
