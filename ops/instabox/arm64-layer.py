"""Create the application layer on the remote builder from verified inputs."""
import hashlib
import io
import json
from pathlib import Path
import sys
import tarfile

ROOT = Path('/work')
RUN = sys.argv[1]
assert Path(RUN).name == RUN and RUN not in ('.', '..')
ART = ROOT / 'artifacts' / RUN
ART.mkdir(exist_ok=True)
inputs = json.loads((ROOT / 'scripts/arm64-inputs.json').read_text())
source = json.loads((ROOT / 'src/SOURCE.json').read_text())
assert source['commit'] == inputs['commit']
epoch = inputs['source_epoch']
frontend = ROOT / 'artifacts/20261028-instabox-01/frontend.tar.gz'
assert hashlib.sha256(frontend.read_bytes()).hexdigest() == inputs['frontend_archive_sha256']
output = ART / 'application-layer.tar'
files = []


def add(archive, name, data, mode=0o644):
    entry = tarfile.TarInfo(name)
    entry.uid = entry.gid = 0
    entry.mtime = epoch
    entry.mode = mode
    entry.size = len(data)
    archive.addfile(entry, io.BytesIO(data))
    files.append({'path': name, 'bytes': len(data), 'sha256': hashlib.sha256(data).hexdigest()})


with tarfile.open(output, 'w') as layer:
    # Hide the old frontend without duplicating it in the merged rootfs.
    add(layer, 'app/build/.wh..wh..opq', b'')
    with tarfile.open(frontend) as archive:
        for member in archive:
            path = Path(member.name)
            assert not path.is_absolute() and '..' not in path.parts
            assert path.parts and path.parts[0] == 'build'
            if member.isdir():
                continue
            assert member.isfile(), member.name
            add(layer, 'app/' + member.name, archive.extractfile(member).read(), member.mode)
    for item in inputs['files']:
        path = ROOT / 'src' / item['path']
        data = path.read_bytes()
        assert len(data) == item['bytes'] and hashlib.sha256(data).hexdigest() == item['sha256'], item['path']
        add(layer, 'app/' + item['path'], data)
(ART / 'application-layer-files.json').write_text(json.dumps(files, indent=2))
(ART / 'arm64-inputs.json').write_text(json.dumps(inputs, indent=2))
print('Verified frontend and all six backend deltas; application layer:', output.stat().st_size, flush=True)
