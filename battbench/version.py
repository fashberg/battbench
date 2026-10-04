"""Version: releases are numbered by year and month (2026.10; bug fix releases 2026.10.1, 2026.10.2 …).
Run from a git checkout, the current commit is shown as well; packaging/build.ps1 writes it into _build.py so the
installed app knows it too."""
import os
import subprocess
import sys

from . import __version__

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
AUTHOR = 'Folke Ashberg'
WEBSITE = 'https://www.ashberg.de'
SOURCE = 'https://git.ashberg.de/folke/battbench'


def _git(*args):
    flags = subprocess.CREATE_NO_WINDOW if sys.platform == 'win32' else 0
    return subprocess.run(['git', '-C', ROOT, *args], capture_output=True, text=True, timeout=3,
                          creationflags=flags).stdout.strip()


def git_info():
    """'git 0d190cf (2026-10-04)', '+' after the hash for uncommitted changes; '' without git."""
    try:
        from ._build import GIT
        return GIT
    except ImportError:
        pass
    if not os.path.exists(os.path.join(ROOT, '.git')):
        return ''
    try:
        commit, date = _git('log', '-1', '--format=%h %cs').split()
        dirty = '+' if _git('status', '--porcelain', '--untracked-files=no') else ''
        return f'git {commit}{dirty} ({date})'
    except (OSError, ValueError, subprocess.SubprocessError):
        return ''


def full_version():
    git = git_info()
    return f'{__version__} · {git}' if git else __version__
