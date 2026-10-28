#!/usr/bin/env bash
# Install only verified prebuilt tools into a project-owned directory.
set -euo pipefail
tools_dir=${1:-/work/cache/arm64-tools}
mkdir -p "$tools_dir"
python - "$tools_dir" <<'PY'
import hashlib, io, json, subprocess, sys, tarfile, urllib.request
from pathlib import Path

root = Path(sys.argv[1])
packages = [
    ('crane.tar.gz', 'https://github.com/google/go-containerregistry/releases/download/v0.22.1/go-containerregistry_Linux_x86_64.tar.gz', '0ab7a1d6932a213aed964ce97666c3077fe691c8606413674a8b3e0b9ec4cda0'),
    ('qemu-user.deb', 'https://deb.debian.org/debian/pool/main/q/qemu/qemu-user_10.0.13+ds-0+deb13u1_amd64.deb', 'ca6ede739327a20ae5a3498230c8a3a9a072c586e131bc6bcc1201eb1725e336'),
]
for name, url, expected in packages:
    path = root / name
    if not path.exists() or hashlib.sha256(path.read_bytes()).hexdigest() != expected:
        with urllib.request.urlopen(url, timeout=180) as response:
            data = response.read()
        assert hashlib.sha256(data).hexdigest() == expected, name
        path.write_bytes(data)
    print('Verified tool package:', name, flush=True)
with tarfile.open(root / 'crane.tar.gz') as archive:
    member = archive.getmember('crane')
    assert member.isfile()
    (root / 'crane').write_bytes(archive.extractfile(member).read())
process = subprocess.Popen(['dpkg-deb', '--fsys-tarfile', str(root / 'qemu-user.deb')], stdout=subprocess.PIPE)
found = False
with tarfile.open(fileobj=process.stdout, mode='r|') as archive:
    for member in archive:
        if member.name.lstrip('./') == 'usr/bin/qemu-aarch64':
            assert member.isfile()
            (root / 'qemu-aarch64').write_bytes(archive.extractfile(member).read())
            found = True
assert process.wait() == 0 and found
for name in ('crane', 'qemu-aarch64'):
    (root / name).chmod(0o755)
    subprocess.run([str(root / name), 'version' if name == 'crane' else '--version'], check=True)
(root / 'tools.json').write_text(json.dumps({name: digest for name, _, digest in packages}, indent=2))
PY
