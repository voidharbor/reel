"""Indexer search, release grading, library names, SABnzbd, and the detached download runner.

No curses in here: the runner process and `reel --check` import this on their own.
"""
import json, math, os, re, shutil, subprocess, sys, time, urllib.error, urllib.parse, urllib.request, uuid
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime

from . import config

BROWSER_UA = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
              '(KHTML, like Gecko) Chrome/126.0 Safari/537.36')
MIN_SIZE = 50_000_000                      # bytes; smaller is a sample or a broken file
VIDEO_EXT = ('.mkv', '.mp4', '.m4v')
SUB_EXT = ('.srt', '.ass', '.ssa', '.vtt', '.sub', '.idx')
FAILED_DAYS = 30
JOB_TIMEOUT = 6 * 3600


class ReelError(Exception):
    """A problem the user can act on. The message is shown as is."""


def http(url, data=None, headers=None, timeout=30, tries=2):
    last = None
    for i in range(tries):
        try:
            req = urllib.request.Request(url, data=data, headers={'User-Agent': BROWSER_UA, **(headers or {})})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            if 400 <= e.code < 500:
                raise                      # a bad key or a 404 won't fix itself on a retry
            last = e
        except Exception as e:
            last = e
        if i + 1 < tries:
            time.sleep(2)
    raise last


def gb(n):
    if not n:
        return '?'
    return f'{n / 1e9:.1f} GB' if n >= 1e9 else f'{n / 1e6:.0f} MB'


# ---------------------------------------------------------------- release names

EP_RE = re.compile(r'(?i)\bS(\d{1,2})[ .]?E(\d{1,3})(?:-?E(\d{1,3}))?\b|\b(\d{1,2})x(\d{2,3})\b')
ALL_SEASONS = 'all'


def norm(s):
    s = s.lower().replace('&', ' and ')
    s = re.sub(r"[’'`]", '', s)
    return re.sub(r'[^a-z0-9]+', ' ', s).strip()


def words(rel):
    return re.sub(r'[._]+', ' ', rel).split()


def episode_tag(name):
    """(season, episode, last episode of a double or None), or None when there's no tag."""
    m = EP_RE.search(name)
    if not m:
        return None
    if m.group(1):
        return int(m.group(1)), int(m.group(2)), int(m.group(3)) if m.group(3) else None
    return int(m.group(4)), int(m.group(5)), None


def pack_seasons(rel):
    """The seasons a pack covers: a set, ALL_SEASONS for a complete series, or None when it isn't a pack."""
    if episode_tag(rel):
        return None
    t = ' '.join(words(rel))
    m = re.search(r'(?i)\bS(\d{1,2})\s*-\s*S?(\d{1,2})\b', t)
    if m:
        return set(range(int(m.group(1)), int(m.group(2)) + 1))
    m = re.search(r'(?i)\bSeasons?\s*(\d{1,2})(?:\s*(?:-|to)\s*(\d{1,2}))?\b', t)
    if m:
        return set(range(int(m.group(1)), int(m.group(2) or m.group(1)) + 1))
    m = re.search(r'(?i)\bS(\d{1,2})\b', t)
    if m:
        return {int(m.group(1))}
    if re.search(r'(?i)\bComplete\b', t):
        return ALL_SEASONS
    return None


def is_tv_release(rel):
    t = ' '.join(words(rel))
    return bool(episode_tag(rel) or re.search(r'(?i)\bS\d{1,2}\b|\bSeasons?\s*\d', t))


def split_movie(rel):
    """('Knowing', 2009) from Knowing.2009.1080p.BluRay...; (None, None) when there's no year."""
    w = words(rel)
    for i, t in enumerate(w):
        if i and re.fullmatch(r'\(?(19|20)\d\d\)?', t):
            return ' '.join(w[:i]), int(t.strip('()'))
    return None, None


def show_of(rel):
    """'Family Guy' from Family.Guy.S24E16... or Family.Guy.S24.1080p..."""
    m = EP_RE.search(rel) or re.search(r'(?i)\b(S\d{1,2}|Seasons?[ .]?\d|Complete)\b', rel)
    return ' '.join(words(rel[:m.start()] if m else rel)).strip(' -')


def title_matches(release, title, year=None):
    """The release name starts with the title (a leading The/A/An may be dropped) and any year in
    it is within one of the one asked for. Indexer search is fuzzy: 'The Others 2001' finds
    Band of Brothers 2001."""
    rel, want = norm(release).split(), norm(title).split()
    if not want:
        return True
    n = len(want)
    if rel[:n] == want:
        rest = rel[n:]
    elif want[0] in ('the', 'a', 'an') and n > 1 and rel[:n - 1] == want[1:]:
        rest = rel[n - 1:]
    elif rel and rel[0] in ('the', 'a', 'an') and rel[1:n + 1] == want:
        rest = rel[n + 1:]
    else:
        return False
    seq = {'2', '3', '4', '5', '6', '7', '8', '9', 'ii', 'iii', 'iv', 'v', 'vi', 'two', 'three', 'four', 'five'}
    if rest and (rest[0] in seq or (rest[0] == 'part' and len(rest) > 1 and rest[1] in seq)):
        return False           # a sequel: Grave Encounters 2 (2012) sits inside the year window of the 2011 one
    if year:
        years = [int(t) for t in rest if re.fullmatch(r'(19|20)\d\d', t)]
        if years and abs(years[0] - int(year)) > 1:
            return False
    return True


def resolution(rel):
    t = ' '.join(words(rel))
    m = re.search(r'(?i)\b(2160p|1080[pi]|720p|576p|480p)\b', t)
    if m:
        return m.group(1).lower().replace('1080i', '1080p')
    return '2160p' if re.search(r'(?i)\b(4K|UHD)\b', t) else ''


def source(rel):
    u = ' '.join(words(rel)).upper()
    if re.search(r'\bREMUX\b', u):
        return 'REMUX'
    if re.search(r'BLU-?RAY|BDRIP|BRRIP', u):
        return 'BluRay'
    if 'WEBRIP' in u:
        return 'WEBRip'
    if re.search(r'WEB-?DL|\bWEB\b', u):
        return 'WEB-DL'
    if 'HDTV' in u:
        return 'HDTV'
    return 'DVD' if 'DVD' in u else ''


def codec(rel):
    u = ' '.join(words(rel)).upper()
    if re.search(r'X265|HEVC|H 265|H265', u):
        return 'x265'
    return 'x264' if re.search(r'X264|H 264|H264|AVC', u) else ''


# ---------------------------------------------------------------- grading

# (pattern, label); a None label shows the word that matched.
EXCLUDE = [
    (r'\b(CAM|HDCAM|TS|HDTS|TELESYNC|TELECINE|TC|HDTC|SCR|SCREENER|DVDSCR|R5|WORKPRINT)\b', 'cam / screener'),
    (r'\bREMUX\b', 'remux'),
    (r'\b(BDMV|BD25|BD50|COMPLETE BLURAY|ISO|DVD5|DVD9|DVDR)\b', 'disc image'),
    (r'\bBlu-?ray\b(?!.*\bWEB).*\bAVC\b', 'disc image'),
    (r'\b(YTS|YIFY|OBFUSCATED)\b', 're-encode'),
    (r'AsRequested', 're-post'),
    (r'\b(HC|HCSUBS|HARDSUB|HARDCODED|KORSUB)\b', 'burned-in subs'),
    (r'\b(EXTRAS|BONUS|FEATURETTES?|SAMPLE|TRAILER)\b', 'extras only'),
    (r'\b(ITA|ITALIAN|MULTI|DUAL|NORDIC|FRENCH|TRUEFRENCH|VFF|VFQ|VF2|GERMAN|GER|SPANISH|LATINO|HINDI|RUS|RUSSIAN'
     r'|DUBBED|VOSTFR|HEBREW|HebDub|POLISH|PL|TURKISH|CZECH|CZ|HUNGARIAN|HUN|PORTUGUESE|PT-BR|UKR|UKRAINIAN|SWEDISH'
     r'|DANISH|FINNISH|NORWEGIAN|THAI|ARABIC|VIETNAMESE)\b', None),
]
EXCLUDE_RE = [(re.compile(p, re.I), label) for p, label in EXCLUDE]
HDR_RE = re.compile(r'(?i)\b(UHD|4K|DoVi|HDR10\+?|HDR|DV)\b')


def verdict(r, want, cfg, failed=None):
    """(ok, why). ok means it's a clean pick for what was asked; why is the short reason when not."""
    t, kind = r['title'], want['kind']
    q = cfg['quality']
    cap = q['max_gb']
    if kind == 'movie':
        if not title_matches(t, want['title'], want.get('year')):
            return False, 'other title'
        if is_tv_release(t):
            return False, 'tv release'
    elif kind in ('episode', 'season'):
        if want.get('show') and not title_matches(t, want['show']):
            return False, 'other show'
        tag, seasons = episode_tag(t), pack_seasons(t)
        s = want['season']
        if kind == 'episode':
            e = want['episode']
            if tag is None:
                return False, 'whole season' if seasons else 'no episode tag'
            last = tag[2] or tag[1]
            if tag[0] != s or not tag[1] <= e <= last:
                return False, 'other episode'
        elif tag is not None:
            if tag[0] != s:
                return False, 'other season'
        elif seasons == ALL_SEASONS or (seasons and s in seasons):
            cap = q['max_pack_gb']
        else:
            return False, 'other season' if seasons else 'no season tag'
    elif pack_seasons(t):
        cap = q['max_pack_gb']
    if failed is not None and failed.has(t):
        return False, 'failed before'
    if r.get('size') and r['size'] > cap * 1e9:
        return False, 'too big'
    flat = ' '.join(words(t))
    for rx, label in EXCLUDE_RE:
        m = rx.search(flat)
        if m:
            return False, label or m.group(0)
    pref = q['resolution']
    if pref != '2160p':
        m = HDR_RE.search(flat)
        if m:
            return False, 'UHD source' if m.group(1).upper() in ('UHD', '4K') else 'HDR'
    res = resolution(t)
    if res != pref:
        return False, res or f'no {pref}'
    return True, ''


def score(r, want):
    """Bigger is better among clean picks: BluRay over WEB-DL over the rest, x264 over x265
    (plays everywhere), and a season pack over single episodes when a whole season was asked for."""
    u = ' '.join(words(r['title'])).upper()
    s = 0
    if re.search(r'BLU-?RAY', u):
        s += 2
    elif re.search(r'\bWEB\b|WEB-?DL', u) and 'WEBRIP' not in u:
        s += 1
    if re.search(r'X265|HEVC|H 265|H265', u):
        s -= 1
    elif re.search(r'X264|H 264|H264|AVC', u):
        s += 1
    if 'REMUX' in u:
        s -= 3
    if want['kind'] == 'season':
        seasons = pack_seasons(r['title'])
        if seasons == ALL_SEASONS or (seasons and len(seasons) > 1):
            s += 2
        elif seasons:
            s += 4
    return s


def popularity(r):
    return math.log10((r.get('grabs') or 0) + 1)


class Failed:
    """Releases that died for missing articles or a bad password, remembered for 30 days."""

    def __init__(self):
        self.path = os.path.join(config.home(), 'failed-releases.json')
        try:
            with open(self.path, encoding='utf-8') as f:
                data = json.load(f)
        except (OSError, ValueError):
            data = {}
        cutoff = time.time() - FAILED_DAYS * 86400
        self.data = {k: v for k, v in data.items() if v.get('when', 0) > cutoff}

    def has(self, title):
        return title in self.data

    def add(self, title, reason):
        self.data[title] = {'when': time.time(), 'reason': reason}
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        with open(self.path, 'w', encoding='utf-8') as f:
            json.dump(self.data, f, indent=1)


def missing_articles(msg):
    return bool(re.search(r'not on your server|cannot be completed|not-complete|missing articles|out of retention',
                          msg or '', re.I))


# ---------------------------------------------------------------- indexer

def _epoch(s):
    if not s:
        return None
    try:
        return parsedate_to_datetime(s).timestamp()
    except (TypeError, ValueError, IndexError):
        return None


def indexer(cfg, t, **params):
    """One newznab API call -> releases: title, link, size, grabs, date, guid."""
    ix = cfg['indexer']
    url = ix['url'].rstrip('/')
    if not url.endswith('/api'):
        url += '/api'
    q = {'t': t, 'apikey': ix['api_key'], 'extended': 1, 'limit': 100, 'max': 100}
    q.update({k: v for k, v in params.items() if v not in (None, '')})
    if 'q' in q:
        q['q'] = re.sub(r"[’'`]", '', str(q['q']))   # NZBGeek finds nothing for "Rosemary's Baby"
    try:
        body = http(url + '?' + urllib.parse.urlencode(q), timeout=40)
    except urllib.error.HTTPError as e:
        raise ReelError(f"{ix['name']} answered HTTP {e.code}; check the indexer URL and API key")
    except Exception as e:
        raise ReelError(f"{ix['name']} didn't answer ({e})")
    try:
        root = ET.fromstring(body)
    except ET.ParseError:
        raise ReelError(f"{ix['name']} sent something that isn't search results")
    if root.tag == 'error':
        raise ReelError(f"{ix['name']} said: {root.get('description') or root.get('code')}")
    out = []
    for item in root.iter('item'):
        attrs = {a.get('name'): a.get('value') for a in item if a.tag.endswith('attr')}
        enc = item.find('enclosure')
        size = attrs.get('size') or (enc.get('length') if enc is not None else None)
        link = (enc.get('url') if enc is not None else None) or item.findtext('link') or ''
        out.append({
            'title': item.findtext('title') or '',
            'link': link,
            'size': int(size) if size and size.isdigit() else None,
            'grabs': int(attrs['grabs']) if (attrs.get('grabs') or '').isdigit() else 0,
            'date': _epoch(attrs.get('usenetdate') or item.findtext('pubDate')),
            'guid': item.findtext('guid') or link,
        })
    return out


def search(cfg, want):
    """Every release for what was asked. Exact ID searches first; text searches as the fallback,
    or merged in for seasons, where packs often only show up under a text search."""
    kind = want['kind']
    merge = False
    if kind == 'movie':
        plans = []
        if want.get('imdb'):
            plans.append(('movie', {'imdbid': want['imdb'][2:] if want['imdb'].startswith('tt') else want['imdb']}))
        plans.append(('search', {'q': f"{want['title']} {want.get('year') or ''}".strip()}))
    elif kind == 'episode':
        s, e, show = want['season'], want['episode'], want['show']
        plans = [('tvsearch', {'tvmazeid': want['tvmaze'], 'season': s, 'ep': e})] if want.get('tvmaze') else []
        plans += [('tvsearch', {'q': show, 'season': s, 'ep': e}), ('search', {'q': f'{show} S{s:02d}E{e:02d}'})]
    elif kind == 'season':
        s, show = want['season'], want['show']
        plans = [('tvsearch', {'tvmazeid': want['tvmaze'], 'season': s})] if want.get('tvmaze') else \
            [('tvsearch', {'q': show, 'season': s})]
        plans += [('search', {'q': f'{show} S{s:02d}'}), ('search', {'q': f'{show} complete'})]
        merge = True
    else:
        plans = [('search', {'q': want['text']})]
    seen, out = set(), []
    for t, params in plans:
        for r in indexer(cfg, t, **params):
            key = r['guid'] or (r['title'], r['size'])
            if key not in seen:
                seen.add(key)
                out.append(r)
        if out and not merge:
            break
    return out


# ---------------------------------------------------------------- library names

BAD_CHARS = re.compile(r'[<>"|?*\x00-\x1f]')


def clean(s):
    """A name that is legal on Windows, macOS and Linux alike."""
    s = str(s).replace(':', ' -').replace('/', '-').replace('\\', '-')
    s = BAD_CHARS.sub('', s)
    return re.sub(r'\s+', ' ', s).strip().rstrip('. ')


def render(template, **fields):
    """A naming template -> path parts. Fields are cleaned first, so an AC/DC title can't add a folder."""
    fields = {k: clean(v) if isinstance(v, str) else v for k, v in fields.items()}
    parts = []
    for p in template.format(**fields).split('/'):
        p = re.sub(r'\s*\(\s*\)', '', p)               # "Title ()" when there's no year
        p = re.sub(r'\s+-\s*$', '', p).strip()        # "S01E02 -" when there's no episode title
        if p:
            parts.append(p)
    return parts


def movie_target(cfg, title, year=None, res=''):
    """Path without the extension; the extension follows the real file."""
    return os.path.join(config.library(cfg, 'movies'),
                        *render(cfg['naming']['movie'], title=title, year=year or '', res=res or ''))


def sxxexx(s, e, last=None):
    return f'S{s:02d}E{e:02d}' + (f'-E{last:02d}' if last and last != e else '')


def episode_target(cfg, show, s, e, title='', last=None, tv_root=None):
    return os.path.join(tv_root or config.library(cfg, 'tv'),
                        *render(cfg['naming']['episode'], show=show, season=s, episode=e,
                                sxxexx=sxxexx(s, e, last), episode_title=title or ''))


def show_dir_name(cfg, show):
    """The existing TV folder for this show, so new episodes land beside the old ones."""
    root = config.library(cfg, 'tv')
    try:
        for n in os.listdir(root):
            if norm(n) == norm(show) and os.path.isdir(os.path.join(root, n)):
                return n
    except OSError:
        pass
    return clean(show)


def episode_title_from_name(name, s, e):
    """'Combaticus' from Superjail.S01E02.Combaticus.720p.WEB-DL..."""
    pat = re.compile(rf'(?:S{s:02d}E{e:02d}(?:-?E\d+)?|{s}x{e:02d})\s*[.\-_ ]+\s*([A-Za-z][A-Za-z0-9 .&\'!,-]*?)'
                     r'(?:\s*[.\- ]\s*(?:1080[pi]|720p|2160p|480p|BluRay|HDTV|WEB|AMZN|NF|DSNP|HMAX|x264|x265|H 26|AAC|DD|PROPER|REPACK)|$)',
                     re.I)
    mm = pat.search(os.path.splitext(name)[0])
    if not mm:
        return ''
    t = re.sub(r'\s+', ' ', mm.group(1).replace('.', ' ')).strip(' .-_')
    return '' if re.fullmatch(r'(?i)(repack|proper|internal|uncut|extended|multi)?', t) else t


def episode_title_from_results(results, s, e):
    for r in results:
        t = episode_title_from_name(r['title'], s, e)
        if t:
            return t
    return ''


def container(path):
    """'mkv' | 'mp4' | None, from the file header (release names lie about the container)."""
    try:
        with open(path, 'rb') as f:
            head = f.read(12)
    except OSError:
        return None
    if head[:4] == b'\x1a\x45\xdf\xa3':
        return 'mkv'
    if head[4:8] == b'ftyp':
        return 'mp4'
    return None


def existing_file(stem):
    """The finished video at this path stem, whichever container it landed in, or None."""
    for ext in VIDEO_EXT:
        p = stem + ext
        try:
            if os.path.getsize(p) >= MIN_SIZE and container(p):
                return p
        except OSError:
            pass
    return None


def episodes_on_disk(show_root):
    """{(season, episode)} already under a show folder."""
    have = set()
    for dirpath, _, files in os.walk(show_root):
        for f in files:
            if f.lower().endswith(VIDEO_EXT):
                tag = episode_tag(f)
                if tag:
                    for e in range(tag[1], (tag[2] or tag[1]) + 1):
                        have.add((tag[0], e))
    return have


def landed_at(path):
    st = os.stat(path)
    return max(st.st_mtime, st.st_ctime)    # unrar keeps the archive's old mtime; the move sets ctime


def recent(cfg, n=5):
    items = []
    for kind in ('movies', 'tv'):
        for dirpath, _, files in os.walk(config.library(cfg, kind)):
            for f in files:
                if f.lower().endswith(VIDEO_EXT):
                    try:
                        items.append((landed_at(os.path.join(dirpath, f)), os.path.splitext(f)[0]))
                    except OSError:
                        pass
    return sorted(items, reverse=True)[:n]


def library_counts(cfg):
    movies = shows = 0
    for dirpath, _, files in os.walk(config.library(cfg, 'movies')):
        movies += sum(1 for f in files if f.lower().endswith(VIDEO_EXT))
    try:
        root = config.library(cfg, 'tv')
        shows = sum(1 for n in os.listdir(root) if os.path.isdir(os.path.join(root, n)))
    except OSError:
        pass
    return movies, shows


# ---------------------------------------------------------------- sabnzbd

class Sab:
    def __init__(self, cfg):
        self.url = cfg['sabnzbd']['url'].rstrip('/')
        self.key = cfg['sabnzbd']['api_key']

    def api(self, mode, timeout=15, **kw):
        q = {'mode': mode, 'output': 'json', 'apikey': self.key}
        q.update(kw)
        data = json.loads(http(f'{self.url}/api?' + urllib.parse.urlencode(q), timeout=timeout, tries=1))
        if isinstance(data, dict) and data.get('status') is False and data.get('error'):
            raise ReelError(f"SABnzbd said: {data['error']}")
        return data

    def version(self):
        try:
            return self.api('version', timeout=5).get('version')
        except Exception:
            return None

    def categories(self):
        return self.api('get_cats').get('categories') or []

    def add(self, nzb, name, cat, **extra):
        """Upload the NZB itself (works even when SABnzbd can't see our folders)."""
        boundary = uuid.uuid4().hex
        body = (f'--{boundary}\r\nContent-Disposition: form-data; name="name"; filename="{name}.nzb"\r\n'
                f'Content-Type: application/x-nzb\r\n\r\n').encode() + nzb + f'\r\n--{boundary}--\r\n'.encode()
        q = {'mode': 'addfile', 'output': 'json', 'apikey': self.key, 'nzbname': name}
        if cat:
            q['cat'] = cat
        q.update(extra)
        data = json.loads(http(f'{self.url}/api?' + urllib.parse.urlencode(q), data=body, timeout=60, tries=1,
                               headers={'Content-Type': f'multipart/form-data; boundary={boundary}'}))
        if not data.get('status') or not data.get('nzo_ids'):
            raise ReelError(f'SABnzbd refused the NZB: {data.get("error") or data}')
        return data['nzo_ids'][0]

    def delete(self, nzo):
        self.api('queue', name='delete', value=nzo, del_files=1)

    def queue_slot(self, nzo):
        q = self.api('queue', nzo_ids=nzo).get('queue', {})
        return q, next((s for s in q.get('slots', []) if s.get('nzo_id') == nzo), None)

    def history_slot(self, nzo=None, name=None):
        kw = {'nzo_ids': nzo} if nzo else {'search': name, 'limit': 20}
        slots = self.api('history', **kw).get('history', {}).get('slots', [])
        if nzo:
            return next((s for s in slots if s.get('nzo_id') == nzo), None)
        return next((s for s in slots if s.get('name') == name and s.get('status') == 'Completed'), None)


# ---------------------------------------------------------------- the runner (a detached process)

def emit(**kw):
    kw['t'] = round(time.time(), 1)
    print(json.dumps(kw), flush=True)


def slug(s):
    return re.sub(r'[^a-z0-9]+', '_', s.lower()).strip('_')[:80]


def jobs_dir():
    return os.path.join(config.home(), 'jobs')


def run_job(spec_path):
    """Entry point of `reel --run-job`: one release in, one verdict line out."""
    with open(spec_path, encoding='utf-8') as f:
        spec = json.load(f)
    try:
        os.remove(spec_path)       # it holds the indexer link with the API key in it
    except OSError:
        pass
    try:
        emit(result='ok', **_run(config.load(), spec))
        return 0
    except ReelError as e:
        emit(result='fail', reason=str(e))
    except Exception as e:
        emit(result='fail', reason=f'{type(e).__name__}: {e}')
    return 1


def _run(cfg, spec):
    rel, kind, target = spec['release'], spec['kind'], spec['target']
    if kind != 'pack':
        have = existing_file(target)
        if have:
            return {'path': have, 'detail': f'{gb(os.path.getsize(have))}, already there'}
    sab = Sab(cfg)
    emit(stage='connecting', msg=f'checking SABnzbd at {sab.url}')
    if not sab.version():
        raise ReelError(f'SABnzbd is not answering at {sab.url}. Start SABnzbd, then pick the release again.')
    name = slug(f"{os.path.basename(target)} {rel['title']}" if kind == 'pack' else os.path.basename(target))
    storage = None
    done = sab.history_slot(name=name)
    if done and done.get('storage') and os.path.exists(done['storage']):
        emit(stage='placing', msg='SABnzbd already finished this one; picking it up')
        storage = done['storage']
    else:
        emit(stage='nzb', msg=f"fetching the nzb for {rel['title']}")
        try:
            nzb = http(rel['link'], timeout=90)
        except Exception as e:
            raise ReelError(f"the indexer wouldn't hand over the nzb ({e})")
        if b'<nzb' not in nzb[:4096]:
            why = re.search(rb'description="([^"]+)"', nzb[:2000])
            raise ReelError('the indexer sent an error instead of an nzb'
                            + (f": {why.group(1).decode('utf-8', 'replace')}" if why else ''))
        if re.search(rb'<meta\s+type="password">\s*\S', nzb[:8192], re.I):
            emit(msg='the nzb carries an archive password; SABnzbd unpacks with it')
        nzo = sab.add(nzb, name, config.category(cfg, 'movies' if kind == 'movie' else 'tv'))
        emit(stage='queued', msg=f'queued in SABnzbd as {name}', nzo=nzo)
        storage = wait(sab, nzo, rel)
    emit(stage='placing', msg='moving it into the library')
    if kind == 'pack':
        return place_pack(cfg, storage, target, spec.get('titles') or {}, spec.get('season_hint'), job=name)
    final = move_video(cfg, storage, target, job=name)
    return {'path': final, 'detail': f'{gb(os.path.getsize(final))}, {container(final)} checked'}


def wait(sab, nzo, rel):
    t0, last, beat, empty_since = time.time(), None, 0, None
    while time.time() - t0 < JOB_TIMEOUT:
        q, slot = sab.queue_slot(nzo)
        line = None
        if slot:
            labels = ' '.join(str(x) for x in slot.get('labels') or []).upper()
            if slot.get('status') == 'Paused' and not q.get('paused') and 'ENCRYPTED' in labels:
                sab.delete(nzo)
                Failed().add(rel['title'], 'encrypted')
                raise ReelError('the archive is password protected and the nzb password did not open it. '
                                'Pick another release.')
            mb, left = float(slot.get('mb') or 0), float(slot.get('mbleft') or 0)
            speed = float(q.get('kbpersec') or 0) * 1024
            status = (slot.get('status') or 'Queued').lower()
            line = dict(stage='downloading' if status == 'downloading' else status,
                        pct=round(float(slot.get('percentage') or 0), 1), total=mb * 1e6, left=left * 1e6,
                        speed=speed, eta=left * 1e6 / speed if speed else None)
        else:
            h = sab.history_slot(nzo)
            if h:
                status = h.get('status') or ''
                if status == 'Completed':
                    storage = h.get('storage')
                    if storage and os.path.exists(storage):
                        return storage
                    empty_since = empty_since or time.time()   # SABnzbd flips the status before it fills the path
                    if time.time() - empty_since > 120:
                        raise ReelError(f'SABnzbd finished but the files are not where it says ({storage})')
                elif status == 'Failed':
                    msg = h.get('fail_message') or 'failed without a reason'
                    if missing_articles(msg):
                        Failed().add(rel['title'], msg)
                    raise ReelError(f'SABnzbd: {msg}. Pick another release.')
                else:
                    line = dict(stage=status.lower(), msg=h.get('action_line') or '')
        if line and (line != last or time.time() - beat > 10):
            emit(**line)
            last, beat = line, time.time()
        time.sleep(2)
    raise ReelError(f'not finished after {JOB_TIMEOUT // 3600} hours. It keeps going in SABnzbd; '
                    'pick the same release again later and reel picks it up.')


def walk_files(root):
    if os.path.isfile(root):
        return [root]
    out = []
    for dirpath, _, files in os.walk(root):
        out += [os.path.join(dirpath, f) for f in files]
    return out


def _videos(storage):
    vids = []
    for p in walk_files(storage):
        n = os.path.basename(p)
        try:
            big = os.path.getsize(p) >= MIN_SIZE
        except OSError:
            continue
        if n.lower().endswith(VIDEO_EXT) and big and not re.search(r'(?i)\bsample\b', n):
            vids.append(p)
    return vids


def _safe_to_remove(cfg, path):
    """Only a job folder SABnzbd made: never a library root, never a folder above one, never home."""
    p = os.path.abspath(path)
    guarded = [os.path.abspath(os.path.expanduser('~')), config.library(cfg, 'movies'), config.library(cfg, 'tv')]
    if any(p == g or g.startswith(p + os.sep) for g in guarded):
        return False
    return os.path.isdir(p) and p.count(os.sep) >= 3


def _tidy(cfg, storage, job=None):
    """Remove SABnzbd's job folder once nothing playable is left in it. SABnzbd reports either the
    folder or the video inside it; a parent folder only counts when it carries our job name, so a
    flat completed-downloads folder is never touched."""
    if os.path.isdir(storage):
        job_dir = storage
    else:
        job_dir = os.path.dirname(storage)
        if not job or not os.path.basename(job_dir).lower().startswith(job.lower()):
            return
    if _safe_to_remove(cfg, job_dir) and not _videos(job_dir):
        shutil.rmtree(job_dir, ignore_errors=True)


def _move_subs(src_video, final_stem):
    d, stem = os.path.dirname(src_video), os.path.splitext(os.path.basename(src_video))[0]
    try:
        names = os.listdir(d)
    except OSError:
        return
    for n in names:
        if n.lower().endswith(SUB_EXT) and n.startswith(stem):
            shutil.move(os.path.join(d, n), final_stem + n[len(stem):])


def move_video(cfg, storage, stem, job=None):
    """Put the finished download at stem + its real extension. Returns the final path."""
    if not storage or not os.path.exists(storage):
        raise ReelError(f'the download finished but its files are gone ({storage})')
    vids = _videos(storage)
    if not vids:
        raise ReelError('the release unpacked without a video file. Pick another release.')
    src = max(vids, key=os.path.getsize)
    kind = container(src)
    if not kind:
        raise ReelError(f'{os.path.basename(src)} is not a playable mkv or mp4. Pick another release.')
    final = f'{stem}.{kind}'
    os.makedirs(os.path.dirname(final), exist_ok=True)
    for ext in VIDEO_EXT:
        if os.path.exists(stem + ext):
            os.remove(stem + ext)
    shutil.move(src, final)
    _move_subs(src, stem)
    _tidy(cfg, storage, job)
    return final


def place_pack(cfg, storage, show_root, titles, season_hint=None, job=None):
    """Spread a season pack into Season folders, one properly named file per episode.
    Episodes already in the library are kept; files with no readable episode number stay put."""
    if not storage or not os.path.exists(storage):
        raise ReelError(f'the download finished but its files are gone ({storage})')
    show = os.path.basename(show_root.rstrip('/\\'))
    tv_root = os.path.dirname(show_root.rstrip('/\\'))
    have = episodes_on_disk(show_root)
    placed, kept, stray = [], [], []
    for v in sorted(_videos(storage)):
        n = os.path.basename(v)
        tag = episode_tag(n)
        if not tag and season_hint:
            m = re.search(r'(?i)\b(?:E|EP|Episode[ .]?)(\d{1,3})\b', ' '.join(words(n)))
            tag = (int(season_hint), int(m.group(1)), None) if m else None
        kind = container(v)
        if not tag or not kind:
            stray.append(n)
            continue
        s, e, last = tag
        if (s, e) in have:
            kept.append(n)
            continue
        title = titles.get(f'{s}:{e}') or episode_title_from_name(n, s, e)
        stem = episode_target(cfg, show, s, e, title, last, tv_root=tv_root)
        os.makedirs(os.path.dirname(stem), exist_ok=True)
        shutil.move(v, f'{stem}.{kind}')
        _move_subs(v, stem)
        have.add((s, e))
        placed.append(n)
    if not placed and not kept:
        raise ReelError(f'no file in the pack had an episode number reel could read; they are still in {storage}')
    if not stray:
        _tidy(cfg, storage, job)
    detail = f'{len(placed)} episode' + ('s' if len(placed) != 1 else '') + ' placed'
    if kept:
        detail += f', {len(kept)} you already had'
    if stray:
        detail += f', {len(stray)} unreadable left in {storage}'
    return {'path': show_root, 'detail': detail}


# ---------------------------------------------------------------- starting and watching jobs

def pid_alive(pid):
    if not pid:
        return False
    if sys.platform == 'win32':
        import ctypes
        k = ctypes.windll.kernel32
        h = k.OpenProcess(0x1000, False, int(pid))         # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return False
        code = ctypes.c_ulong()
        ok = k.GetExitCodeProcess(h, ctypes.byref(code))
        k.CloseHandle(h)
        return bool(ok) and code.value == 259              # STILL_ACTIVE
    try:
        os.kill(int(pid), 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def start_job(release, kind, target, titles=None, season_hint=None):
    """Write the spec, launch `python -m reel --run-job` fully detached, return (meta, Popen)."""
    os.makedirs(jobs_dir(), exist_ok=True)
    jid = time.strftime('%Y%m%d-%H%M%S-') + uuid.uuid4().hex[:6]
    base = os.path.join(jobs_dir(), jid)
    spec = {'release': {k: release.get(k) for k in ('title', 'link', 'size')}, 'kind': kind, 'target': target,
            'titles': titles or {}, 'season_hint': season_hint}
    with open(base + '.spec', 'w', encoding='utf-8') as f:
        json.dump(spec, f)
    if sys.platform != 'win32':
        os.chmod(base + '.spec', 0o600)
    pkg_parent = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    env = dict(os.environ, PYTHONPATH=pkg_parent + os.pathsep + os.environ.get('PYTHONPATH', ''),
               PYTHONIOENCODING='utf-8')
    kw = {'start_new_session': True}
    if sys.platform == 'win32':
        kw = {'creationflags': 0x00000008 | 0x00000200}     # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    with open(base + '.log', 'w', encoding='utf-8') as log:
        proc = subprocess.Popen([sys.executable, '-m', 'reel', '--run-job', base + '.spec'], stdin=subprocess.DEVNULL,
                                stdout=log, stderr=subprocess.STDOUT, cwd=os.path.expanduser('~'), env=env, **kw)
    meta = {'id': jid, 'release': release['title'], 'size': release.get('size'), 'kind': kind, 'target': target,
            'pid': proc.pid, 'started': time.time(), 'log': base + '.log'}
    with open(base + '.json', 'w', encoding='utf-8') as f:
        json.dump(meta, f)
    return meta, proc


# ---------------------------------------------------------------- doctor

def doctor(cfg):
    """[(level, what, message)] with level OK, WARN or FAIL. Used by `reel --check` and the setup screen."""
    out = []
    ix = cfg['indexer']
    if not ix['api_key']:
        out.append(('FAIL', 'indexer', f"no {ix['name']} API key yet (run reel --setup)"))
    else:
        try:
            indexer(cfg, 'search', q='big buck bunny', limit=1)
            out.append(('OK', 'indexer', f"{ix['name']} accepted the API key"))
        except ReelError as e:
            out.append(('FAIL', 'indexer', str(e)))
    sab = Sab(cfg)
    v = sab.version()
    if not v:
        out.append(('FAIL', 'sabnzbd', f'nothing answering at {sab.url}; is SABnzbd running?'))
    else:
        try:
            cats = sab.categories()
            out.append(('OK', 'sabnzbd', f'SABnzbd {v} at {sab.url} accepted the API key'))
            for kind in ('movies', 'tv'):
                cat = config.category(cfg, kind)
                if cat and cat not in cats:
                    out.append(('WARN', 'sabnzbd', f'no "{cat}" category in SABnzbd; {kind} use its default '
                                                   'download folder (fine, or add the category)'))
                    break
        except ReelError as e:
            out.append(('FAIL', 'sabnzbd', f'SABnzbd {v} is up but {e}'))
        except Exception as e:
            out.append(('FAIL', 'sabnzbd', f'SABnzbd {v} is up but the API call failed ({e})'))
    for kind in ('movies', 'tv'):
        p = config.library(cfg, kind)
        if not os.path.isdir(p):
            out.append(('WARN', kind, f'{p} does not exist yet (reel makes it on the first download)'))
        elif not os.access(p, os.W_OK):
            out.append(('FAIL', kind, f'{p} is not writable'))
        else:
            out.append(('OK', kind, p))
    return out
