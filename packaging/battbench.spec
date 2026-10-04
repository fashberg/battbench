# PyInstaller build of BattBench (one folder, no console). Used by packaging/build.ps1:
#   pyinstaller --noconfirm --distpath build/dist --workpath build/work packaging/battbench.spec
import os

from PyInstaller.utils.hooks import collect_submodules

ROOT = os.path.dirname(SPECPATH)

a = Analysis(
    [os.path.join(SPECPATH, 'launcher.py')],
    pathex=[ROOT],
    datas=[
        (os.path.join(ROOT, 'battbench', 'translations', '*.qm'), 'battbench/translations'),
        (os.path.join(ROOT, 'battbench', 'resources', '*.svg'), 'battbench/resources'),
    ],
    hiddenimports=collect_submodules('winrt') + collect_submodules('bleak.backends.winrt'),
    excludes=['tkinter', 'matplotlib', 'PySide6.QtWebEngineCore', 'PySide6.QtWebEngineWidgets', 'PySide6.QtQml',
              'PySide6.QtQuick', 'PySide6.QtMultimedia', 'PySide6.Qt3DCore', 'PySide6.QtCharts'],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='BattBench',
    icon=os.path.join(SPECPATH, 'battbench.ico'),
    console=False,
    upx=False,
)
coll = COLLECT(exe, a.binaries, a.datas, upx=False, name='BattBench')
