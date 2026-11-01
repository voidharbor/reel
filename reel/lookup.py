"""Title suggestions (IMDb's public search-box feed), episode lists (TVmaze), and the spelling
fixer behind "Showing results for" and "Did you mean". All free, no keys. Every call fails soft:
no network means no suggestions, never a crash."""
import difflib, json, math, re, threading, urllib.error, urllib.parse

from .engine import EP_RE, episode_tag, http, norm

IMDB = 'https://v3.sg.media-imdb.com/suggestion/x/{}.json'
TVMAZE = 'https://api.tvmaze.com'
KINDS = {'movie': 'movie', 'tvMovie': 'movie', 'tvSeries': 'tv', 'tvMiniSeries': 'tv'}
CORRECT = 0.85     # this close to a real title: search it and say "Showing results for"
MAYBE = 0.6        # this close: search what was typed and offer "Did you mean"

_cache, _lock = {}, threading.Lock()


def _cached(key, fn):
    with _lock:
        if key in _cache:
            return _cache[key]
    val = fn()
    with _lock:
        _cache[key] = val
    return val


def label(c):
    return f"{c['title']} ({c['year']})" if c.get('year') else c['title']


# ---------------------------------------------------------------- typed text

YEAR_RE = re.compile(r'^(.*?)[\s(]*\b((?:19|20)\d\d)\)?$')
SEASON_RE = re.compile(r'(?i)\b(?:S(\d{1,2})|season\s*(\d{1,2}))\s*$')


def parse(text):
    """'superjail s01e02' -> title + season + episode; 'knowing 2009' -> title + year.
    Tags only count at the end, so '2001 a space odyssey' keeps its 2001."""
    t = text.strip()
    m = EP_RE.search(t)
    if m and not t[m.end():].strip():
        tag = episode_tag(m.group(0))
        return {'title': t[:m.start()].strip(' -'), 'year': None, 'season': tag[0], 'episode': tag[1]}
    m = SEASON_RE.search(t)
    if m:
        return {'title': t[:m.start()].strip(' -'), 'year': None,
                'season': int(m.group(1) or m.group(2)), 'episode': None}
    m = YEAR_RE.match(t)
    if m and m.group(1).strip():
        return {'title': m.group(1).strip(), 'year': int(m.group(2)), 'season': None, 'episode': None}
    return {'title': t, 'year': None, 'season': None, 'episode': None}


# ---------------------------------------------------------------- sources

def suggest(text):
    """IMDb titles for what's typed so far: movies and series only, most relevant first."""
    q = re.sub(r'\s+', ' ', text.strip().lower())
    if len(q) < 2:
        return []

    def fetch():
        data = json.loads(http(IMDB.format(urllib.parse.quote(q)), timeout=6, tries=1))
        out = []
        for d in data.get('d', []):
            kind = KINDS.get(d.get('qid'))
            if kind and d.get('l') and str(d.get('id', '')).startswith('tt'):
                out.append({'imdb': d['id'], 'title': d['l'], 'year': d.get('y'), 'years': d.get('yr') or '',
                            'kind': kind, 'cast': d.get('s') or '', 'rank': d.get('rank') or 10 ** 7})
        return out
    try:
        return _cached(('imdb', q), fetch)
    except Exception:
        return []


def tv_search(name):
    """TVmaze's fuzzy show search; it forgives typos IMDb's prefix search doesn't."""
    def fetch():
        data = json.loads(http(f'{TVMAZE}/search/shows?q={urllib.parse.quote(name)}', timeout=6, tries=1))
        out = []
        for x in data[:5]:
            s = x.get('show') or {}
            prem = s.get('premiered') or ''
            out.append({'imdb': (s.get('externals') or {}).get('imdb'), 'tvmaze': s.get('id'), 'title': s.get('name'),
                        'year': int(prem[:4]) if prem[:4].isdigit() else None, 'years': '', 'kind': 'tv',
                        'cast': '', 'rank': 10 ** 6})
        return [c for c in out if c['title']]
    try:
        return _cached(('tvmaze-search', norm(name)), fetch)
    except Exception:
        return []


def tv_show(imdb=None, tvmaze=None, name=None):
    """Seasons and episodes for a show, or None when TVmaze doesn't know it. Raises when TVmaze is down."""
    def fetch():
        try:
            if tvmaze:
                show = json.loads(http(f'{TVMAZE}/shows/{tvmaze}', timeout=8, tries=1))
            elif imdb:
                show = json.loads(http(f'{TVMAZE}/lookup/shows?imdb={imdb}', timeout=8, tries=1))
            else:
                show = json.loads(http(f'{TVMAZE}/singlesearch/shows?q={urllib.parse.quote(name)}', timeout=8, tries=1))
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            raise
        eps = json.loads(http(f"{TVMAZE}/shows/{show['id']}/episodes", timeout=10, tries=1))
        seasons = {}
        for e in eps:
            if e.get('season') is not None and e.get('number') is not None:    # specials have no number
                seasons.setdefault(e['season'], []).append(
                    {'episode': e['number'], 'title': e.get('name') or '', 'airdate': e.get('airdate') or ''})
        prem = show.get('premiered') or ''
        return {'tvmaze': show['id'], 'imdb': (show.get('externals') or {}).get('imdb'), 'name': show['name'],
                'year': int(prem[:4]) if prem[:4].isdigit() else None, 'premiered': prem,
                'ended': show.get('ended') or '', 'status': show.get('status') or '', 'seasons': seasons}
    return _cached(('tvmaze', tvmaze, imdb, norm(name or '')), fetch)


def episode_titles(show):
    return {f"{s}:{e['episode']}": e['title'] for s, eps in (show or {}).get('seasons', {}).items() for e in eps}


# ---------------------------------------------------------------- spelling

def _bare(s):
    return re.sub(r'^(the|a|an) ', '', norm(s))


def similarity(typed, title):
    a, b = norm(typed), norm(title)
    best = difflib.SequenceMatcher(None, a, b).ratio()
    return max(best, difflib.SequenceMatcher(None, _bare(typed), _bare(title)).ratio())


def _ranked(p, cands, kind):
    """(score, sim, candidate) best first. Similarity to what was typed leads; the year, the
    kind and IMDb popularity break ties."""
    merged = {}
    for c in cands:
        key = c.get('imdb') or ('tvmaze', c.get('tvmaze'))
        if key in merged:
            merged[key] = {**c, **{k: v for k, v in merged[key].items() if v}}    # IMDb fields win, TVmaze adds its id
        else:
            merged[key] = c
    out = []
    for c in merged.values():
        sim = similarity(p['title'], c['title'])
        s = sim
        if p['year'] and c.get('year'):
            s += 0.1 if abs(int(c['year']) - p['year']) <= 1 else -0.25
        if kind and c['kind'] != kind:
            s -= 0.3
        s += 0.06 * max(0.0, 1 - math.log10(max(1, c.get('rank') or 10 ** 7)) / 7)
        out.append((s, sim, c))
    out.sort(key=lambda x: -x[0])
    return out


def resolve(text, mode='auto'):
    """What the typed text most likely means.

    Returns (parsed, candidate, how, alternative): how is 'exact' (search it, say nothing),
    'corrected' (search the candidate: "Showing results for"), 'maybe' (search what was typed,
    offer "Did you mean"), or None. alternative is a same-name title worth offering."""
    p = parse(text)
    if len(norm(p['title'])) < 2:
        return p, None, None, None
    kind = 'tv' if p['season'] is not None else (mode if mode in ('movie', 'tv') else None)
    cands = list(suggest(p['title']))
    ranked = _ranked(p, cands, kind)
    if kind != 'movie' and (not ranked or ranked[0][1] < 0.95):
        cands += tv_search(p['title'])
        ranked = _ranked(p, cands, kind)
    title = norm(p['title'])
    for cut in (1, 2, 3):      # IMDb matches prefixes: "superjial" finds nothing, "superj" finds Superjail!
        if ranked and ranked[0][1] >= CORRECT or len(title) - cut < 4:
            break
        cands += suggest(title[:-cut])
        ranked = _ranked(p, cands, kind)
    if not ranked:
        return p, None, None, None
    score, sim, c = ranked[0]
    same = {norm(c['title']), norm(p['title'])}      # The Office (2005) vs (2001); Sopranos the film vs the show
    alt = None if p['year'] else next((x[2] for x in ranked[1:] if norm(x[2]['title']) in same), None)
    if _bare(p['title']) == _bare(c['title']) and (not p['year'] or not c.get('year')
                                                    or abs(int(c['year']) - p['year']) <= 1):
        return p, c, 'exact', alt
    if sim >= CORRECT:
        return p, c, 'corrected', alt
    if sim >= MAYBE:
        return p, c, 'maybe', None
    return p, None, None, None
