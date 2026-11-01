"""The settings file: where it lives, what's in it, and finding SABnzbd's details on its own."""
import copy, json, os, sys

DEFAULTS = {
    'indexer': {
        'name': 'NZBGeek',
        'url': 'https://api.nzbgeek.info/api',
        'api_key': '',
    },
    'sabnzbd': {
        'url': 'http://127.0.0.1:8080',
        'api_key': '',
        'category': 'reel',          # its own category, so Sonarr/Radarr never fight reel over a download
        'category_movies': '',       # optional overrides of `category`
        'category_tv': '',
    },
    'library': {
        'movies': '~/Media/Movies',
        'tv': '~/Media/TV',
    },
    'naming': {
        # {title} {year} {res}; a "/" makes a folder. The extension follows the real file.
        'movie': '{title} ({year})/{title} ({year})',
        # {show} {season} {episode} {sxxexx} {episode_title}
        'episode': '{show}/Season {season:02d}/{show} {sxxexx} - {episode_title}',
    },
    'quality': {
        'resolution': '1080p',       # the one the recommended pick looks for: 720p, 1080p or 2160p
        'max_gb': 30,                # bigger single releases are box sets or disc images
        'max_pack_gb': 250,          # season packs get more room
    },
    'lookups': True,                 # IMDb title suggestions and TVmaze episode lists
}


def home():
    """Folder for config.json, job logs, search history and the failed-release list."""
    if os.environ.get('REEL_HOME'):
        return os.environ['REEL_HOME']
    if sys.platform == 'win32':
        return os.path.join(os.environ.get('APPDATA') or os.path.expanduser('~'), 'reel')
    return os.path.join(os.environ.get('XDG_CONFIG_HOME') or os.path.expanduser('~/.config'), 'reel')


def path():
    return os.path.join(home(), 'config.json')


def _merge(base, extra):
    for k, v in extra.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _merge(base[k], v)
        else:
            base[k] = v
    return base


def load():
    """Defaults, then config.json on top, then SABnzbd's own ini for anything still blank."""
    cfg = copy.deepcopy(DEFAULTS)
    try:
        with open(path(), encoding='utf-8') as f:
            _merge(cfg, json.load(f))
    except (OSError, ValueError):
        pass
    if not cfg['sabnzbd']['api_key']:
        found = detect_sabnzbd()
        if found:
            cfg['sabnzbd']['url'], cfg['sabnzbd']['api_key'] = found
    return cfg


def save(cfg):
    os.makedirs(home(), exist_ok=True)
    tmp = path() + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(cfg, f, indent=2)
    if sys.platform != 'win32':
        os.chmod(tmp, 0o600)      # it holds API keys
    os.replace(tmp, path())


def is_complete(cfg):
    return bool(cfg['indexer']['api_key'] and cfg['sabnzbd']['api_key'])


def library(cfg, kind):
    return os.path.abspath(os.path.expanduser(cfg['library'][kind]))


def category(cfg, kind):
    s = cfg['sabnzbd']
    return s.get(f'category_{kind}') or s.get('category') or ''


def sabnzbd_ini_candidates():
    h = os.path.expanduser('~')
    if sys.platform == 'win32':
        roots = [os.environ.get('LOCALAPPDATA'), os.environ.get('APPDATA')]
        return [os.path.join(r, 'sabnzbd', 'sabnzbd.ini') for r in roots if r]
    return [os.path.join(h, '.sabnzbd', 'sabnzbd.ini'),
            os.path.join(h, '.config', 'sabnzbd', 'sabnzbd.ini'),
            os.path.join(h, 'Library', 'Application Support', 'SABnzbd', 'sabnzbd.ini')]


def read_sabnzbd_ini(ini):
    """The [misc] section of a sabnzbd.ini. It's ConfigObj, not configparser, so read it by hand."""
    misc, section = {}, None
    with open(ini, encoding='utf-8', errors='replace') as f:
        for line in f:
            s = line.strip()
            if s.startswith('['):
                section = s.strip('[]').lower()
                continue
            if section == 'misc' and '=' in s:
                k, v = s.split('=', 1)
                misc[k.strip()] = v.strip().strip('"')
    return misc


def detect_sabnzbd():
    """(url, api_key) from the first sabnzbd.ini on this machine, or None."""
    for ini in sabnzbd_ini_candidates():
        try:
            misc = read_sabnzbd_ini(ini)
        except OSError:
            continue
        key = misc.get('api_key')
        if not key:
            continue
        host = misc.get('host') or '127.0.0.1'
        if host in ('0.0.0.0', '::', '', 'localhost'):
            host = '127.0.0.1'
        https = misc.get('enable_https') == '1' and misc.get('https_port')
        port = misc.get('https_port') if https else (misc.get('port') or '8080')
        return f"{'https' if https else 'http'}://{host}:{port}", key
    return None
