"""Update the translation files after texts in the source or the README's user guide changed:

    .venv/Scripts/python battbench/translations/update.py      (Windows)
    .venv/bin/python battbench/translations/update.py          (Linux / macOS)

1. lupdate collects all tr() / QT_TRANSLATE_NOOP texts into battbench_<lang>.ts (existing translations are kept).
2. Translate the new entries, e.g. with Qt Linguist (pyside6-linguist battbench_de.ts).
3. Run this script again: lrelease compiles the .ts files into the .qm files the app loads.

Built-in help: the README section between the help markers is copied to battbench/help/help_en.md. The other
languages (help_<lang>.md) are translated by hand; the script warns when the English text changed since. After
updating a translation run it with --help-translated to record that.
"""
import argparse
import glob
import hashlib
import os
import re
import subprocess
import sys

import PySide6

HERE = os.path.dirname(os.path.abspath(__file__))
PKG = os.path.dirname(HERE)
README = os.path.join(os.path.dirname(PKG), 'README.md')
HELP = os.path.join(PKG, 'help')
LANGS = ['de']


def tool(name):
    exe = os.path.join(os.path.dirname(PySide6.__file__), name + ('.exe' if sys.platform == 'win32' else ''))
    return exe if os.path.exists(exe) else 'pyside6-' + name


def update_help(translated):
    """README user guide -> help_en.md; check (or with translated: record) that the translations are current."""
    with open(README, encoding='utf-8') as f:
        m = re.search(r'<!-- help\b[^>]*-->\s*\n(.*?)\n<!-- /help -->', f.read(), re.S)
    if not m:
        sys.exit('README.md: help markers not found')
    text = m.group(1).strip() + '\n'
    with open(os.path.join(HELP, 'help_en.md'), 'w', encoding='utf-8', newline='\n') as f:
        f.write(text)
    digest = hashlib.sha1(text.encode('utf-8')).hexdigest()
    for lang in LANGS:
        stamp = os.path.join(HELP, f'help_{lang}.source')
        if translated:
            with open(stamp, 'w', encoding='utf-8') as f:
                f.write(digest + '\n')
        elif not os.path.exists(stamp) or open(stamp, encoding='utf-8').read().strip() != digest:
            print(f'NOTE: the user guide in README.md changed - update help/help_{lang}.md, then run this script '
                  'with --help-translated')


def main():
    ap = argparse.ArgumentParser(description='Update translations and the built-in help.')
    ap.add_argument('--help-translated', action='store_true',
                    help='the help translations match the current README user guide')
    a = ap.parse_args()
    sources = sorted(glob.glob(os.path.join(PKG, '*.py')))
    for lang in LANGS:
        ts = os.path.join(HERE, f'battbench_{lang}.ts')
        subprocess.run([tool('lupdate'), '-no-obsolete', '-locations', 'none', '-target-language', lang, *sources,
                        '-ts', ts], check=True)
        subprocess.run([tool('lrelease'), ts, '-qm', ts[:-3] + '.qm'], check=True)
    update_help(a.help_translated)


if __name__ == '__main__':
    main()
