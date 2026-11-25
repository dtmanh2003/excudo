"""Launch the Ink terminal UI using this environment's Python core."""
import argparse
import os
from pathlib import Path
import shutil
import subprocess
import sys


def main():
    parser = argparse.ArgumentParser(description='Excudo React / Ink CLI')
    parser.add_argument('--workspace', default=os.getenv('EXCUDO_WORKSPACE_ROOT', str(Path.cwd())), help='Workspace root')
    args = parser.parse_args()
    node = shutil.which('node')
    root = Path(__file__).resolve().parent.parent
    if not Path(args.workspace).is_dir():
        parser.error('Workspace must be an existing directory')
    if node is None or not (root / 'cli' / 'node_modules' / 'ink').exists():
        parser.exit(1, 'Ink CLI requires Node.js >=22 and frontend dependencies.\n'
                    'Run: pnpm --dir cli install --frozen-lockfile\n'
                    'Existing CLI: python main.py\n')
    command = [node, str(root / 'cli' / 'src' / 'index.js'),
               '--python', sys.executable, '--workspace', str(Path(args.workspace).resolve())]
    try:
        result = subprocess.call(command)
    except KeyboardInterrupt:
        # Ink handles raw-mode Ctrl+C; this covers interruption before raw mode starts.
        result = 130
    raise SystemExit(result)


if __name__ == '__main__':
    main()
