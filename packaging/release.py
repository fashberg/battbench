"""Create a release. Work happens on the branch devel (pushed to origin only); main holds one squash commit per
release and is the same on origin and github (the public repository).

    .venv\\Scripts\\python packaging\\release.py [--version 2026.10] [--dry-run] [--build]

On a clean devel: asks for the version (suggests the next one), commits it on devel ('Release <version>'), adds one
commit on main with exactly devel's files (message: the devel commits since the last release), tags it v<version>
and offers to push devel to origin, main and the tag to origin and github. The tag on github starts the workflow
.github/workflows/release.yml: it builds the Windows installer and publishes the GitHub release (notes = the release
commit's message). --build also builds the installer here, from main (build.ps1), e.g. to try it first.
Versions: year.month (2026.10), bug fix releases 2026.10.1, 2026.10.2 ...
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
WORK, MAIN, PUBLIC = 'devel', 'main', ('origin', 'github')
TRAILER = 'Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>'


def git(*args, stdin=None):
    return subprocess.run(['git', *args], cwd=ROOT, check=True, capture_output=True, text=True,
                          input=stdin).stdout.strip()


def suggest():
    """This month's version, or the next bug fix release of it if that is tagged already."""
    month = datetime.date.today().strftime('%Y.%m')
    tags = [t for t in git('tag', '--list', f'v{month}*').split() if re.fullmatch(rf'v{re.escape(month)}(\.\d+)?', t)]
    if not tags:
        return month
    return f'{month}.{max(int(t.rsplit(".", 1)[1]) if t != "v" + month else 0 for t in tags) + 1}'


def changes(version):
    """Subjects of the devel commits since the last release (oldest first): since the newest devel commit with the
    same files as main (main's last release was made from it; devel may have moved on after its 'Release' commit),
    else since the last release commit on devel; a commit for this same version (left by a release that was not
    finished) does not count as one."""
    main_tree = git('rev-parse', f'{MAIN}^{{tree}}')
    last = next((h for h, t in (line.split() for line in git('log', WORK, '--format=%H %T').splitlines())
                 if t == main_tree), '')
    if not last:
        last = next((h for h, s in (line.split(' ', 1) for line in git(
            'log', WORK, '--format=%H %s', '--grep=^Release [0-9]').splitlines()) if s != f'Release {version}'), '')
    rng = f'{last}..{WORK}' if last else WORK
    return [s for s in git('log', '--reverse', '--format=%s', rng).splitlines() if not re.match(r'Release \d', s)]


def fail(text):
    print(text, file=sys.stderr)
    sys.exit(1)


def main():
    ap = argparse.ArgumentParser(description='Create a BattBench release.')
    ap.add_argument('--version', help='e.g. 2026.10 or 2026.10.1 (default: ask)')
    ap.add_argument('--dry-run', action='store_true', help='only show what would be done')
    ap.add_argument('--build', action='store_true', help='also build the installer here (GitHub Actions builds the '
                                                         'release one)')
    a = ap.parse_args()

    if git('branch', '--show-current') != WORK:
        fail(f'Releases are made from the branch {WORK} (git checkout {WORK}).')
    if git('status', '--porcelain'):
        print(git('status', '--short'))
        fail('Uncommitted changes - commit or stash them first, a release contains committed work only.')
    for remote in PUBLIC:
        git('fetch', '-q', remote)
    heads = {r: git('rev-parse', f'{r}/{MAIN}') for r in PUBLIC}
    if len(set(heads.values()) | {git('rev-parse', MAIN)}) != 1:
        fail(f'{MAIN} differs between the local repository, {" and ".join(PUBLIC)}: {heads} - sort that out first.')
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
    subjects = changes(version)

    print(f'Release {version}: {len(subjects)} commit(s) on {WORK} since the last release')
    for s in subjects:
        print(f'  - {s}')
    if a.dry_run:
        print(f"(dry run) would set __version__ = '{version}' on {WORK}, add 'Release {version}' to {MAIN} with "
              f"{WORK}'s files, tag v{version}" + (', build the installer from main' if a.build else ''))
        return

    if current != version:                # else set already (a release that was not finished)
        with open(INIT, 'w', encoding='utf-8', newline='') as f:
            f.write(VERSION_RE.sub(f"__version__ = '{version}'", init, count=1))
        git('add', INIT)
        git('commit', '-q', '-m', f'Release {version}')
    message = f'Release {version}\n\n' + ''.join(f'- {s}\n' for s in subjects) + f'\n{TRAILER}\n'
    squash = git('commit-tree', f'{WORK}^{{tree}}', '-p', MAIN, '-F', '-', stdin=message)
    git('branch', '-f', MAIN, squash)
    git('tag', '-a', f'v{version}', '-m', f'BattBench {version}', squash)
    print(f'{MAIN}: Release {version} ({squash[:7]}), tagged v{version}')

    if a.build:                           # from main: the installer shows the public commit
        git('checkout', '-q', MAIN)
        try:
            build = os.path.join(ROOT, 'packaging', 'build.ps1')
            ok = subprocess.run(['powershell', '-ExecutionPolicy', 'Bypass', '-File', build], cwd=ROOT).returncode == 0
        finally:
            git('checkout', '-q', WORK)
        if not ok:
            fail('build.ps1 failed (commits and tag are there; fix the problem, check out main and run build.ps1 '
                 'again).')
        print(f'Installer: build\\BattBench-{version}-setup.exe')

    if input(f'Push {WORK} to origin, {MAIN} and v{version} to {" and ".join(PUBLIC)}? [y/N] ').strip().lower() \
            in ('y', 'yes', 'j', 'ja'):
        git('push', 'origin', WORK)
        for remote in PUBLIC:
            git('push', remote, MAIN, f'v{version}')
        print('Pushed. GitHub Actions now builds the installer and publishes the release: '
              'https://github.com/fashberg/battbench/actions')
    else:
        print(f'Not pushed. Later: git push origin {WORK} && git push origin {MAIN} v{version} && '
              f'git push github {MAIN} v{version}')


if __name__ == '__main__':
    main()
