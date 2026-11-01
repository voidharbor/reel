"""The curses app: search, pick the title (and season or episode), pick the exact release, download."""
import curses, json, locale, os, sys, textwrap, threading, time, unicodedata

from . import config, engine as E, lookup as L

JOB_DAYS = 2           # downloads older than this drop off the list
SORTS = ['best', 'size', 'name', 'newest', 'grabs']


# ---------------------------------------------------------------- text width

def cwidth(ch):
    if unicodedata.combining(ch):
        return 0
    return 2 if unicodedata.east_asian_width(ch) in ('W', 'F') else 1


def dwidth(s):
    return sum(cwidth(c) for c in s)


def fit(s, n):
    out, w = [], 0
    for ch in s:
        c = cwidth(ch)
        if w + c > n:
            break
        out.append(ch)
        w += c
    return ''.join(out)


def clip(s, n):
    if n <= 0:
        return ''
    return s if dwidth(s) <= n else fit(s, n - 1) + '…'


def pad(s, n):
    s = fit(s, n)
    return s + ' ' * (n - dwidth(s))


def tilde(p):
    h = os.path.expanduser('~')
    return '~' + p[len(h):] if p.startswith(h + os.sep) else p


def fmt_dur(sec):
    sec = int(max(0, sec or 0))
    h, r = divmod(sec, 3600)
    m, s = divmod(r, 60)
    return f'{h}:{m:02d}:{s:02d}' if h else f'{m}:{s:02d}'


def age(ts):
    if not ts:
        return '?'
    d = max(0, time.time() - ts)
    for unit, n in (('y', 365 * 86400), ('mo', 30 * 86400), ('d', 86400), ('h', 3600)):
        if d >= n:
            return f'{int(d // n)}{unit}'
    return 'new'


# ---------------------------------------------------------------- history

def history_path():
    return os.path.join(config.home(), 'history.json')


def load_history():
    try:
        with open(history_path(), encoding='utf-8') as f:
            return [h for h in json.load(f) if isinstance(h, dict) and h.get('text')][-50:]
    except (OSError, ValueError):
        return []


def save_history(hist):
    try:
        os.makedirs(config.home(), exist_ok=True)
        with open(history_path(), 'w', encoding='utf-8') as f:
            json.dump(hist[-50:], f)
    except OSError:
        pass


# ---------------------------------------------------------------- look

C = {}


def init_colors():
    try:
        curses.start_color()
        rich = curses.COLORS >= 256
        bg = 16 if rich else curses.COLOR_BLACK
        spec = {'main': 34, 'hi': 46, 'dim': 28, 'border': 22, 'amber': 214, 'red': 196} if rich else \
            {'main': curses.COLOR_GREEN, 'hi': curses.COLOR_GREEN, 'dim': curses.COLOR_GREEN,
             'border': curses.COLOR_GREEN, 'amber': curses.COLOR_YELLOW, 'red': curses.COLOR_RED}
        for i, (k, fg) in enumerate(spec.items(), 1):
            curses.init_pair(i, fg, bg)
            C[k] = curses.color_pair(i)
        curses.init_pair(len(spec) + 1, bg, spec['hi'])
        C['sel'] = curses.color_pair(len(spec) + 1) | curses.A_BOLD
        C['hi'] |= curses.A_BOLD
        if not rich:
            C['dim'] |= curses.A_DIM
            C['border'] |= curses.A_DIM
            C['amber'] |= curses.A_BOLD
    except curses.error:
        C.update(main=curses.A_NORMAL, hi=curses.A_BOLD, dim=curses.A_DIM, border=curses.A_DIM,
                 amber=curses.A_BOLD, red=curses.A_BOLD, sel=curses.A_REVERSE)


_R = ['██████╗ ', '██╔══██╗', '██████╔╝', '██╔══██╗', '██║  ██║', '╚═╝  ╚═╝']
_E = ['███████╗', '██╔════╝', '█████╗  ', '██╔══╝  ', '███████╗', '╚══════╝']
_L = ['██╗     ', '██║     ', '██║     ', '██║     ', '███████╗', '╚══════╝']
BANNER = [' '.join(row) for row in zip(_R, _E, _E, _L)]

TITLES = {'setup': 'SETUP', 'search': 'SEARCH', 'show': 'SHOW', 'results': 'RESULTS', 'confirm': 'CONFIRM',
          'job': 'DOWNLOAD', 'jobs': 'DOWNLOADS'}
HINTS = {
    'setup': '↑↓ move · type to edit · enter next · esc back',
    'search': 'enter search · ↓ pick a suggestion · tab movie/tv · ^D downloads · ^P settings · esc quit',
    'show': '↑↓ move · enter open · esc back · d downloads · q quit',
    'results': '↑↓ move · enter grab · s sort · r reverse · f filter · h hide junk · esc back · q quit',
    'filter': 'type to filter · -word leaves it out · enter keep · esc clear',
    'confirm': 'enter grab it · esc back · type to edit where it goes',
    'job': 'esc back (it keeps downloading) · d downloads · / new search · q quit',
    'jobs': '↑↓ move · enter open · esc back · / new search · q quit',
}
KEYS = {curses.KEY_UP: 'UP', curses.KEY_DOWN: 'DOWN', curses.KEY_LEFT: 'LEFT', curses.KEY_RIGHT: 'RIGHT',
        curses.KEY_HOME: 'HOME', curses.KEY_END: 'END', curses.KEY_PPAGE: 'PGUP', curses.KEY_NPAGE: 'PGDN',
        curses.KEY_BACKSPACE: 'BS', curses.KEY_DC: 'DEL', curses.KEY_ENTER: 'ENTER',
        curses.KEY_RESIZE: 'RESIZE', curses.KEY_BTAB: 'BTAB'}
CHARS = {'\n': 'ENTER', '\r': 'ENTER', '\x1b': 'ESC', '\t': 'TAB', '\x7f': 'BS', '\x08': 'BS',
         '\x01': 'HOME', '\x05': 'END', '\x15': 'KILL', '\x17': 'WORD', '\x03': 'QUIT', '\x04': 'JOBS',
         '\x10': 'SETUP'}
for _name in ('PADENTER', 'CTL_PAD8', 'CTL_PAD2'):        # windows-curses keypad extras
    if hasattr(curses, 'KEY_' + _name):
        KEYS[getattr(curses, 'KEY_' + _name)] = 'ENTER' if _name == 'PADENTER' else ''


def keyname(k):
    if isinstance(k, int):
        return KEYS.get(k, '')
    return CHARS.get(k, k)


class Line:
    """A one-line text field with a blinking block cursor."""

    def __init__(self, t='', secret=False):
        self.secret = secret
        self.set(t)

    def set(self, t):
        self.t, self.c = t, len(t)

    def key(self, k):
        if len(k) == 1 and k.isprintable():
            self.t = self.t[:self.c] + k + self.t[self.c:]
            self.c += 1
        elif k == 'BS' and self.c:
            self.t = self.t[:self.c - 1] + self.t[self.c:]
            self.c -= 1
        elif k == 'DEL':
            self.t = self.t[:self.c] + self.t[self.c + 1:]
        elif k == 'LEFT':
            self.c = max(0, self.c - 1)
        elif k == 'RIGHT':
            self.c = min(len(self.t), self.c + 1)
        elif k == 'HOME':
            self.c = 0
        elif k == 'END':
            self.c = len(self.t)
        elif k == 'KILL':
            self.set('')
        elif k == 'WORD':
            left = self.t[:self.c].rstrip()
            cut = left.rfind(' ') + 1
            self.t, self.c = left[:cut] + self.t[self.c:], cut
        else:
            return False
        return True

    def masked(self):
        if not self.secret or len(self.t) <= 4:
            return self.t
        return '•' * (len(self.t) - 4) + self.t[-4:]

    def draw(self, app, y, x, w, attr, focus=True):
        if not focus:
            app.put(y, x, clip(self.masked(), w), attr)
            return
        start = max(0, self.c - w + 1)
        app.put(y, x, fit(self.t[start:], w), attr)
        if int(time.time() * 2) % 2 == 0:
            app.put(y, x + dwidth(self.t[start:self.c]), self.t[self.c] if self.c < len(self.t) else ' ', C['sel'])


class Task:
    """One background call (search, lookup, checks) with a spinner; then() runs on the main thread."""

    def __init__(self, label, fn, then, fail=None):
        self.label, self.then, self.fail = label, then, fail
        self.t0, self.done, self.res, self.err = time.time(), False, None, None

        def work():
            try:
                self.res = fn()
            except E.ReelError as e:
                self.err = str(e)
            except Exception as e:
                self.err = f'{type(e).__name__}: {e}'
            self.done = True
        threading.Thread(target=work, daemon=True).start()


# ---------------------------------------------------------------- downloads

POST = ('verifying', 'repairing', 'extracting', 'moving', 'running', 'placing', 'fetching')


class Job:
    """One detached runner. Its JSON-lines log is the whole state, so jobs outlive reel."""

    def __init__(self, meta, proc=None):
        self.m, self.proc = meta, proc
        self.lines, self.offset, self.state = [], 0, {}
        self.result, self.notified, self.alive = None, False, True

    @classmethod
    def load_all(cls):
        jobs, d = [], E.jobs_dir()
        try:
            names = sorted(n for n in os.listdir(d) if n.endswith('.json'))
        except OSError:
            return jobs
        cutoff = time.time() - JOB_DAYS * 86400
        for n in names:
            try:
                with open(os.path.join(d, n), encoding='utf-8') as f:
                    meta = json.load(f)
            except (OSError, ValueError):
                continue
            if meta.get('started', 0) > cutoff:
                j = cls(meta)
                j.refresh()
                j.notified = j.result is not None
                jobs.append(j)
        return jobs

    def running(self):
        return self.proc.poll() is None if self.proc is not None else E.pid_alive(self.m.get('pid'))

    def refresh(self):
        alive = self.running()          # checked before the read, so a finished job's log is complete
        try:
            with open(self.m['log'], 'rb') as f:
                f.seek(self.offset)
                chunk = f.read()
        except OSError:
            chunk = b''
        end = chunk.rfind(b'\n') + 1
        if end:
            self.offset += end
            for raw in chunk[:end].decode('utf-8', 'replace').splitlines():
                self.take(raw)
        if self.result is None and not alive:
            last = self.lines[-1] if self.lines else 'no output at all'
            self.result = {'result': 'fail', 'reason': 'the download process stopped without a verdict',
                           'hint': last, 't': time.time()}
        self.alive = alive

    def take(self, raw):
        try:
            d = json.loads(raw)
        except ValueError:
            if raw.strip():
                self.lines.append(raw.strip())       # a traceback, say
            return
        stamp = time.strftime('%H:%M:%S', time.localtime(d.get('t') or time.time()))
        if 'result' in d:
            self.result = d
            self.lines.append(f"{stamp}  {'OK ' + d.get('path', '') if d['result'] == 'ok' else 'FAILED ' + d.get('reason', '')}")
            return
        if 'stage' in d:
            if d['stage'] != self.state.get('stage') and not d.get('msg'):
                self.lines.append(f"{stamp}  {d['stage']}")
            self.state = d
        if d.get('msg'):
            self.lines.append(f"{stamp}  {d['msg']}")

    def stage(self):
        return (self.state.get('stage') or 'starting').upper()

    def frac(self):
        st = self.state.get('stage')
        if st == 'downloading' and self.state.get('pct') is not None:
            return min(1.0, self.state['pct'] / 100)
        return 1.0 if st in POST else None

    def label(self):
        if self.result and self.result.get('result') == 'ok' and self.m['kind'] != 'pack':
            return os.path.basename(self.result['path'])
        name = os.path.basename(self.m['target'].rstrip('/\\'))
        return name + (' (season pack)' if self.m['kind'] == 'pack' else '')

    def status(self):
        if self.result is None:
            f = self.frac()
            return self.stage().lower() + (f' {int(f * 100)}%' if f is not None else '')
        return 'landed' if self.result['result'] == 'ok' else 'failed'

    def elapsed(self):
        end = (self.result or {}).get('t') or time.time()
        return end - self.m['started']


# ---------------------------------------------------------------- the app

class App:
    def __init__(self, scr, initial='', force_setup=False):
        self.scr = scr
        self.cfg = config.load()
        self.failed = E.Failed()
        self.mode = 'auto'
        self.edit = Line(initial.strip())
        self.hist = load_history()
        self.dsel = -1
        self.sugg = {'text': None, 'items': [], 'due': 0, 'busy': False}
        self.task = None
        self.show, self.season, self.ssel, self.stop, self.have_eps = None, None, 0, 0, set()
        self.want, self.raw, self.rows, self.sel, self.top, self.list_h = None, [], [], 0, 0, 10
        self.sort, self.rev, self.filt, self.filtering, self.hide_junk = 'best', False, Line(), False, False
        self.notice, self.have, self.results_back, self.batch = None, '', 'search', None
        self.pick, self.pick_kind, self.path, self.preview, self.pick_titles, self.season_hint = \
            None, None, Line(), [], {}, None
        self.jobs, self.job, self.jsel, self.job_back, self.jobs_back = Job.load_all(), None, 0, 'results', 'search'
        self.flash_msg, self.flash_until, self.flash_attr = '', 0, 'amber'
        self.sab_state = ('CHECKING', 'dim')
        self.boot_t0, self.quit = time.time(), False
        self.setup_fields, self.fsel, self.checks = [], 0, []
        self.screen = 'search'
        self.refresh_library()
        self.check_sab()
        if force_setup or not config.is_complete(self.cfg):
            self.open_setup()
        elif self.edit.t:
            self.submit()

    # -- plumbing

    def put(self, y, x, s, attr=None):
        h, w = self.scr.getmaxyx()
        if y < 0 or y >= h or x < 0 or x >= w or not s:
            return
        try:
            self.scr.addstr(y, x, fit(s, w - x), C['main'] if attr is None else attr)
        except curses.error:
            pass        # the bottom-right cell raises after drawing

    def box(self, y, x, w, h, attr):
        self.put(y, x, '┌' + '─' * (w - 2) + '┐', attr)
        for i in range(1, h - 1):
            self.put(y + i, x, '│', attr)
            self.put(y + i, x + w - 1, '│', attr)
        self.put(y + h - 1, x, '└' + '─' * (w - 2) + '┘', attr)

    def col(self, w, most=96):
        cw = min(w - 6, most)
        return max(3, (w - cw) // 2), cw

    def flash(self, msg, attr='amber', secs=6):
        self.flash_msg, self.flash_attr, self.flash_until = msg, attr, time.time() + secs

    def busy(self):
        return self.task is not None

    def animating(self):
        return (self.boot_t0 and time.time() - self.boot_t0 < 0.8) or self.task is not None

    def run(self):
        try:
            curses.raw()            # ^C, ^S, ^O and friends come to us instead of the terminal
            curses.curs_set(0)
        except curses.error:
            pass
        if hasattr(curses, 'set_escdelay'):
            try:
                curses.set_escdelay(25)
            except curses.error:
                pass
        init_colors()
        self.scr.bkgd(' ', C['main'])
        self.scr.keypad(True)
        while not self.quit:
            try:
                self.tick()
                self.draw()
                self.scr.timeout(30 if self.animating() else 200)
                try:
                    k = self.scr.get_wch()
                except curses.error:
                    continue
                name = keyname(k)
                if name == 'RESIZE' and sys.platform == 'win32':
                    try:
                        curses.resize_term(0, 0)
                    except curses.error:
                        pass
                self.key(name)
            except KeyboardInterrupt:
                self.quit = True

    def refresh_library(self):
        try:
            self.counts = E.library_counts(self.cfg)
            self.recent = E.recent(self.cfg)
        except OSError:
            self.counts, self.recent = (0, 0), []

    def check_sab(self):
        def work():
            v = E.Sab(self.cfg).version()
            self.sab_state = (f'UP · {v}', 'hi') if v else ('NOT ANSWERING', 'amber')
        self.sab_state = ('CHECKING', 'dim')
        threading.Thread(target=work, daemon=True).start()

    def tick(self):
        t = self.task
        if t and t.done:
            self.task = None
            if t.err:
                (t.fail or (lambda e: self.flash(e, 'red', 10)))(t.err)
            else:
                t.then(t.res)
        s = self.sugg
        text = L.parse(self.edit.t)['title']
        if (self.screen == 'search' and self.cfg.get('lookups', True) and len(text) >= 2 and text != s['text']
                and time.time() >= s['due'] and not s['busy']):
            s['busy'] = True

            def work(q=text):
                items = L.suggest(q)
                s.update(text=q, items=items, busy=False)
            threading.Thread(target=work, daemon=True).start()
        for j in self.jobs:
            j.refresh()
            if j.result and not j.notified:
                j.notified = True
                ok = j.result['result'] == 'ok'
                if not (self.screen == 'job' and self.job is j):
                    self.flash(('✓ landed: ' if ok else '✗ failed: ') + j.label(), 'hi' if ok else 'red', 8)
                if ok:
                    self.refresh_library()
                    if self.show:
                        self.have_eps = E.episodes_on_disk(self.show_root())
                else:
                    self.failed = E.Failed()
                    for row in self.rows:
                        if row['r']['title'] == j.m['release']:
                            row.update(ok=False, why='failed now', best=False)

    # -- finding the title

    def dropdown(self):
        """Suggestions for what's typed, or recent searches when the box is empty."""
        if not self.edit.t.strip():
            seen, out = set(), []
            for h in reversed(self.hist):
                if h['text'].lower() not in seen:
                    seen.add(h['text'].lower())
                    out.append({'type': 'hist', 'text': h['text'], 'cand': h.get('cand')})
            return out[:6]
        if self.sugg['text'] != L.parse(self.edit.t)['title']:
            return []
        items = [c for c in self.sugg['items'] if self.mode == 'auto' or c['kind'] == self.mode]
        return [{'type': 'sugg', 'cand': c} for c in items[:6]]

    def remember(self, text, cand=None):
        self.hist = [h for h in self.hist if h['text'].lower() != text.lower()]
        self.hist.append({'text': text, 'cand': cand})
        save_history(self.hist)

    def submit(self, item=None):
        if item:
            if item['type'] == 'sugg':
                c = item['cand']
                p = L.parse(self.edit.t)
                self.remember(L.label(c) + self.tail(p), c)
                return self.choose(c, p, None)
            self.edit.set(item['text'])
            if item.get('cand'):
                self.remember(item['text'], item['cand'])
                return self.choose(item['cand'], L.parse(item['text']), None)
        text = self.edit.t.strip()
        if not text:
            self.flash('type a title first')
            return
        self.remember(text)
        if not self.cfg.get('lookups', True):
            return self.plain_search(L.parse(text), text)
        mode = self.mode
        self.task = Task('LOOKING IT UP', lambda: L.resolve(text, mode), lambda r: self.resolved(text, *r))

    def tail(self, p):
        if p['season'] is not None and p['episode'] is not None:
            return f" S{p['season']:02d}E{p['episode']:02d}"
        return f" S{p['season']:02d}" if p['season'] is not None else ''

    def resolved(self, text, p, c, how, alt):
        if how in ('exact', 'corrected'):
            notice = ('corrected', c, text) if how == 'corrected' else (('alt', alt, text) if alt else None)
            self.choose(c, p, notice)
        elif how == 'maybe':
            self.plain_search(p, text, ('maybe', c, text))
        else:
            self.plain_search(p, text)

    def choose(self, c, p, notice):
        self.notice = notice
        if c['kind'] == 'movie':
            self.results_back = 'search'
            return self.run_search({'kind': 'movie', 'title': c['title'], 'year': c.get('year') or p['year'],
                                    'imdb': c.get('imdb'), 'label': L.label(c)})
        self.task = Task('FETCHING THE EPISODE LIST',
                         lambda: L.tv_show(imdb=c.get('imdb'), tvmaze=c.get('tvmaze'), name=c['title']),
                         lambda show: self.got_show(show, c, p), fail=lambda err: self.got_show(None, c, p))

    def got_show(self, show, c, p):
        if not show:
            self.flash(f"couldn't get an episode list for {c['title']}; searching by name")
            q = dict(p, title=c['title'])
            return self.plain_search(q, c['title'], self.notice, as_tv=True)
        self.show = show
        self.have_eps = E.episodes_on_disk(self.show_root())
        self.ssel = self.stop = 0
        if p['season'] is not None and p['season'] in show['seasons']:
            self.season = p['season']
            self.results_back = 'show'
            if p['episode'] is not None:
                return self.pick_episode(p['season'], p['episode'])
            return self.pick_season(p['season'])
        self.season = None
        self.screen = 'show'

    def show_root(self):
        return os.path.join(config.library(self.cfg, 'tv'), E.show_dir_name(self.cfg, self.show['name']))

    def plain_search(self, p, text, notice=None, as_tv=False):
        self.notice = notice
        if p['season'] is not None and p['episode'] is not None:
            want = {'kind': 'episode', 'show': p['title'], 'season': p['season'], 'episode': p['episode'],
                    'label': f"{p['title']} S{p['season']:02d}E{p['episode']:02d}"}
        elif p['season'] is not None:
            want = {'kind': 'season', 'show': p['title'], 'season': p['season'],
                    'label': f"{p['title']} season {p['season']}"}
        elif self.mode == 'movie' or (p['year'] and not as_tv and self.mode != 'tv'):
            want = {'kind': 'movie', 'title': p['title'], 'year': p['year'], 'label': text}
        else:
            want = {'kind': 'raw', 'text': text, 'label': text}
        self.results_back = 'search'
        self.run_search(want)

    def pick_season(self, s):
        sh = self.show
        self.run_search({'kind': 'season', 'show': sh['name'], 'season': s, 'tvmaze': sh['tvmaze'],
                         'titles': L.episode_titles(sh), 'label': f"{sh['name']} · season {s}"})

    def pick_episode(self, s, e):
        sh = self.show
        title = next((x['title'] for x in sh['seasons'].get(s, []) if x['episode'] == e), '')
        self.run_search({'kind': 'episode', 'show': sh['name'], 'season': s, 'episode': e, 'tvmaze': sh['tvmaze'],
                         'titles': L.episode_titles(sh),
                         'label': f"{sh['name']} S{s:02d}E{e:02d}" + (f' · {title}' if title else '')})

    def run_search(self, want):
        name = self.cfg['indexer']['name'].upper()
        self.task = Task(f'SEARCHING {name}', lambda: E.search(self.cfg, want), lambda res: self.got_results(want, res))

    def got_results(self, want, res):
        if not res and not (self.notice and self.notice[0] == 'maybe'):
            self.flash(f"{self.cfg['indexer']['name']} has nothing for {want['label']}", 'amber', 8)
            return
        self.want, self.raw = want, res
        rows = []
        for r in res:
            ok, why = E.verdict(r, want, self.cfg, self.failed)
            rows.append({'r': r, 'ok': ok, 'why': why, 'score': E.score(r, want), 'pop': E.popularity(r),
                         'pack': bool(E.pack_seasons(r['title'])) and want['kind'] != 'movie'})
        self.batch = self.every_episode(want, rows) if want['kind'] == 'season' else None
        best = sorted((x for x in rows if x['ok'] and not x.get('have')), key=lambda x: (-x['score'], -x['pop']))
        if self.batch and not (best and best[0]['pack']):
            self.batch['best'] = True
        elif best:
            best[0]['best'] = True
        self.rows, self.sel, self.top = rows, 0, 0
        self.sort, self.rev, self.hide_junk, self.filtering = 'best', False, False, False
        self.filt.set('')
        self.have = self.have_line(want, best[0] if best else None)
        self.screen = 'results'

    def every_episode(self, want, rows):
        """The best clean single-episode release of each episode not in the library yet, as one row."""
        per = {}
        listed = {x['episode'] for x in (self.show or {}).get('seasons', {}).get(want['season'], [])}
        for row in rows:
            tag = E.episode_tag(row['r']['title'])
            row['have'] = bool(tag and (tag[0], tag[1]) in self.have_eps)
            if (row['ok'] and tag and tag[0] == want['season'] and not tag[2] and not row['have']
                    and (not listed or tag[1] in listed)):        # TVmaze's list keeps E00 specials out
                cur = per.get(tag[1])
                if cur is None or (row['score'], row['pop']) > (cur['score'], cur['pop']):
                    per[tag[1]] = row
        if not per:
            return None
        picks = [per[e] for e in sorted(per)]
        total = sum(p['r'].get('size') or 0 for p in picks)
        n = len(picks)
        return {'batch': picks, 'ok': True, 'why': '', 'score': 99, 'pop': 0, 'pack': False,
                'r': {'title': f"every episode you don't have: {n} release{'s' if n != 1 else ''}, the best clean one of each",
                      'size': total, 'grabs': None, 'date': None}}

    def have_line(self, want, best):
        if want['kind'] == 'movie' and best:
            stem = E.movie_target(self.cfg, want['title'], want.get('year'),
                                  E.resolution(best['r']['title']) or self.cfg['quality']['resolution'])
            have = E.existing_file(stem)
            return f'✓ already in the library: {os.path.basename(have)} ({E.gb(os.path.getsize(have))})' if have else ''
        if want['kind'] == 'episode' and (want['season'], want['episode']) in self.have_eps:
            return '✓ this episode is already in the library'
        if want['kind'] == 'season' and self.show:
            n = len(self.show['seasons'].get(want['season'], []))
            got = sum(1 for (s, _) in self.have_eps if s == want['season'])
            if got:
                return f'✓ {got} of {n} episodes already in the library; a pack only adds the rest'
        return ''

    # -- results

    def visible(self):
        rows = [r for r in self.rows if r['ok']] if self.hide_junk else list(self.rows)
        terms = self.filt.t.lower().split()
        if terms:
            def keep(r):
                t = r['r']['title'].lower()
                return all((x[1:] not in t) if x.startswith('-') and len(x) > 1 else (x in t) for x in terms)
            rows = [r for r in rows if keep(r)]
        key = {
            'best': lambda r: (not r['ok'], -r['score'], -r['pop']),
            'size': lambda r: -(r['r'].get('size') or 0),
            'name': lambda r: r['r']['title'].lower(),
            'newest': lambda r: -(r['r'].get('date') or 0),
            'grabs': lambda r: -(r['r'].get('grabs') or 0),
        }[self.sort]
        rows.sort(key=key, reverse=self.rev)
        if self.batch and not terms:
            rows.insert(0, self.batch)
        return rows

    def open_confirm(self, row):
        t, w = row['r']['title'], self.want
        tag, seasons = E.episode_tag(t), E.pack_seasons(t)
        self.pick_titles, self.season_hint, self.preview = w.get('titles') or {}, None, []
        if row.get('batch'):
            kind = 'batch'
            path = os.path.join(config.library(self.cfg, 'tv'), E.show_dir_name(self.cfg, w['show']))
            for p in row['batch']:
                s, e, _ = E.episode_tag(p['r']['title'])
                name = self.pick_titles.get(f'{s}:{e}') or E.episode_title_from_results(self.raw, s, e)
                self.preview.append(f"S{s:02d}E{e:02d}  {pad(name, 26)}  {E.gb(p['r'].get('size')):>8}   {p['r']['title']}")
        elif w['kind'] == 'movie' or (w['kind'] == 'raw' and not tag and not seasons):
            if w['kind'] == 'movie':
                title, year = w['title'], w.get('year')
            else:
                title, year = E.split_movie(t)
                title = title or w['text']
            res = E.resolution(t) or self.cfg['quality']['resolution']
            kind, path = 'movie', E.movie_target(self.cfg, title, year, res) + '.mkv'
        elif tag:
            s, e, last = tag
            show = w.get('show') or E.show_of(t)
            ep = self.pick_titles.get(f'{s}:{e}') or E.episode_title_from_results(self.raw, s, e)
            kind = 'episode'
            path = E.episode_target(self.cfg, E.show_dir_name(self.cfg, show), s, e, ep, last) + '.mkv'
        elif seasons:
            show = w.get('show') or E.show_of(t)
            kind = 'pack'
            path = os.path.join(config.library(self.cfg, 'tv'), E.show_dir_name(self.cfg, show))
            if seasons != E.ALL_SEASONS and len(seasons) == 1:
                self.season_hint = next(iter(seasons))
            first = min(seasons) if seasons != E.ALL_SEASONS else (min(self.show['seasons']) if self.show else 1)
            eps = (self.show or {}).get('seasons', {}).get(first) or [{'episode': i, 'title': ''} for i in (1, 2, 3)]
            for x in eps[:3]:
                stem = E.episode_target(self.cfg, os.path.basename(path), first, x['episode'],
                                        self.pick_titles.get(f"{first}:{x['episode']}") or x['title'], tv_root='')
                self.preview.append(stem.lstrip('/\\') + '.mkv')
        else:
            self.flash("can't tell which episode or season this release is")
            return
        self.pick, self.pick_kind = row, kind
        self.path.set(tilde(path))
        self.screen = 'confirm'

    def go(self):
        target = os.path.abspath(os.path.expanduser(self.path.t.strip()))
        if not self.path.t.strip():
            self.flash('say where it goes first')
            return
        if self.pick_kind == 'batch':
            return self.go_batch(target)
        if self.pick_kind != 'pack':
            base, ext = os.path.splitext(target)
            target = base if ext.lower() in E.VIDEO_EXT else target
        for j in self.jobs:
            if j.result is None and os.path.normcase(j.m['target']) == os.path.normcase(target):
                self.flash('that one is already downloading')
                self.job, self.job_back, self.screen = j, 'results', 'job'
                return
        try:
            meta, proc = E.start_job(self.pick['r'], self.pick_kind, target, self.pick_titles, self.season_hint)
        except OSError as e:
            self.flash(f'could not start the download: {e}', 'red', 10)
            return
        job = Job(meta, proc)
        self.jobs.append(job)
        self.job, self.job_back, self.screen = job, 'results', 'job'

    def go_batch(self, show_root):
        show, tv_root = os.path.basename(show_root.rstrip('/\\')), os.path.dirname(show_root.rstrip('/\\'))
        busy = {os.path.normcase(j.m['target']) for j in self.jobs if j.result is None}
        started = 0
        for p in self.pick['batch']:
            s, e, last = E.episode_tag(p['r']['title'])
            name = self.pick_titles.get(f'{s}:{e}') or E.episode_title_from_results(self.raw, s, e)
            stem = E.episode_target(self.cfg, show, s, e, name, last, tv_root=tv_root)
            if os.path.normcase(stem) in busy:
                continue
            try:
                meta, proc = E.start_job(p['r'], 'episode', stem)
            except OSError as e:
                self.flash(f'could not start the downloads: {e}', 'red', 10)
                break
            self.jobs.append(Job(meta, proc))
            started += 1
        self.open_jobs()
        self.jobs_back = 'results'
        self.flash(f'started {started} download' + ('s' if started != 1 else '') + '; SABnzbd takes them in order', 'hi')

    def open_jobs(self):
        if self.screen not in ('jobs', 'job'):
            self.jobs_back = self.screen
        self.jsel, self.screen = 0, 'jobs'

    def jobs_sorted(self):
        return sorted(self.jobs, key=lambda j: j.m['started'], reverse=True)

    def new_search(self):
        self.edit.set('')
        self.dsel = -1
        self.screen = 'search'

    def notice_key(self, k):
        """i: search instead for what was typed. m: take the "did you mean"."""
        n = self.notice
        if not n:
            return False
        if k == 'i' and n[0] == 'corrected':
            self.notice = None
            self.plain_search(L.parse(n[2]), n[2])
            return True
        if k == 'm' and n[0] in ('maybe', 'alt'):
            self.notice = None
            self.choose(n[1], L.parse(n[2]), None)
            return True
        return False

    # -- setup

    def open_setup(self):
        c = self.cfg
        self.setup_fields = [
            ('Indexer API key', ('indexer', 'api_key'), True,
             'NZBGeek: sign in at nzbgeek.info, open your profile, copy the API key'),
            ('Indexer URL', ('indexer', 'url'), False, 'leave it for NZBGeek; any newznab indexer works'),
            ('SABnzbd URL', ('sabnzbd', 'url'), False, 'filled in by itself when SABnzbd is on this computer'),
            ('SABnzbd API key', ('sabnzbd', 'api_key'), True,
             'SABnzbd: Config (the gear), General, Security, API Key'),
            ('Movies folder', ('library', 'movies'), False, 'where finished movies go'),
            ('TV folder', ('library', 'tv'), False, 'where finished episodes go'),
        ]
        self.setup_lines = [Line(str(c[a][b]), secret) for _, (a, b), secret, _ in self.setup_fields]
        self.fsel, self.checks = 0, []
        self.setup_back = self.screen if self.screen != 'setup' else 'search'
        self.screen = 'setup'

    def setup_cfg(self):
        import copy
        cfg = copy.deepcopy(self.cfg)
        for (label, (a, b), _, _), line in zip(self.setup_fields, self.setup_lines):
            cfg[a][b] = line.t.strip()
        if cfg['indexer']['url'] and '://' not in cfg['indexer']['url']:
            cfg['indexer']['url'] = 'https://' + cfg['indexer']['url']
        if cfg['sabnzbd']['url'] and '://' not in cfg['sabnzbd']['url']:
            cfg['sabnzbd']['url'] = 'http://' + cfg['sabnzbd']['url']
        if 'nzbgeek' not in cfg['indexer']['url'].lower() and cfg['indexer']['name'] == 'NZBGeek':
            cfg['indexer']['name'] = 'Indexer'
        return cfg

    def setup_go(self, save):
        cfg = self.setup_cfg()

        def work():
            if save:
                for kind in ('movies', 'tv'):
                    os.makedirs(config.library(cfg, kind), exist_ok=True)
                config.save(cfg)
            return E.doctor(cfg)

        def then(rows):
            self.checks = rows
            if save:
                self.cfg = cfg
                self.refresh_library()
                self.check_sab()
                if not any(r[0] == 'FAIL' for r in rows):
                    self.screen = 'search'
                    self.flash(f'saved to {tilde(config.path())} · every check passed', 'hi', 8)
                else:
                    self.flash('saved, but something above needs fixing', 'amber', 8)
        self.task = Task('SAVING AND CHECKING' if save else 'CHECKING', work, then)

    # -- keys

    def key(self, k):
        if not k or k == 'RESIZE':
            return
        self.boot_t0 = 0
        if k == 'QUIT':
            self.quit = True
        elif self.task:
            if k == 'ESC':
                self.task = None
                self.flash('cancelled')
        else:
            getattr(self, 'key_' + self.screen)(k)

    def key_setup(self, k):
        n = len(self.setup_fields) + 2
        if k in ('DOWN', 'TAB'):
            self.fsel = (self.fsel + 1) % n
        elif k in ('UP', 'BTAB'):
            self.fsel = (self.fsel - 1) % n
        elif k == 'ESC':
            if config.is_complete(self.cfg):
                self.screen = self.setup_back
            else:
                self.quit = True
        elif k == 'ENTER':
            if self.fsel < len(self.setup_fields):
                self.fsel += 1
            else:
                self.setup_go(save=self.fsel == n - 1)
        elif self.fsel < len(self.setup_fields):
            self.setup_lines[self.fsel].key(k)

    def key_search(self, k):
        drop = self.dropdown()
        if k == 'ESC':
            self.quit = True
        elif k in ('TAB', 'BTAB'):
            order = ['auto', 'movie', 'tv']
            self.mode = order[(order.index(self.mode) + (1 if k == 'TAB' else -1)) % 3]
            self.dsel = -1
        elif k == 'DOWN':
            self.dsel = min(len(drop) - 1, self.dsel + 1)
        elif k == 'UP':
            self.dsel = max(-1, self.dsel - 1)
        elif k == 'ENTER':
            self.submit(drop[self.dsel] if 0 <= self.dsel < len(drop) else None)
        elif k == 'JOBS':
            self.open_jobs()
        elif k == 'SETUP':
            self.open_setup()
        elif self.edit.key(k):
            self.dsel = -1
            self.sugg['due'] = time.time() + 0.25

    def key_show(self, k):
        sh = self.show
        if self.season is None:
            n = len(sh['seasons'])
        else:
            n = len(sh['seasons'].get(self.season, [])) + 1
        if k in ('UP', 'k'):
            self.ssel = max(0, self.ssel - 1)
        elif k in ('DOWN', 'j'):
            self.ssel = min(n - 1, self.ssel + 1)
        elif k == 'PGUP':
            self.ssel = max(0, self.ssel - 10)
        elif k == 'PGDN':
            self.ssel = min(n - 1, self.ssel + 10)
        elif k in ('ENTER', 'RIGHT') and n:
            if self.season is None:
                self.season = sorted(sh['seasons'])[self.ssel]
                self.ssel = self.stop = 0
            elif self.ssel == 0:
                self.results_back = 'show'
                self.pick_season(self.season)
            else:
                self.results_back = 'show'
                self.pick_episode(self.season, sh['seasons'][self.season][self.ssel - 1]['episode'])
        elif k in ('ESC', 'LEFT', 'BS'):
            if self.season is not None:
                self.ssel = max(0, sorted(sh['seasons']).index(self.season)) if self.season in sh['seasons'] else 0
                self.season, self.stop = None, 0
            else:
                self.screen = 'search'
        elif k in ('d', 'JOBS'):
            self.open_jobs()
        elif k == 'q':
            self.quit = True
        elif k == '/':
            self.new_search()
        else:
            self.notice_key(k)

    def key_results(self, k):
        if self.filtering:
            if k == 'ENTER':
                self.filtering = False
            elif k == 'ESC':
                self.filt.set('')
                self.filtering = False
            elif self.filt.key(k):
                self.sel = self.top = 0
            return
        rows = self.visible()
        n, page = len(rows), max(1, self.list_h - 1)
        if k in ('UP', 'k'):
            self.sel = max(0, self.sel - 1)
        elif k in ('DOWN', 'j'):
            self.sel = min(n - 1, self.sel + 1)
        elif k == 'PGUP':
            self.sel = max(0, self.sel - page)
        elif k == 'PGDN':
            self.sel = min(n - 1, self.sel + page)
        elif k in ('HOME', 'g'):
            self.sel = 0
        elif k in ('END', 'G'):
            self.sel = max(0, n - 1)
        elif k == 'ENTER' and rows:
            self.open_confirm(rows[self.sel])
        elif k == 'ESC':
            if self.filt.t:
                self.filt.set('')
                self.sel = self.top = 0
            else:
                self.screen = self.results_back if self.results_back != 'show' or self.show else 'search'
        elif k == 's':
            self.sort = SORTS[(SORTS.index(self.sort) + 1) % len(SORTS)]
            self.rev, self.sel, self.top = False, 0, 0
        elif k == 'r':
            self.rev = not self.rev
            self.sel = self.top = 0
        elif k == 'f':
            self.filtering = True
        elif k == 'h':
            self.hide_junk = not self.hide_junk
            self.sel = self.top = 0
        elif k == '/':
            self.new_search()
        elif k in ('d', 'JOBS'):
            self.open_jobs()
        elif k == 'q':
            self.quit = True
        else:
            self.notice_key(k)

    def key_confirm(self, k):
        if k == 'ENTER':
            self.go()
        elif k == 'ESC':
            self.screen = 'results'
        elif k == 'JOBS':
            self.open_jobs()
        else:
            self.path.key(k)

    def key_job(self, k):
        if k in ('ESC', 'ENTER', 'LEFT'):
            back = self.job_back
            self.screen = back if back != 'results' or self.rows else 'search'
        elif k in ('d', 'JOBS'):
            self.open_jobs()
        elif k == '/':
            self.new_search()
        elif k == 'q':
            self.quit = True

    def key_jobs(self, k):
        js = self.jobs_sorted()
        if k in ('UP', 'k'):
            self.jsel = max(0, self.jsel - 1)
        elif k in ('DOWN', 'j'):
            self.jsel = min(len(js) - 1, self.jsel + 1)
        elif k in ('ENTER', 'RIGHT') and js:
            self.job, self.job_back, self.screen = js[self.jsel], 'jobs', 'job'
        elif k in ('ESC', 'LEFT'):
            self.screen = self.jobs_back
        elif k == '/':
            self.new_search()
        elif k == 'q':
            self.quit = True

    # -- drawing

    def draw(self):
        self.scr.erase()
        h, w = self.scr.getmaxyx()
        if h < 18 or w < 56:
            self.put(0, 0, 'reel needs a window of at least 56 x 18', C['amber'])
            self.scr.refresh()
            return
        self.put(0, 0, '╔' + '═' * (w - 2) + '╗', C['border'])
        for y in range(1, h - 1):
            self.put(y, 0, '║', C['border'])
            self.put(y, w - 1, '║', C['border'])
        self.put(h - 1, 0, '╚' + '═' * (w - 2) + '╝', C['border'])
        self.put(0, 3, f'╡ REEL ▸ {TITLES[self.screen]} ╞', C['hi'])
        clock = time.strftime(' %a %H:%M:%S ')
        self.put(0, w - len(clock) - 3, clock, C['dim'])
        getattr(self, 'draw_' + self.screen)(h, w)
        if self.task:
            self.draw_spinner(h, w)
        self.footer(h, w)
        self.scr.refresh()

    def footer(self, h, w):
        self.put(h - 3, 1, '─' * (w - 2), C['border'])
        live = [j for j in self.jobs if j.result is None]
        right = ''
        if live:
            j = max(live, key=lambda j: j.m['started'])
            f = j.frac()
            right = f'⇣ {len(live)} running · {clip(j.label(), 28)}' + (f' {int(f * 100)}%' if f is not None else '')
        if time.time() < self.flash_until:
            left, attr = self.flash_msg, C[self.flash_attr]
        else:
            key = 'filter' if self.screen == 'results' and self.filtering else self.screen
            left, attr = HINTS[key], C['dim']
        self.put(h - 2, 3, clip(left, w - 6 - (dwidth(right) + 3 if right else 0)), attr)
        if right:
            self.put(h - 2, w - 3 - dwidth(right), right, C['hi'])

    def draw_spinner(self, h, w):
        sp = '|/-\\'[int(time.time() * 8) % 4]
        msg = f'{sp}  {self.task.label}  {int(time.time() - self.task.t0)}s'
        bw = dwidth(msg) + 8
        y, x = h // 2 - 2, (w - bw) // 2
        for i in range(5):
            self.put(y + i, x, ' ' * bw, C['main'])
        self.box(y, x, bw, 5, C['hi'])
        self.put(y + 1, x + 4, msg, C['hi'])
        self.put(y + 3, x + 4, 'esc cancels', C['dim'])

    def draw_notice(self, y, x, w):
        """The "Showing results for" / "Did you mean" lines. Returns the next free row."""
        n = self.notice
        if not n:
            return y
        kind, c, typed = n
        if kind == 'corrected':
            self.put(y, x, 'Showing results for ', C['main'])
            self.put(y, x + 20, clip(L.label(c), w - 20), C['hi'])
            self.put(y + 1, x, clip(f'Search instead for “{typed}”  [i]', w), C['dim'])
            return y + 2
        self.put(y, x, 'Did you mean: ', C['amber'])
        self.put(y, x + 14, clip(f'{L.label(c)}?   [m]', w - 14), C['hi'])
        return y + 1

    def draw_setup(self, h, w):
        x0, cw = self.col(w, 84)
        y = 2
        self.put(y, x0, 'SETTINGS', C['hi'])
        self.put(y, x0 + cw - dwidth(tilde(config.path())), tilde(config.path()), C['dim'])
        y += 1
        self.put(y, x0, clip('reel needs an indexer to search and SABnzbd to download. Keys stay on this computer.',
                             cw), C['dim'])
        y += 2
        for i, ((label, _, _, hint), line) in enumerate(zip(self.setup_fields, self.setup_lines)):
            focus = i == self.fsel
            self.put(y, x0, ('▶ ' if focus else '  ') + label, C['hi'] if focus else C['main'])
            self.put(y, x0 + 20, '[', C['border'])
            line.draw(self, y, x0 + 21, cw - 23, C['hi'] if focus else C['main'], focus)
            self.put(y, x0 + cw - 1, ']', C['border'])
            if focus and hint:
                self.put(y + 1, x0 + 21, clip(hint, cw - 21), C['dim'])
            y += 2
        for j, label in enumerate((' TEST ', ' SAVE ')):
            focus = self.fsel == len(self.setup_fields) + j
            self.put(y, x0 + 20 + j * 12, f'[{label}]', C['sel'] if focus else C['main'])
        y += 2
        for level, what, msg in self.checks:
            if y >= h - 4:
                break
            attr = C['hi'] if level == 'OK' else C['amber'] if level == 'WARN' else C['red']
            self.put(y, x0, f'{level:<5} {what:<8}', attr)
            self.put(y, x0 + 15, clip(msg, cw - 15), C['main'])
            y += 1

    def draw_search(self, h, w):
        bw = min(w - 6, 84)
        x0 = max(3, (w - bw) // 2)
        y = 2
        if h >= 34:
            for i, ln in enumerate(BANNER):
                self.put(y + i, (w - dwidth(ln)) // 2, ln, C['hi'])
            y += len(BANNER)
            sub = '▓▒░  USENET MOVIE + TV GRABBER  ░▒▓'
            self.put(y, (w - dwidth(sub)) // 2, sub, C['main'])
            y += 2
        else:
            self.put(y, x0, '▓▒░ REEL ░▒▓  usenet movie + tv grabber', C['hi'])
            y += 2
        ix = self.cfg['indexer']
        boot = [(ix['name'].upper(), 'KEY SET' if ix['api_key'] else 'NO KEY', 'hi' if ix['api_key'] else 'red'),
                ('SABNZBD ' + self.cfg['sabnzbd']['url'].split('://')[-1], *self.sab_state),
                ('LIBRARY', f'{self.counts[0]} FILMS / {self.counts[1]} SHOWS', 'hi')]
        budget = (time.time() - self.boot_t0) * 300 if self.boot_t0 else 1e9
        for label, value, attr in boot:
            pre = f'> {label} ' + '.' * max(2, 34 - dwidth(label)) + ' '
            shown = int(min(len(pre) + len(value), max(0, budget)))
            budget -= len(pre) + len(value)
            self.put(y, x0, pre[:shown], C['dim'])
            if shown > len(pre):
                self.put(y, x0 + len(pre), value[:shown - len(pre)], C[attr])
            y += 1
        y += 1
        x = x0
        for m, lab in (('auto', '  ANY  '), ('movie', ' MOVIES '), ('tv', '   TV   ')):
            self.put(y, x, f'[{lab}]', C['sel'] if self.mode == m else C['dim'])
            x += len(lab) + 3
        self.put(y, x, 'tab switches', C['dim'])
        y += 1
        self.box(y, x0, bw, 3, C['hi'])
        self.put(y + 1, x0 + 2, '>', C['hi'])
        self.edit.draw(self, y + 1, x0 + 4, bw - 6, C['hi'])
        y += 3
        drop = self.dropdown()
        if drop:
            head = 'RECENT SEARCHES' if drop[0]['type'] == 'hist' else 'SUGGESTIONS'
            self.put(y, x0 + 1, f'{head} · ↓ to pick', C['border'])
            y += 1
            for i, item in enumerate(drop):
                if y >= h - 4:
                    break
                sel = i == self.dsel
                if item['type'] == 'hist':
                    line, extra = item['text'], ''
                else:
                    c = item['cand']
                    line = L.label(c)
                    extra = ('TV  ' if c['kind'] == 'tv' else 'FILM') + ('  ' + c['cast'] if c['cast'] else '')
                tw = min(44, bw // 2)
                if sel:
                    self.put(y, x0, pad(f' ▶ {pad(clip(line, tw), tw)}  {extra}', bw), C['sel'])
                else:
                    self.put(y, x0 + 3, clip(line, tw), C['main'])
                    self.put(y, x0 + 5 + tw, clip(extra, bw - tw - 6), C['dim'])
                y += 1
        else:
            hint = {'auto': 'a title, with a year or s01 / s01e02 if you like:  knowing 2009 · superjail s01',
                    'movie': 'a movie title, a year helps:  knowing 2009',
                    'tv': 'a show, with s01 or s01e02 if you like:  superjail s01e02'}[self.mode]
            self.put(y, x0 + 2, clip(hint, bw - 2), C['dim'])
            y += 1
        y += 1
        if self.recent and y + 1 < h - 3:
            self.put(y, x0, '── RECENTLY LANDED ' + '─' * (bw - 19), C['border'])
            y += 1
            for when, name in self.recent:
                if y >= h - 3:
                    break
                stamp = time.strftime('%d %b %H:%M', time.localtime(when))
                self.put(y, x0 + 2, clip(f'{stamp}   {name}', bw - 2), C['dim'])
                y += 1

    def draw_show(self, h, w):
        sh = self.show
        x0, cw = self.col(w, 100)
        y = 2
        years = sh['premiered'][:4] + ('-' + sh['ended'][:4] if sh['ended'] else ('-now' if sh['status'] == 'Running' else ''))
        neps = sum(len(v) for v in sh['seasons'].values())
        self.put(y, x0, clip(f"▸ {sh['name']}", cw - 30), C['hi'])
        info = f"{years} · {len(sh['seasons'])} seasons · {neps} episodes"
        self.put(y, x0 + cw - dwidth(info), info, C['dim'])
        y = self.draw_notice(y + 1, x0, cw) + 1
        today = time.strftime('%Y-%m-%d')
        if self.season is None:
            rows = []
            for s in sorted(sh['seasons']):
                eps = sh['seasons'][s]
                got = sum(1 for e in eps if (s, e['episode']) in self.have_eps)
                dates = [e['airdate'] for e in eps if e['airdate']]
                span = f'{dates[0][:4]}' + (f'-{dates[-1][:4]}' if dates and dates[-1][:4] != dates[0][:4] else '') if dates else ''
                mark = '✓ all in library' if got == len(eps) and got else (f'✓ {got} in library' if got else '')
                rows.append((f'Season {s}', f'{len(eps):>3} episodes', span, mark, False))
            self.put(y, x0, 'SEASONS', C['border'])
        else:
            eps = sh['seasons'].get(self.season, [])
            got = sum(1 for e in eps if (self.season, e['episode']) in self.have_eps)
            rows = [(f'★ Whole season {self.season}', 'a season pack, or every episode', '',
                     f'✓ {got} of {len(eps)} in library' if got else '', False)]
            for e in eps:
                unaired = e['airdate'] > today if e['airdate'] else False
                mark = '✓ in library' if (self.season, e['episode']) in self.have_eps else ('not aired' if unaired else '')
                rows.append((f"S{self.season:02d}E{e['episode']:02d}", e['title'], e['airdate'], mark, unaired))
            self.put(y, x0, f'SEASON {self.season} · ← back to seasons', C['border'])
        y += 1
        top_y, n = y, max(1, h - 4 - y)
        self.ssel = max(0, min(self.ssel, len(rows) - 1))
        if self.ssel < self.stop:
            self.stop = self.ssel
        if self.ssel >= self.stop + n:
            self.stop = self.ssel - n + 1
        for i, (a, b, c, mark, dim) in enumerate(rows[self.stop:self.stop + n]):
            yy, sel = top_y + i, self.stop + i == self.ssel
            line = f'{pad(a, 22)} {pad(b, max(10, cw - 60))} {pad(c, 11)} {pad(mark, 20)}'
            if sel:
                self.put(yy, x0, pad(' ▶ ' + line, cw), C['sel'])
            else:
                attr = C['dim'] if dim else C['main']
                self.put(yy, x0 + 3, clip(line, cw - 3), attr)
                if mark.startswith('✓'):
                    self.put(yy, x0 + 3 + dwidth(line) - 20, mark, C['hi'])
        if self.stop + n < len(rows):
            self.put(top_y + n - 1, x0 + cw, '▼', C['hi'])

    def draw_results(self, h, w):
        rows = self.visible()
        good = sum(1 for r in self.rows if r['ok'])
        kind = {'movie': 'MOVIE', 'episode': 'EPISODE', 'season': 'SEASON', 'raw': 'ANY'}[self.want['kind']]
        info = f'{kind} · {good} clean of {len(self.rows)}'
        self.put(2, 3, clip(f"▸ {self.want['label']}", w - len(info) - 10), C['hi'])
        self.put(2, w - 3 - len(info), info, C['dim'])
        y = self.draw_notice(3, 3, w - 6)
        if self.have:
            self.put(y, 3, clip(self.have, w - 6), C['amber'])
            y += 1
        x = 3
        self.put(y, x, 'SORT', C['dim'])
        x += 5
        for s in SORTS:
            on = s == self.sort
            lab = s + (' ↑' if on and self.rev else '')
            self.put(y, x, f' {lab} ', C['sel'] if on else C['dim'])
            x += dwidth(lab) + 3
        tags = []
        if self.filt.t:
            tags.append(f'FILTER “{self.filt.t}”')
        if self.hide_junk:
            tags.append('JUNK HIDDEN')
        if tags:
            self.put(y, x + 2, clip('  ·  '.join(tags), w - x - 6), C['amber'])
        y += 1
        wide, mid = w >= 118, w >= 86
        head = f'   {"STATUS":<12}' + (f'{"RES":<7}{"SOURCE":<8}{"CODEC":<6}' if wide else '') + \
            f'{"SIZE":>8}' + (f'{"AGE":>6}' if mid else '') + (f'{"GRABS":>7}' if wide else '') + '   RELEASE'
        self.put(y, 1, head, C['dim'])
        self.put(y + 1, 1, '─' * (w - 2), C['border'])
        top_y, bottom = y + 2, h - 6
        n = self.list_h = max(1, bottom - top_y + 1)
        if not rows:
            self.put(top_y, 3, 'nothing left after the filter · esc clears it' if self.filt.t or self.hide_junk
                     else 'no results', C['amber'])
        else:
            self.sel = max(0, min(self.sel, len(rows) - 1))
            if self.sel < self.top:
                self.top = self.sel
            if self.sel >= self.top + n:
                self.top = self.sel - n + 1
            for i, row in enumerate(rows[self.top:self.top + n]):
                self.draw_row(top_y + i, w, row, self.top + i == self.sel, wide, mid)
            if self.top:
                self.put(top_y, w - 2, '▲', C['hi'])
            if self.top + n < len(rows):
                self.put(bottom, w - 2, '▼', C['hi'])
        if self.filtering:
            self.put(h - 5, 3, 'filter >', C['hi'])
            self.filt.draw(self, h - 5, 12, w - 16, C['hi'])
            self.put(h - 4, 3, clip('words to keep, -word to drop:  x265 · web-dl · -hevc', w - 6), C['dim'])
        elif rows:
            row = rows[self.sel]
            self.put(h - 5, 3, clip(row['r']['title'], w - 6), C['main'])
            if row.get('batch'):
                eps = ', '.join(f"E{E.episode_tag(p['r']['title'])[1]:02d}" for p in row['batch'])
                note, attr = clip(f'{eps} · each one its own download, enter shows the list', w - 6), C['hi']
            elif row.get('have'):
                note, attr = 'you already have this episode; enter grabs it again under the same name', C['main']
            elif row.get('best'):
                note, attr = '★ the recommended pick', C['hi']
            elif row['ok']:
                note, attr = ('a clean season pack' if row['pack'] else 'a clean pick') + ' · enter grabs it', C['main']
            else:
                note, attr = f'marked “{row["why"]}”: not a clean pick, enter grabs it anyway', C['amber']
            self.put(h - 4, 3, clip(note, w - 6), attr)

    def draw_row(self, y, w, row, sel, wide, mid):
        r = row['r']
        t = r['title']
        if row.get('batch'):
            status = '★ ALL EPS' if row.get('best') else '· all eps'
        elif row.get('best'):
            status = '★ PACK' if row['pack'] else '★ BEST'
        elif row.get('have'):
            status = '✓ have'
        else:
            status = ('· pack' if row['pack'] else '· ok') if row['ok'] else row['why']
        src = row['batch'][0]['r']['title'] if row.get('batch') else t
        cols = (f'{E.resolution(src) or "?":<7}{E.source(src) or "?":<8}{E.codec(src) or "?":<6}' if wide else '') + \
            f'{E.gb(r.get("size")):>8}' + (f'{age(r.get("date")) if r.get("date") else "":>6}' if mid else '') + \
            (f'{r.get("grabs") if r.get("grabs") is not None else "":>7}' if wide else '') + '   '
        if sel:
            self.put(y, 1, pad(f' ▶ {pad(status, 12)}{cols}{t}', w - 3), C['sel'])
            return
        st_attr = C['hi'] if row.get('best') else C['main'] if row['ok'] else C['amber']
        body = C['main'] if row['ok'] else C['dim']
        self.put(y, 4, pad(status, 12), st_attr)
        self.put(y, 16, clip(cols + t, w - 19), body)

    def draw_confirm(self, h, w):
        x0, cw = self.col(w)
        r, t = self.pick['r'], self.pick['r']['title']
        y = 2
        what = {'movie': 'GRAB THIS MOVIE?', 'episode': 'GRAB THIS EPISODE?', 'pack': 'GRAB THIS SEASON PACK?',
                'batch': f"GRAB THESE {len(self.preview)} EPISODES?"}
        self.put(y, x0, what[self.pick_kind], C['hi'])
        y += 2
        if self.pick_kind == 'batch':
            self.put(y + 1, x0, 'show folder', C['dim'])
            self.box(y, x0 + 12, cw - 12, 3, C['hi'])
            self.path.draw(self, y + 1, x0 + 14, cw - 16, C['hi'])
            y += 4
            self.put(y, x0, f"{E.gb(r.get('size'))} in all, one download per episode:", C['main'])
            y += 1
            room = h - 8 - y
            for line in self.preview[:room]:
                self.put(y, x0 + 2, clip(line, cw - 2), C['dim'])
                y += 1
            if len(self.preview) > room > 0:
                self.put(y, x0 + 2, f'… and {len(self.preview) - room} more', C['dim'])
                y += 1
            self.put(y + 1, x0, '[enter] grab them all      [esc] back', C['main'])
            return
        bits = ' · '.join(b for b in (E.gb(r.get('size')), E.resolution(t), E.source(t), E.codec(t),
                                      f"{r.get('grabs') or 0} grabs", f"posted {age(r.get('date'))} ago") if b)
        for label, val in (('release', t), ('details', bits)):
            self.put(y, x0, label, C['dim'])
            self.put(y, x0 + 10, clip(val, cw - 10), C['main'])
            y += 1
        y += 1
        self.put(y + 1, x0, 'goes to' if self.pick_kind != 'pack' else 'show folder', C['dim'])
        self.box(y, x0 + 12, cw - 12, 3, C['hi'])
        self.path.draw(self, y + 1, x0 + 14, cw - 16, C['hi'])
        y += 4
        if self.pick_kind == 'pack':
            self.put(y, x0, 'each episode lands as its own file, for example:', C['main'])
            y += 1
            for p in self.preview:
                self.put(y, x0 + 2, clip(p, cw - 2), C['dim'])
                y += 1
            self.put(y, x0, "episodes you already have are kept as they are", C['dim'])
            y += 2
        if not self.pick['ok']:
            self.put(y, x0, clip(f'⚠ marked “{self.pick["why"]}”: not a clean pick. enter grabs it anyway.', cw),
                     C['amber'])
            y += 1
        if self.pick_kind != 'pack':
            stem = os.path.splitext(os.path.expanduser(self.path.t.strip()))[0]
            have = E.existing_file(stem)
            if have:
                self.put(y, x0, clip(f'✓ already there ({E.gb(os.path.getsize(have))}): grabbing just says OK. '
                                     'change the name to keep both.', cw), C['amber'])
                y += 1
        self.put(y + 1, x0, '[enter] grab it      [esc] back', C['main'])

    def draw_job(self, h, w):
        x0, cw = self.col(w)
        j, y = self.job, 2
        res = j.result
        if res is None:
            sp = '|/-\\'[int(time.time() * 4) % 4]
            self.put(y, x0, f'{sp}  {j.stage()}', C['hi'])
        elif res['result'] == 'ok':
            self.put(y, x0, '✓  LANDED IN THE LIBRARY', C['hi'])
        else:
            self.put(y, x0, '✗  FAILED', C['red'] | curses.A_BOLD)
        el = f'elapsed {fmt_dur(j.elapsed())}'
        self.put(y, x0 + cw - len(el), el, C['dim'])
        y += 2
        self.put(y, x0, clip(j.label(), cw), C['hi'])
        self.put(y + 1, x0, clip('from ' + j.m['release'], cw), C['dim'])
        y += 3
        if res is None:
            bw = max(10, min(cw - 8, 64))
            f = j.frac()
            if f is None:
                k = int(time.time() * 10) % bw
                bar, pct = ''.join('█' if (i - k) % bw < 4 else '░' for i in range(bw)), '  …'
            else:
                fill = int(f * bw)
                bar, pct = '█' * fill + '░' * (bw - fill), f'{int(f * 100):3d}%'
            self.put(y, x0, f'[{bar}] {pct}', C['hi'])
            y += 1
            st = j.state
            if st.get('stage') == 'downloading' and st.get('speed'):
                stats = f"{st['speed'] / 1e6:.1f} MB/s · {E.gb(st.get('left'))} of {E.gb(st.get('total'))} left" + \
                    (f" · eta {fmt_dur(st['eta'])}" if st.get('eta') else '')
            else:
                stats = {'STARTING': 'starting up', 'CONNECTING': 'checking SABnzbd', 'NZB': 'fetching the nzb',
                         'QUEUED': 'queued in SABnzbd', 'DOWNLOADING': 'waiting for the first bytes',
                         'PAUSED': 'paused in SABnzbd', 'PLACING': 'moving it into the library'
                         }.get(j.stage(), 'SABnzbd is checking and unpacking it')
            self.put(y, x0, stats, C['main'])
            y += 2
        elif res['result'] == 'ok':
            self.put(y, x0, clip(tilde(res.get('path', '')), cw), C['hi'])
            self.put(y + 1, x0, clip(res.get('detail', ''), cw), C['dim'])
            y += 3
        else:
            for ln in textwrap.wrap(res.get('reason', ''), cw)[:4]:
                self.put(y, x0, ln, C['red'])
                y += 1
            for ln in textwrap.wrap(res.get('hint', ''), cw)[:2]:
                self.put(y, x0, ln, C['amber'])
                y += 1
            self.put(y, x0, 'esc goes back to the list so you can pick another release', C['dim'])
            y += 2
        if y < h - 5:
            self.put(y, x0, '── LOG ' + '─' * (cw - 7), C['border'])
            y += 1
            room = h - 4 - y
            for ln in j.lines[-room:] if room > 0 else []:
                attr = C['red'] if 'FAILED' in ln else C['hi'] if '  OK ' in ln else C['dim']
                self.put(y, x0, clip(ln, cw), attr)
                y += 1

    def draw_jobs(self, h, w):
        x0, cw = self.col(w)
        js = self.jobs_sorted()
        self.put(2, x0, f'DOWNLOADS · last {JOB_DAYS} days', C['hi'])
        if not js:
            self.put(4, x0, 'nothing yet. pick a release and it shows up here.', C['dim'])
            return
        n = max(1, h - 8)
        self.jsel = max(0, min(self.jsel, len(js) - 1))
        top = max(0, self.jsel - n + 1)
        for i, j in enumerate(js[top:top + n]):
            y, sel = 4 + i, top + i == self.jsel
            icon = '⇣' if j.result is None else '✓' if j.result['result'] == 'ok' else '✗'
            when = time.strftime('%a %H:%M', time.localtime(j.m['started']))
            right = j.status()
            left = f'{"▶" if sel else " "} {icon} {when}   '
            line = left + clip(j.label(), cw - dwidth(left) - len(right) - 3)
            if sel:
                self.put(y, x0, pad(pad(line, cw - len(right)) + right, cw), C['sel'])
                continue
            attr = C['hi'] if j.result is None else C['main'] if j.result['result'] == 'ok' else C['red']
            self.put(y, x0, line, attr)
            self.put(y, x0 + cw - len(right), right, C['dim'])


def run(initial='', force_setup=False):
    locale.setlocale(locale.LC_ALL, '')
    os.environ.setdefault('ESCDELAY', '25')
    holder = {}

    def go(scr):
        holder['app'] = App(scr, initial, force_setup)
        holder['app'].run()

    try:
        curses.wrapper(go)
    except KeyboardInterrupt:
        pass
    app = holder.get('app')
    live = [j for j in app.jobs if j.result is None] if app else []
    if live:
        print(f'reel: {len(live)} download(s) still running in the background:')
        for j in live:
            print(f'  {j.label()}')
    return 0
