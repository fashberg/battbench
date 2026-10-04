"""Update the translation files after texts in the source changed:

    .venv/Scripts/python battbench/translations/update.py      (Windows)
    .venv/bin/python battbench/translations/update.py          (Linux / macOS)

1. lupdate collects all tr() / QT_TRANSLATE_NOOP texts into battbench_<lang>.ts (existing translations are kept).
2. Translate the new entries, e.g. with Qt Linguist (pyside6-linguist battbench_de.ts).
3. Run this script again: lrelease compiles the .ts files into the .qm files the app loads.
"""
import glob
import os
import subprocess
import sys

import PySide6

HERE = os.path.dirname(os.path.abspath(__file__))
PKG = os.path.dirname(HERE)
LANGS = ['de']


def tool(name):
    exe = os.path.join(os.path.dirname(PySide6.__file__), name + ('.exe' if sys.platform == 'win32' else ''))
    return exe if os.path.exists(exe) else 'pyside6-' + name


def main():
    sources = sorted(glob.glob(os.path.join(PKG, '*.py')))
    for lang in LANGS:
        ts = os.path.join(HERE, f'battbench_{lang}.ts')
        subprocess.run([tool('lupdate'), '-no-obsolete', '-locations', 'none', '-target-language', lang, *sources,
                        '-ts', ts], check=True)
        subprocess.run([tool('lrelease'), ts, '-qm', ts[:-3] + '.qm'], check=True)


if __name__ == '__main__':
    main()
