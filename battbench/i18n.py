"""Translations. Texts in the source are English; other languages come from Qt translation files
(translations/battbench_<lang>.ts, compiled to .qm, see translations/update.py).

- tr(text): UI texts and messages. Works without Qt too (driver / DB scripts): then it returns the English text.
- QT_TRANSLATE_NOOP('data', text): marks values that are stored or passed on in English (task, status, grade,
  phase, mode names); they are translated only when shown, with tr_data().
"""
import os

try:
    from PySide6.QtCore import QCoreApplication, QLibraryInfo, QLocale, QTranslator
except ImportError:                     # used without the GUI
    QCoreApplication = None

LANGUAGES = {'en': 'English', 'de': 'Deutsch'}
current = 'en'                          # language installed by install()
DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'translations')


def tr(text):
    return QCoreApplication.translate('', text) if QCoreApplication else text


def tr_data(text):
    return QCoreApplication.translate('data', text) if QCoreApplication and text else text


def QT_TRANSLATE_NOOP(_context, text):
    return text


def pick_language(setting):
    """'auto' / '' -> German on a German system, else English."""
    if setting in LANGUAGES:
        return setting
    return 'de' if QLocale.system().language() == QLocale.German else 'en'


def install(app, lang):
    """Load the app's and Qt's own translations (standard buttons) for lang; English needs none."""
    global current
    current = lang
    app.translators = []
    if lang == 'en':
        return
    for name, path in (('battbench', DIR), ('qtbase', QLibraryInfo.path(QLibraryInfo.TranslationsPath))):
        t = QTranslator(app)
        if t.load(QLocale(lang), name, '_', path):
            app.installTranslator(t)
            app.translators.append(t)
