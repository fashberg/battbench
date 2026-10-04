"""Create a release: asks for the version (suggests the next one), writes it into battbench/__init__.py, commits,
tags v<version>, builds the Windows installer (build.ps1) and offers to push commit and tag.

    .venv\\Scripts\\python packaging\\release.py [--version 2026.10] [--dry-run] [--no-build]

Versions: year.month (2026.10), bug fix releases 2026.10.1, 2026.10.2 ... The working tree must be clean.
"""
import argparse
import datetime
import os
import re
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INIT = os.path.join(ROOT, 'battbench', '__init__.py')
VERSION_RE = re.compile(r"__version__ = '([^']+)'")


def git(*args):
    return subprocess.run(['git', *args], cwd=ROOT, check=True, capture_output=True, text=True).stdout.strip()


def suggest():
    """This month's version, or the next bug fix release of it if that is tagged already."""
    month = datetime.date.today().strftime('%Y.%m')
    tags = [t for t in git('tag', '--list', f'v{month}*').split() if re.fullmatch(rf'v{re.escape(month)}(\.\d+)?', t)]
    if not tags:
        return month
    return f'{month}.{max(int(t.rsplit(".", 1)[1]) if t != "v" + month else 0 for t in tags) + 1}'


def fail(text):
    print(text, file=sys.stderr)
    sys.exit(1)


def main():
    ap = argparse.ArgumentParser(description='Create a BattBench release.')
    ap.add_argument('--version', help='e.g. 2026.10 or 2026.10.1 (default: ask)')
    ap.add_argument('--dry-run', action='store_true', help='only show what would be done')
    ap.add_argument('--no-build', action='store_true', help="commit and tag, but don't build the installer")
    a = ap.parse_args()

    if git('status', '--porcelain'):
        print(git('status', '--short'))
        fail('Uncommitted changes - commit or stash them first, a release contains committed work only.')
    with open(INIT, encoding='utf-8') as f:
        init = f.read()
    current = VERSION_RE.search(init).group(1)
    version = a.version
    if not version:
        proposal = suggest()
        version = input(f'Version (current {current}, Enter = {proposal}): ').strip() or proposal
    if not re.fullmatch(r'\d{4}\.\d{2}(\.\d+)?', version):
        fail(f"Version must look like 2026.10 or 2026.10.1, not '{version}'.")
    if git('tag', '--list', f'v{version}'):
        fail(f'Tag v{version} exists already.')

    print(f'Release {version}')
    if a.dry_run:
        print(f"(dry run) would set __version__ = '{version}', commit 'Release {version}', tag v{version}"
              + ('' if a.no_build else ', run build.ps1'))
        return

    with open(INIT, 'w', encoding='utf-8', newline='') as f:
        f.write(VERSION_RE.sub(f"__version__ = '{version}'", init, count=1))
    if git('status', '--porcelain'):
        git('add', INIT)
        git('commit', '-q', '-m', f'Release {version}')
    git('tag', '-a', f'v{version}', '-m', f'BattBench {version}')
    print(f'Committed and tagged v{version}')

    if not a.no_build:
        build = os.path.join(ROOT, 'packaging', 'build.ps1')
        if subprocess.run(['powershell', '-ExecutionPolicy', 'Bypass', '-File', build], cwd=ROOT).returncode:
            fail('build.ps1 failed (commit and tag are there; fix the problem and run build.ps1 again).')
        print(f'Installer: build\\BattBench-{version}-setup.exe')

    if input('Push commit and tag to origin? [y/N] ').strip().lower() in ('y', 'yes', 'j', 'ja'):
        git('push', 'origin', 'HEAD')
        git('push', 'origin', f'v{version}')
        print('Pushed.')
    else:
        print(f'Not pushed. Later: git push origin HEAD && git push origin v{version}')


if __name__ == '__main__':
    main()
