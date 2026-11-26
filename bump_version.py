"""Advance VAOS's patch version once per completed change."""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def set_version(version: str) -> None:
    if not re.fullmatch(r"(?:1\.[23]\.\d+(?:-beta\.?\d+)?)", version):
        raise ValueError("Versions must follow an authorized 1.2.x/1.3.x stable or beta version.")
    metadata = json.loads((ROOT / 'VAOS_PROJECT_METADATA.json').read_text())
    previous = metadata['version']
    # Keep current version consumers synchronized; historical release notes stay intact.
    for name in ('VAOS_PROJECT_METADATA.json', 'project_metadata.py', 'pyproject.toml',
                 'CITATION.cff', 'main.py', 'vaos_cli.py', 'observability_siem.py',
                 'README_FIRST.txt', 'docs/README.md', 'docs/KNOWLEDGEBASE.md',
                 'tests/fixtures/README.md'):
        path = ROOT / name
        data = path.read_bytes()
        data = data.replace(previous.encode(), version.encode())
        if name == 'CITATION.cff':
            data = re.sub(rb'(?m)^cff-version: .*$', b'cff-version: 1.2.0', data)
        path.write_bytes(data)
    path = ROOT / 'README.md'
    data = path.read_bytes()
    data = re.sub(rb'\A# VAOS[^\r\n]*', f'# VAOS {version}'.encode(), data)
    path.write_bytes(data)
    (ROOT / 'static' / 'version.json').write_text(json.dumps({'version': version}) + '\n')
    print(f'VAOS {previous} -> {version}')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--set', dest='version', help='Set an explicitly authorized VAOS version.')
    args = parser.parse_args()
    current = json.loads((ROOT / 'VAOS_PROJECT_METADATA.json').read_text())['version']
    if args.version:
        next_version = args.version
    else:
        if re.fullmatch(r'1\.3\.\d+-beta\.?\d+', current):
            next_version = re.sub(r'(beta\.?)(\d+)$', lambda m:m[1]+str(int(m[2])+1), current)
        elif re.fullmatch(r'1\.[23]\.\d+', current):
            major,minor,patch=current.split('.')
            next_version = f'{major}.{minor}.{int(patch)+1}'
        else:
            parser.error('Initialize an authorized baseline using --set first.')
    set_version(next_version)
