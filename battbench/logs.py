"""Log file next to the database (battbench.log, rotated): connections, chargers that stop answering, reconnects,
unhandled errors. --debug adds every unanswered query and skipped answer."""
import logging
import logging.handlers
import os
import sys
import threading

from . import __version__

log = logging.getLogger('battbench')
LOG_BYTES, LOG_KEEP = 2_000_000, 4


def log_path(db_path):
    return os.path.join(os.path.dirname(os.path.abspath(db_path)), 'battbench.log')


def setup(db_path, debug=False):
    """Log to <database folder>/battbench.log (and to stderr with --debug). Returns the file name."""
    path = log_path(db_path)
    log.setLevel(logging.DEBUG if debug else logging.INFO)
    fmt = logging.Formatter('%(asctime)s %(levelname)-7s %(threadName)s %(name)s: %(message)s')
    try:
        h = logging.handlers.RotatingFileHandler(path, maxBytes=LOG_BYTES, backupCount=LOG_KEEP, encoding='utf-8')
    except OSError as e:
        print(f'no log file {path}: {e!r}', file=sys.stderr)
    else:
        h.setFormatter(fmt)
        log.addHandler(h)
    if debug:
        h = logging.StreamHandler()
        h.setFormatter(fmt)
        log.addHandler(h)

    def hook(kind, value, tb):
        log.critical('unhandled exception', exc_info=(kind, value, tb))
        sys.__excepthook__(kind, value, tb)

    def thread_hook(a):
        log.critical('unhandled exception in thread %s', a.thread and a.thread.name,
                     exc_info=(a.exc_type, a.exc_value, a.exc_traceback))
        threading.__excepthook__(a)

    sys.excepthook = hook
    threading.excepthook = thread_hook
    log.info('BattBench %s started, Python %s, database %s', __version__, sys.version.split()[0], db_path)
    return path
