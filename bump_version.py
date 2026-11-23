"""Advance VAOS's patch version once per completed change."""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def set_version(version: str) -> None:
    if not re.fullmatch(r"1\.2\.\d+", version):
        raise ValueError("Versions must follow 1.2.x.")
    metadata = json.loads((ROOT / 'VAOS_PROJECT_METADATA.json').read_text())
    previous = metadata['version']
    # Keep current version consumers synchronized; historical release notes stay intact.
    for name in ('VAOS_PROJECT_METADATA.json', 'project_metadata.py', 'pyproject.toml',
                 'CITATION.cff', 'main.py', 'vaos_cli.py', 'observability_siem.py'):
        path = ROOT / name
        data = path.read_bytes()
        path.write_bytes(data.replace(previous.encode(), version.encode()))
    path = ROOT / 'README.md'
    data = path.read_bytes()
    data = re.sub(rb'\A# VAOS [0-9]+\.[0-9]+\.[0-9]+', f'# VAOS {version}'.encode(), data)
    path.write_bytes(data)
    print(f'VAOS {previous} -> {version}')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--set', dest='version', help='Set an explicit 1.2.x version.')
    args = parser.parse_args()
    current = json.loads((ROOT / 'VAOS_PROJECT_METADATA.json').read_text())['version']
    if args.version:
        next_version = args.version
    else:
        if not re.fullmatch(r'1\.2\.\d+', current):
            parser.error('Initialize the 1.2.x baseline using --set first.')
        next_version = f'1.2.{int(current.split(".")[2]) + 1}'
    set_version(next_version)
