"""Load the existing local launcher settings before importing either frontend."""
import argparse
import json
import os
from pathlib import Path
import runpy
import sys


def load_settings(root):
    path = Path(root) / '.excudo' / 'launcher-env.json'
    if not path.exists():
        return
    if path.is_symlink() or path.is_junction() or path.stat().st_size > 65536:
        raise ValueError('Launcher settings must be a small regular local JSON file.')
    settings = json.loads(path.read_text(encoding='utf-8-sig'))
    if not isinstance(settings, dict):
        raise ValueError('Launcher settings must be a JSON object.')
    for key, value in settings.items():
        if isinstance(key,str) and key.startswith('EXCUDO_') and isinstance(value,(str,int,float,bool)):
            os.environ[key] = str(value)


def main():
    parser=argparse.ArgumentParser(description='Excudo Studio / React Ink launcher')
    parser.add_argument('--app', choices=['studio','ink'],default='studio')
    args, forwarded=parser.parse_known_args()
    root=Path(__file__).resolve().parent.parent
    try:
        load_settings(root)
    except (OSError,ValueError):
        parser.exit(1,'Local launcher settings could not be read. Check .excudo/launcher-env.json.\n')
    sys.argv=[sys.argv[0],*forwarded]
    runpy.run_module('excudo.cli_launcher' if args.app=='ink' else 'excudo.app',run_name='__main__')


if __name__=='__main__':
    main()
