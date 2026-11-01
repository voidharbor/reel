"""Command line: reel, reel <search>, reel --setup, reel --check."""
import platform, sys

from . import __version__

USAGE = """reel: pick movies and TV episodes off usenet with the arrow keys.

  reel                    open the picker
  reel knowing 2009       open it with a search already running
  reel superjail s01      a show plus s01 (a season) or s01e02 (an episode)
  reel --setup            change the indexer, SABnzbd or library folders
  reel --check            test every connection and print what's wrong
  reel --version
"""


def check():
    from . import config, engine, lookup
    cfg = config.load()
    print(f'reel {__version__}   settings: {config.path()}')
    rows = []
    try:
        import curses  # noqa: F401
        rows.append(('OK', 'python', f'{platform.python_version()} on {platform.system()}, curses works'))
    except ImportError:
        rows.append(('FAIL', 'python', 'no curses module; on Windows run:  py -m pip install windows-curses'))
    rows += engine.doctor(cfg)
    if cfg.get('lookups', True):
        ok = lookup.suggest('knowing')
        rows.append(('OK', 'lookups', 'IMDb suggestions answered') if ok else
                    ('WARN', 'lookups', 'IMDb suggestions did not answer; search still works, without suggestions'))
    for level, what, msg in rows:
        print(f'  {level:<5} {what:<8} {msg}')
    bad = sum(1 for r in rows if r[0] == 'FAIL')
    print('  all good' if not bad else f'  {bad} problem(s); fix them with  reel --setup')
    return 1 if bad else 0


def main(argv=None):
    a = sys.argv[1:] if argv is None else argv
    if a[:1] in (['-h'], ['--help']):
        print(USAGE)
        return 0
    if a[:1] in (['-V'], ['--version']):
        print(f'reel {__version__}')
        return 0
    if a[:1] == ['--run-job'] and len(a) == 2:
        from .engine import run_job
        return run_job(a[1])
    if a[:1] == ['--check']:
        return check()
    try:
        from . import ui
    except ImportError as e:
        if 'curses' in str(e):
            print('reel needs curses. On Windows run:  py -m pip install windows-curses')
            return 1
        raise
    return ui.run(' '.join(x for x in a if x != '--setup'), force_setup='--setup' in a)
