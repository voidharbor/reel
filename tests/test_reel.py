"""Offline tests: no network, no SABnzbd. Run with  python -m unittest  from the repo root."""
import copy, json, os, shutil, sys, tempfile, time, unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from reel import config, engine as E, lookup as L  # noqa: E402


def cfg_in(tmp):
    cfg = copy.deepcopy(config.DEFAULTS)
    cfg['library'] = {'movies': os.path.join(tmp, 'Movies'), 'tv': os.path.join(tmp, 'TV')}
    return cfg


def fake_video(path, kind='mkv', size=64):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'wb') as f:
        f.write(b'\x1a\x45\xdf\xa3' if kind == 'mkv' else b'\x00\x00\x00\x18ftypmp42')
        f.write(b'\0' * size)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='reel-test-')
        self.old_home, self.old_min = os.environ.get('REEL_HOME'), E.MIN_SIZE
        os.environ['REEL_HOME'] = os.path.join(self.tmp, 'home')
        E.MIN_SIZE = 10
        self.cfg = cfg_in(self.tmp)

    def tearDown(self):
        E.MIN_SIZE = self.old_min
        if self.old_home is None:
            os.environ.pop('REEL_HOME', None)
        else:
            os.environ['REEL_HOME'] = self.old_home
        shutil.rmtree(self.tmp, ignore_errors=True)


class Names(unittest.TestCase):
    def test_episode_tags(self):
        self.assertEqual(E.episode_tag('Family.Guy.S24E16.1080p'), (24, 16, None))
        self.assertEqual(E.episode_tag('Show.S01E09E10.1080p'), (1, 9, 10))
        self.assertEqual(E.episode_tag('Show.S01E09-E10.1080p'), (1, 9, 10))
        self.assertEqual(E.episode_tag('superjail 1x02'), (1, 2, None))
        self.assertIsNone(E.episode_tag('Movie.1920x1080.mkv'))
        self.assertIsNone(E.episode_tag('Breaking.Bad.S01.1080p'))

    def test_pack_seasons(self):
        self.assertEqual(E.pack_seasons('Breaking.Bad.S01.1080p.BluRay'), {1})
        self.assertEqual(E.pack_seasons('Breaking.Bad.S01-S05.1080p'), {1, 2, 3, 4, 5})
        self.assertEqual(E.pack_seasons('Breaking.Bad.S01-05.BOX-SET.1080p'), {1, 2, 3, 4, 5})
        self.assertEqual(E.pack_seasons('Show Season 2 1080p'), {2})
        self.assertEqual(E.pack_seasons('Show.Complete.Series.1080p'), E.ALL_SEASONS)
        self.assertIsNone(E.pack_seasons('Show.S01E02.1080p'))

    def test_split_and_show(self):
        self.assertEqual(E.split_movie('Knowing.2009.1080p.BluRay.x264'), ('Knowing', 2009))
        self.assertEqual(E.split_movie('2001.A.Space.Odyssey.1968.1080p'), ('2001 A Space Odyssey', 1968))
        self.assertEqual(E.show_of('Family.Guy.S24E16.1080p'), 'Family Guy')
        self.assertEqual(E.show_of('Breaking.Bad.S01.1080p'), 'Breaking Bad')

    def test_title_matches(self):
        self.assertTrue(E.title_matches('Knowing.2009.1080p', 'Knowing', 2009))
        self.assertFalse(E.title_matches('Band.of.Brothers.2001.1080p', 'The Others', 2001))
        self.assertFalse(E.title_matches('Grave.Encounters.2.2012.1080p', 'Grave Encounters', 2011))
        self.assertTrue(E.title_matches('Sopranos.S01E01.1080p', 'The Sopranos'))
        self.assertTrue(E.title_matches('The.Office.US.S01E01', 'The Office'))

    def test_episode_title_from_name(self):
        self.assertEqual(E.episode_title_from_name('Superjail.S01E02.Combaticus.720p.WEB-DL.AAC2.0', 1, 2), 'Combaticus')
        self.assertEqual(E.episode_title_from_name('Superjail.S01E02.1080p.WEB.H264', 1, 2), '')

    def test_clean_and_render(self):
        self.assertEqual(E.clean('2001: A Space Odyssey'), '2001 - A Space Odyssey')
        self.assertEqual(E.clean('Who Are You?'), 'Who Are You')
        self.assertEqual(E.render('{title} ({year})/{title} ({year})', title='AC/DC Live', year=''),
                         ['AC-DC Live', 'AC-DC Live'])
        self.assertEqual(E.render('{show}/Season {season:02d}/{show} {sxxexx} - {episode_title}', show='Show',
                                  season=1, episode=2, sxxexx='S01E02', episode_title=''),
                         ['Show', 'Season 01', 'Show S01E02'])


class Grading(unittest.TestCase):
    cfg = copy.deepcopy(config.DEFAULTS)

    def v(self, title, want, size=5e9):
        return E.verdict({'title': title, 'size': size}, want, self.cfg)

    def test_movie(self):
        want = {'kind': 'movie', 'title': 'Knowing', 'year': 2009}
        self.assertEqual(self.v('Knowing.2009.1080p.BluRay.x264-GRP', want), (True, ''))
        self.assertEqual(self.v('Knowing.2009.720p.BluRay.x264', want), (False, '720p'))
        self.assertEqual(self.v('Knowing.2009.2160p.UHD.BluRay.x265', want)[0], False)
        self.assertEqual(self.v('Knowing.2009.1080p.BluRay.REMUX.AVC', want), (False, 'remux'))
        self.assertEqual(self.v('Knowing.2009.EXTRAS.1080p.BluRay', want), (False, 'extras only'))
        self.assertEqual(self.v('Knowing.2009.MULTI.1080p.BluRay.x264', want), (False, 'MULTI'))
        self.assertEqual(self.v('Knowing.2009.1080p.BluRay.x264', want, size=40e9), (False, 'too big'))
        self.assertEqual(self.v('Band.of.Brothers.2009.1080p', want), (False, 'other title'))

    def test_episode_and_season(self):
        ep = {'kind': 'episode', 'show': 'Superjail!', 'season': 1, 'episode': 2}
        self.assertEqual(self.v('Superjail.S01E02.1080p.WEB.H264', ep, 8e8), (True, ''))
        self.assertEqual(self.v('Superjail.S01E03.1080p.WEB.H264', ep, 8e8), (False, 'other episode'))
        self.assertEqual(self.v('Superjail.S01.1080p.WEB.H264', ep, 8e9), (False, 'whole season'))
        season = {'kind': 'season', 'show': 'Breaking Bad', 'season': 1}
        self.assertEqual(self.v('Breaking.Bad.S01.1080p.BluRay.x264', season, 40e9), (True, ''))
        self.assertEqual(self.v('Breaking.Bad.S02.1080p.BluRay.x264', season, 40e9), (False, 'other season'))
        self.assertEqual(self.v('Breaking.Bad.S01-S05.1080p.BluRay.x264', season, 200e9), (True, ''))
        self.assertEqual(self.v('Breaking.Bad.S01E04.1080p.BluRay.x264', season, 2e9), (True, ''))

    def test_pack_scores_above_episodes_for_a_season(self):
        season = {'kind': 'season', 'show': 'Breaking Bad', 'season': 1}
        pack = E.score({'title': 'Breaking.Bad.S01.1080p.BluRay.x264'}, season)
        ep = E.score({'title': 'Breaking.Bad.S01E01.1080p.BluRay.x264'}, season)
        self.assertGreater(pack, ep)


class Library(Base):
    def test_targets(self):
        self.assertEqual(E.movie_target(self.cfg, 'Knowing', 2009, '1080p'),
                         os.path.join(self.tmp, 'Movies', 'Knowing (2009)', 'Knowing (2009)'))
        os.makedirs(os.path.join(self.tmp, 'TV', 'Superjail'))
        self.assertEqual(E.show_dir_name(self.cfg, 'Superjail!'), 'Superjail')
        self.assertEqual(E.episode_target(self.cfg, 'Superjail', 1, 2, 'Combaticus'),
                         os.path.join(self.tmp, 'TV', 'Superjail', 'Season 01', 'Superjail S01E02 - Combaticus'))

    def test_move_video_and_tidy(self):
        job = os.path.join(self.tmp, 'complete', 'knowing_2009')
        fake_video(os.path.join(job, 'Knowing.2009.1080p.mkv'))
        with open(os.path.join(job, 'Knowing.2009.1080p.srt'), 'w') as f:
            f.write('1\n')
        stem = E.movie_target(self.cfg, 'Knowing', 2009)
        final = E.move_video(self.cfg, os.path.join(job, 'Knowing.2009.1080p.mkv'), stem, job='knowing_2009')
        self.assertEqual(final, stem + '.mkv')
        self.assertTrue(os.path.exists(stem + '.srt'))
        self.assertFalse(os.path.exists(job), 'the job folder should be gone')

    def test_tidy_never_touches_a_flat_folder(self):
        flat = os.path.join(self.tmp, 'complete')
        fake_video(os.path.join(flat, 'Knowing.2009.1080p.mkv'))
        with open(os.path.join(flat, 'someone-elses-album.flac'), 'w') as f:
            f.write('x')
        E.move_video(self.cfg, os.path.join(flat, 'Knowing.2009.1080p.mkv'), E.movie_target(self.cfg, 'Knowing', 2009),
                     job='knowing_2009')
        self.assertTrue(os.path.exists(os.path.join(flat, 'someone-elses-album.flac')))

    def test_mp4_keeps_its_container(self):
        job = os.path.join(self.tmp, 'complete', 'x')
        fake_video(os.path.join(job, 'Movie.2020.1080p.mkv'), kind='mp4')
        final = E.move_video(self.cfg, job, E.movie_target(self.cfg, 'Movie', 2020))
        self.assertTrue(final.endswith('.mp4'))

    def test_place_pack(self):
        job = os.path.join(self.tmp, 'complete', 'superjail_s01')
        for e in (1, 2, 3):
            fake_video(os.path.join(job, f'Superjail.S01E{e:02d}.1080p.WEB.mkv'))
        fake_video(os.path.join(job, 'Sample', 'superjail.sample.mkv'))
        fake_video(os.path.join(job, 'Superjail.S01E02.1080p.WEB.mkv').replace('E02', 'E02x'), size=1)   # junk name
        show_root = os.path.join(self.tmp, 'TV', 'Superjail')
        fake_video(os.path.join(show_root, 'Season 01', 'Superjail S01E01 - Superbar.mkv'))   # already have E01
        out = E.place_pack(self.cfg, job, show_root, {'1:2': 'Combaticus', '1:3': 'Ladies Night'}, 1,
                           job='superjail_s01')
        season = os.path.join(show_root, 'Season 01')
        self.assertEqual(sorted(os.listdir(season)), ['Superjail S01E01 - Superbar.mkv',
                                                      'Superjail S01E02 - Combaticus.mkv',
                                                      'Superjail S01E03 - Ladies Night.mkv'])
        self.assertIn('2 episodes placed', out['detail'])
        self.assertIn('1 you already had', out['detail'])

    def test_pack_with_unreadable_files_stays_put(self):
        job = os.path.join(self.tmp, 'complete', 'pack')
        fake_video(os.path.join(job, 'Show.S01E01.mkv'))
        fake_video(os.path.join(job, 'bonus.disc.mkv'))
        out = E.place_pack(self.cfg, job, os.path.join(self.tmp, 'TV', 'Show'), {}, job='pack')
        self.assertIn('unreadable', out['detail'])
        self.assertTrue(os.path.exists(os.path.join(job, 'bonus.disc.mkv')))

    def test_failed_memory(self):
        f = E.Failed()
        f.add('Some.Release', 'Not on your server')
        self.assertTrue(E.Failed().has('Some.Release'))


class Config(Base):
    def test_detect_sabnzbd(self):
        ini = os.path.join(self.tmp, 'sabnzbd.ini')
        with open(ini, 'w') as f:
            f.write('[misc]\nhost = 0.0.0.0\nport = 8085\napi_key = abc123\n[servers]\n[[news.example.com]]\n'
                    'api_key = not-this-one\n')
        old = config.sabnzbd_ini_candidates
        config.sabnzbd_ini_candidates = lambda: [ini]
        try:
            self.assertEqual(config.detect_sabnzbd(), ('http://127.0.0.1:8085', 'abc123'))
        finally:
            config.sabnzbd_ini_candidates = old

    def test_save_and_load(self):
        cfg = copy.deepcopy(config.DEFAULTS)
        cfg['indexer']['api_key'] = 'k'
        cfg['sabnzbd']['api_key'] = 's'
        config.save(cfg)
        self.assertTrue(config.is_complete(config.load()))
        self.assertEqual(config.category(cfg, 'tv'), 'reel')


class Lookup(unittest.TestCase):
    def setUp(self):
        self.old = (L.suggest, L.tv_search)

    def tearDown(self):
        L.suggest, L.tv_search = self.old

    def fake(self, imdb, tvmaze=()):
        L.suggest = lambda text: [dict(c) for c in imdb if c['title'].lower().startswith(text[:3].lower())] \
            if len(text) < 6 else [dict(c) for c in imdb]
        L.tv_search = lambda text: [dict(c) for c in tvmaze]

    def test_parse(self):
        self.assertEqual(L.parse('knowing 2009'), {'title': 'knowing', 'year': 2009, 'season': None, 'episode': None})
        self.assertEqual(L.parse('superjail s01e02')['episode'], 2)
        self.assertEqual(L.parse('breaking bad season 2')['season'], 2)
        self.assertEqual(L.parse('2001 a space odyssey')['title'], '2001 a space odyssey')

    def test_resolve(self):
        knowing = {'imdb': 'tt0448011', 'title': 'Knowing', 'year': 2009, 'kind': 'movie', 'cast': '', 'rank': 900}
        shining = {'imdb': 'tt0081505', 'title': 'The Shining', 'year': 1980, 'kind': 'movie', 'cast': '', 'rank': 300}
        days = {'imdb': 'tt1', 'title': 'The Shinning Days', 'year': 2009, 'kind': 'tv', 'cast': '', 'rank': 900000}
        self.fake([knowing])
        self.assertEqual(L.resolve('knowing 2009')[2], 'exact')
        self.assertEqual(L.resolve('knowign 2009')[2], 'corrected')
        self.fake([days, shining])
        p, c, how, _ = L.resolve('the shinning')
        self.assertEqual((c['title'], how), ('The Shining', 'corrected'))
        self.fake([{'imdb': 'tt2', 'title': 'Superbia', 'year': 2016, 'kind': 'movie', 'cast': '', 'rank': 90000}])
        self.assertEqual(L.resolve('superjial', 'movie')[2], 'maybe')
        self.fake([])
        self.assertEqual(L.resolve('xqzv')[2], None)


class JobLog(Base):
    def test_job_reads_its_log(self):
        from reel import ui
        log = os.path.join(self.tmp, 'job.log')
        lines = [{'stage': 'connecting', 'msg': 'checking SABnzbd', 't': 1},
                 {'stage': 'downloading', 'pct': 40.0, 'left': 6e8, 'total': 1e9, 'speed': 1e8, 't': 2},
                 {'result': 'ok', 'path': '/x/Show S01E02 - Title.mkv', 'detail': '1.0 GB, mkv checked', 't': 3}]
        with open(log, 'w') as f:
            f.write(json.dumps(lines[0]) + '\n' + json.dumps(lines[1]) + '\n')
        job = ui.Job({'pid': os.getpid(), 'log': log, 'kind': 'episode', 'target': '/x/Show S01E02 - Title',
                      'release': 'r', 'started': time.time()})
        job.refresh()
        self.assertIsNone(job.result)
        self.assertAlmostEqual(job.frac(), 0.4)
        with open(log, 'a') as f:
            f.write(json.dumps(lines[2]) + '\n')
        job.refresh()
        self.assertEqual(job.result['result'], 'ok')
        self.assertEqual(job.label(), 'Show S01E02 - Title.mkv')

    def test_dead_job_without_verdict_fails(self):
        from reel import ui
        log = os.path.join(self.tmp, 'job.log')
        with open(log, 'w') as f:
            f.write('Traceback (most recent call last):\n')
        job = ui.Job({'pid': 999999999, 'log': log, 'kind': 'movie', 'target': '/x/M', 'release': 'r',
                      'started': time.time()})
        job.refresh()
        self.assertEqual(job.result['result'], 'fail')


if __name__ == '__main__':
    unittest.main()
