"""
Eden Scheduler

Daily 10AM  — scrape ottmovierelease.com → search nzbs.in → queue to NZBGet
Every 5 min — read .eden_ready.log → move completed files to language libraries
"""

import os
import re
import json
import shutil
import logging
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path
from threading import Lock
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup
from apscheduler.schedulers.background import BackgroundScheduler

logger = logging.getLogger(__name__)

# ── Paths ─────────────────────────────────────────────────────────────────────
STORAGE_MOUNT = Path('/media')
READY_LOG     = STORAGE_MOUNT / '.eden_ready.log'
DATA_DIR      = Path('/app/data')
MOVIES_DB     = DATA_DIR / 'movies.json'
LOG_POSITION  = DATA_DIR / 'log_position.txt'

LIBRARY_PATHS = {
    'malayalam': Path('/libraries/malayalam'),
    'hindi':     Path('/libraries/hindi'),
    'tamil':     Path('/libraries/tamil'),
    'english':   Path('/libraries/english'),
}

# ── Config ────────────────────────────────────────────────────────────────────
NZBS_API_KEY  = os.getenv('NZBS_API_KEY', '')
NZBS_BASE_URL = 'https://nzbs.in/api'
NZBGET_URL    = os.getenv('NZBGET_URL', '')
NZBGET_USER   = os.getenv('NZBGET_USERNAME', '')
NZBGET_PASS   = os.getenv('NZBGET_PASSWORD', '')
NZB_CATEGORY  = os.getenv('NZB_CATEGORY', 'Evaluate')

MIN_SIZE_BYTES = 1 * 1024 ** 3   # 1 GB minimum
MAX_SIZE_BYTES = 10 * 1024 ** 3  # 10 GB cap
NEWZNAB_NS     = '{http://www.newznab.com/DTD/2010/feeds/attributes/}'
TARGET_LANGS   = {'malayalam', 'hindi', 'tamil'}
PROTECTED_DIRS = {'preroll', 'temp'}   # compared case-insensitively
OTT_URL        = 'https://www.ottmovierelease.com'

# Plex
PLEX_URL      = os.getenv('PLEX_URL', '')
PLEX_TOKEN    = os.getenv('PLEX_TOKEN', '')
PLEX_SECTIONS = {
    'malayalam': os.getenv('PLEX_SECTION_MALAYALAM', ''),
    'hindi':     os.getenv('PLEX_SECTION_HINDI', ''),
    'tamil':     os.getenv('PLEX_SECTION_TAMIL', ''),
    'english':   os.getenv('PLEX_SECTION_ENGLISH', ''),
}

_db_lock = Lock()
_scheduler = None


# ── Helpers ───────────────────────────────────────────────────────────────────

def _norm(text: str) -> str:
    """Lowercase alphanumeric only — used for fuzzy matching."""
    return re.sub(r'[^\w]', '', text.lower())


def _fuzzy_match(title: str, candidate: str) -> bool:
    """
    Bidirectional fuzzy match: strip year/ext from candidate, then check
    if either string contains the other with ≥60% length overlap.
    Catches 'Chand Mera Dil' vs 'Chand.Mera.Dil.2026.1080p.mkv' while
    blocking short-prefix false positives like 'Lurk' vs 'Lurking in the Shadows'.
    """
    norm_title = _norm(title)
    # Strip trailing year and quality tokens from candidate before comparing
    bare = re.sub(r'((?:19|20)\d{2}.*)', '', candidate)
    norm_cand = _norm(bare) or _norm(candidate)
    if not norm_title or not norm_cand:
        return False
    if norm_title == norm_cand:
        return True
    shorter, longer = (norm_title, norm_cand) if len(norm_title) <= len(norm_cand) else (norm_cand, norm_title)
    return shorter in longer and len(shorter) / len(longer) >= 0.6


# Strips year + all quality/codec tokens from an NZB title to extract the bare movie name
_QUALITY_STRIP_RE = re.compile(
    r'(?i)[.\s_-](?:(?:19|20)\d{2}|s\d{2}e\d{2}|\d{3,4}[pi]|uhd|4k|'
    r'web[-.]?dl|blu[-.]?ray|bdrip|hdtv|dvdrip|x\d{3}|h\.?\d{3}|hevc|avc|'
    r'dts|ac3|ddp?\d*|flac|aac|atmos|remux|proper|repack).*'
)


def _nzb_title_matches(query: str, nzb_name: str) -> bool:
    """
    Verify an NZB result title actually corresponds to the queried movie.
    Strips quality tokens, then requires the bare name and query to be
    equal or have ≥60% length overlap — prevents 'lurk' matching 'Lurking
    in the Shadows' or other unrelated results that share a substring.
    """
    bare = _QUALITY_STRIP_RE.sub('', nzb_name)
    bare = re.sub(r'[.\-_]', ' ', bare).strip()
    nq = _norm(query)
    nb = _norm(bare)
    if not nq or not nb:
        return False
    if nq == nb:
        return True
    shorter, longer = (nq, nb) if len(nq) <= len(nb) else (nb, nq)
    return shorter in longer and len(shorter) / len(longer) >= 0.6


def _plex_scan(language: str):
    """Trigger a Plex library scan for the given language section."""
    if not PLEX_URL or not PLEX_TOKEN:
        return
    section_id = PLEX_SECTIONS.get(language, '')
    if not section_id:
        logger.warning('No Plex section ID configured for language: %s', language)
        return
    url = f'{PLEX_URL}/library/sections/{section_id}/refresh'
    try:
        r = requests.get(url, params={'X-Plex-Token': PLEX_TOKEN}, timeout=10)
        if r.status_code == 200:
            logger.info('Plex scan triggered for %s (section %s)', language, section_id)
        else:
            logger.warning('Plex scan returned %d for section %s', r.status_code, section_id)
    except Exception as e:
        logger.error('Plex scan failed for %s: %s', language, e)


def _extract_name_year(raw: str) -> tuple:
    """
    Extract (clean_title, year) from an NZB title or folder name.
    'Lenin.2026.1080p.ZEE5...' → ('Lenin', '2026')
    """
    m = re.search(r'^(.*?)[.\s_\-]\(?((?:19|20)\d{2})\)?', raw)
    if m:
        name = re.sub(r'[.\-_]', ' ', m.group(1)).strip()
        return name, m.group(2)
    first = re.split(r'[.\-_\s]', raw)[0].strip()
    return first, None


def _db_key(title: str, year) -> str:
    return _norm(title) + (year or '')


# ── Data store ────────────────────────────────────────────────────────────────

def _load_db() -> dict:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if MOVIES_DB.exists():
        try:
            return json.loads(MOVIES_DB.read_text())
        except Exception:
            pass
    return {'downloads': {}, 'activity': []}


def _save_db(db: dict):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    MOVIES_DB.write_text(json.dumps(db, indent=2, default=str))


def _is_tracked(title: str, year) -> bool:
    with _db_lock:
        db = _load_db()
        entry = db.get('downloads', {}).get(_db_key(title, year))
        if not entry:
            return False
        if entry.get('status') == 'moved':
            return True
        # Queued >7 days ago means the download likely failed/was wrong — allow retry
        if entry.get('status') == 'queued':
            queued_at = entry.get('queued_at', '')
            if queued_at:
                try:
                    age = datetime.utcnow() - datetime.fromisoformat(queued_at)
                    if age.days > 7:
                        logger.info('Stale queued entry (>7d), will retry: %s', title)
                        return False
                except Exception:
                    pass
        return True


def _record_queued(title: str, year, language: str, nzb_title: str, nzbget_id: int):
    with _db_lock:
        db = _load_db()
        key = _db_key(title, year)
        db.setdefault('downloads', {})[key] = {
            'title': title, 'year': year, 'language': language,
            'nzb_title': nzb_title, 'nzbget_id': nzbget_id,
            'queued_at': datetime.utcnow().isoformat(), 'status': 'queued',
        }
        _log_activity(db, 'queued', title=title, year=year, language=language, detail=nzb_title)
        _save_db(db)


def _record_moved(key: str, title: str, dest: str):
    with _db_lock:
        db = _load_db()
        entry = db.get('downloads', {}).get(key, {})
        entry.update({'status': 'moved', 'moved_to': dest, 'moved_at': datetime.utcnow().isoformat()})
        db.setdefault('downloads', {})[key] = entry
        _log_activity(db, 'moved', title=title, detail=dest)
        _save_db(db)


def _log_activity(db: dict, kind: str, **kwargs):
    db.setdefault('activity', []).insert(0, {
        'type': kind, 'at': datetime.utcnow().isoformat(), **kwargs
    })
    db['activity'] = db['activity'][:200]


def get_activity(limit: int = 50) -> list:
    with _db_lock:
        return _load_db().get('activity', [])[:limit]


def get_stats() -> dict:
    with _db_lock:
        db = _load_db()
        dl = db.get('downloads', {})
        return {
            'total_queued': len(dl),
            'total_moved': sum(1 for v in dl.values() if v.get('status') == 'moved'),
        }


# ── Library existence check ───────────────────────────────────────────────────

def _in_library(title: str) -> bool:
    """Return True if a movie matching title already exists in any library."""
    # STORAGE_PATH (Evaluate) — skip protected dirs and hidden files
    if STORAGE_MOUNT.exists():
        for item in STORAGE_MOUNT.iterdir():
            if item.name.lower() in PROTECTED_DIRS:
                continue
            if item.name.startswith('.'):
                continue
            if _fuzzy_match(title, item.name):
                return True

    # Language libraries
    for lp in LIBRARY_PATHS.values():
        if lp.exists():
            for item in lp.iterdir():
                if _fuzzy_match(title, item.name):
                    return True

    return False


# ── OTT scraper ───────────────────────────────────────────────────────────────

def scrape_ott_movies() -> list:
    """
    Fetch ottmovierelease.com and return STREAMING-only Malayalam/Hindi/Tamil movies.
    Returns: [{'title': str, 'language': str, 'year': str|None}]
    """
    try:
        resp = requests.get(OTT_URL, timeout=20,
                            headers={'User-Agent': 'Mozilla/5.0 (compatible; Eden)'})
        resp.raise_for_status()
    except Exception as e:
        logger.error('OTT scrape failed: %s', e)
        return []

    soup = BeautifulSoup(resp.text, 'html.parser')
    movies = []
    seen = set()

    for card in soup.find_all('article', class_='movie-card'):
        lang = card.get('data-language', '').lower().strip()
        if lang not in TARGET_LANGS:
            continue

        # Only streaming (OTT) releases — skip theatrical
        if card.get('data-release-type', '').lower() != 'ott':
            continue

        title_el = card.find('h2', class_='movie-card__title')
        if not title_el:
            continue
        title = title_el.get_text(strip=True)
        if not title:
            continue

        date = card.get('data-release', '')
        year = date[:4] if len(date) >= 4 else None

        key = (_norm(title), lang)
        if key in seen:
            continue
        seen.add(key)

        movies.append({'title': title, 'language': lang, 'year': year})

    logger.info('OTT scrape: %d streaming %s movies found',
                len(movies), '/'.join(sorted(TARGET_LANGS)))
    return movies


# ── nzbs.in search ────────────────────────────────────────────────────────────

def search_1080p(title: str) -> list:
    """
    Search nzbs.in for 1080p releases of a title (falls back to 2160p/4K if none found).
    Results are title-verified, filtered to 1GB–10GB, sorted smallest-first.
    """
    if not NZBS_API_KEY:
        logger.warning('NZBS_API_KEY not set — skipping search')
        return []

    params = {'t': 'movie', 'apikey': NZBS_API_KEY, 'q': title, 'extended': 1}
    try:
        resp = requests.get(NZBS_BASE_URL, params=params, timeout=15)
        resp.raise_for_status()
        root = ET.fromstring(resp.text)
    except Exception as e:
        logger.error('nzbs.in search error for "%s": %s', title, e)
        return []

    results_1080 = []
    results_4k   = []

    for item in root.findall('.//item'):
        nzb_title = item.findtext('title') or ''
        link      = item.findtext('link') or ''

        is_1080 = '1080p' in nzb_title or '1080i' in nzb_title
        is_4k   = '2160p' in nzb_title or '4K' in nzb_title or 'UHD' in nzb_title
        if not is_1080 and not is_4k:
            continue

        # Verify the NZB title actually corresponds to the searched movie
        if not _nzb_title_matches(title, nzb_title):
            logger.debug('Title mismatch — skipping: query=%r nzb=%r', title, nzb_title)
            continue

        # Parse size from newznab:attr, fall back to enclosure
        size = 0
        for attr in item.findall(f'{NEWZNAB_NS}attr'):
            if attr.attrib.get('name') == 'size':
                v = attr.attrib.get('value', '0')
                size = int(v) if v.isdigit() else 0
                break
        if size == 0:
            enc = item.find('enclosure')
            if enc is not None:
                v = enc.attrib.get('length', '0')
                size = int(v) if v.isdigit() else 0

        if size < MIN_SIZE_BYTES or size > MAX_SIZE_BYTES:
            continue

        entry = {'title': nzb_title, 'url': link, 'size_bytes': size}
        if is_1080:
            results_1080.append(entry)
        else:
            results_4k.append(entry)

    # Prefer 1080p; fall back to 4K/2160p if nothing found
    results = results_1080 or results_4k
    results.sort(key=lambda x: x['size_bytes'])
    return results


# ── NZBGet queue ──────────────────────────────────────────────────────────────

def queue_to_nzbget(nzb_url: str, name: str) -> int | None:
    if not NZBGET_URL:
        logger.warning('NZBGET_URL not set')
        return None

    payload = {
        'method': 'append',
        'params': [f'{name}.nzb', nzb_url, NZB_CATEGORY, 0, False, False, '', 0, 'FORCE'],
    }
    auth = (NZBGET_USER, NZBGET_PASS) if NZBGET_USER else None
    try:
        r = requests.post(urljoin(NZBGET_URL, '/jsonrpc'), json=payload, auth=auth, timeout=10)
        r.raise_for_status()
        nid = r.json().get('result', 0)
        return nid if nid > 0 else None
    except Exception as e:
        logger.error('NZBGet queue error: %s', e)
        return None


# ── Daily auto-download job ───────────────────────────────────────────────────

def _record_job_run(scraped: int, queued: int, skipped: int, no_nzb: int, status: str):
    """Write a job_run summary record to the activity log."""
    with _db_lock:
        db = _load_db()
        _log_activity(db, 'job_run',
                      scraped=scraped, queued=queued,
                      skipped=skipped, no_nzb=no_nzb, status=status)
        _save_db(db)


def auto_download_job():
    logger.info('=== Auto-download job started ===')
    movies = scrape_ott_movies()
    if not movies:
        logger.warning('Nothing scraped from OTT site — aborting')
        _record_job_run(scraped=0, queued=0, skipped=0, no_nzb=0, status='scrape_failed')
        return

    queued = skipped_exists = skipped_no_nzb = 0

    for movie in movies:
        title = movie['title']
        lang  = movie['language']
        year  = movie.get('year')

        if _is_tracked(title, year):
            logger.debug('Already tracked: %s (%s)', title, year)
            skipped_exists += 1
            continue

        if _in_library(title):
            logger.info('Already in library: %s', title)
            skipped_exists += 1
            continue

        results = search_1080p(title)
        if not results:
            logger.info('No 1080p NZB (1–10 GB) found for: %s', title)
            skipped_no_nzb += 1
            continue

        best = results[0]  # smallest above 1 GB
        nzb_title = best['title']
        nzb_url   = best['url']
        size_gb   = best['size_bytes'] / 1024 ** 3

        # Clean name for NZBGet filename: Title.Year
        _, nzb_year = _extract_name_year(nzb_title)
        use_year = nzb_year or year
        safe_title = re.sub(r'[^\w\s]', '', title).strip().replace(' ', '.')
        clean_name = f'{safe_title}.{use_year}' if use_year else safe_title

        nzbget_id = queue_to_nzbget(nzb_url, clean_name)
        if nzbget_id:
            res = '4K' if ('2160p' in nzb_title or '4K' in nzb_title or 'UHD' in nzb_title) else '1080p'
            logger.info('✓ Queued "%s" [%s/%s] %.2fGB → NZBGet #%d', title, lang, res, size_gb, nzbget_id)
            _record_queued(title, use_year, lang, nzb_title, nzbget_id)
            queued += 1
        else:
            logger.error('✗ Failed to queue "%s" to NZBGet', title)

    logger.info('=== Auto-download done: %d queued | %d in library | %d no NZB ===',
                queued, skipped_exists, skipped_no_nzb)
    status = 'queued_some' if queued > 0 else ('all_skipped' if skipped_exists > 0 else 'nothing_found')
    _record_job_run(scraped=len(movies), queued=queued,
                    skipped=skipped_exists, no_nzb=skipped_no_nzb, status=status)


# ── File mover ────────────────────────────────────────────────────────────────

def _get_log_pos() -> int:
    if LOG_POSITION.exists():
        try:
            return int(LOG_POSITION.read_text().strip())
        except Exception:
            pass
    return 0


def _save_log_pos(pos: int):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    LOG_POSITION.write_text(str(pos))


def process_completed_files():
    """
    Read .eden_ready.log written by the NZBGet post-process script.
    For each new entry, look up the movie's language and move the file
    to the appropriate library folder with a clean name (Title.Year.ext).
    """
    if not READY_LOG.exists():
        return

    try:
        lines = READY_LOG.read_text(errors='replace').splitlines()
    except Exception as e:
        logger.error('Cannot read ready log: %s', e)
        return

    pos = _get_log_pos()
    new_lines = lines[pos:]
    if not new_lines:
        return

    moved_count = 0

    for line in new_lines:
        pos += 1
        line = line.strip()
        if not line:
            continue

        # Format: timestamp|relative/path/to/file
        parts = line.split('|', 1)
        rel_path = parts[1].strip() if len(parts) == 2 else parts[0].strip()
        if not rel_path:
            continue

        # Resolve path inside the container
        src = STORAGE_MOUNT / rel_path

        # Safety: never touch PREROLL or TEMP (case-insensitive)
        if any(p.lower() in PROTECTED_DIRS for p in src.parts):
            logger.warning('Skipping protected path: %s', src)
            continue

        if not src.exists():
            logger.warning('Completed file not found in container: %s', src)
            continue

        # Extract title + year from filename
        raw_name = src.stem if src.is_file() else src.name
        movie_title, movie_year = _extract_name_year(raw_name)
        # Strip embedded language tags like "(Tamil)", "(Hindi)" that NZBGet sometimes
        # appends to filenames — they corrupt the DB key and break the lookup
        movie_title = re.sub(
            r'\s*\((?:malayalam|hindi|tamil|english|telugu|kannada)\)\s*',
            '', movie_title, flags=re.IGNORECASE
        ).strip()
        key = _db_key(movie_title, movie_year)

        # Look up language in tracking DB
        with _db_lock:
            db = _load_db()
        entry = db.get('downloads', {}).get(key)

        if not entry:
            # Fuzzy fallback: match against stored title values (handles year mismatches
            # and cases where the extracted title differs slightly from the queued title)
            norm = _norm(movie_title)
            entry = next(
                (v for v in db.get('downloads', {}).values()
                 if norm in _norm(v.get('title', '')) or _norm(v.get('title', '')) in norm),
                None
            )

        if not entry:
            logger.warning('No tracking entry for "%s" — cannot move (run through the UI first)', raw_name)
            continue

        lang = entry.get('language', '')
        dest_dir = LIBRARY_PATHS.get(lang)

        if not dest_dir:
            logger.warning('No library path for language "%s"', lang)
            continue

        if not dest_dir.exists():
            logger.warning('Library path not mounted/exists: %s', dest_dir)
            continue

        # Build clean destination: Title.Year.ext  (e.g. Lenin.2026.mkv)
        ext = src.suffix if src.is_file() else ''
        clean_stem = f'{movie_title.replace(" ", ".")}.{movie_year}' if movie_year \
                     else movie_title.replace(' ', '.')
        dest = dest_dir / f'{clean_stem}{ext}'

        if dest.exists():
            logger.info('Already at destination, skipping: %s', dest)
            continue

        # Determine source folder BEFORE the move (path is gone after)
        src_folder = src.parent if src.is_file() else src

        try:
            shutil.move(str(src), str(dest))
            logger.info('Moved: %s → %s', src.name, dest)
            _record_moved(key, movie_title, str(dest))
            moved_count += 1

            # Clean up source folder in STORAGE_PATH (NZBGet download folder)
            if src_folder != STORAGE_MOUNT and src_folder.name.lower() not in PROTECTED_DIRS:
                try:
                    shutil.rmtree(str(src_folder))
                    logger.info('Cleaned up source folder: %s', src_folder.name)
                except Exception as rm_err:
                    logger.warning('Could not remove source folder %s: %s', src_folder, rm_err)

            # Trigger Plex library scan for the destination language
            _plex_scan(lang)
        except Exception as e:
            logger.error('Move failed %s → %s: %s', src, dest, e)

    _save_log_pos(pos)
    if moved_count:
        logger.info('File mover: moved %d file(s)', moved_count)


# ── Scheduler init ────────────────────────────────────────────────────────────

def start_scheduler():
    global _scheduler
    if _scheduler is not None:
        return

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    _scheduler = BackgroundScheduler(timezone='UTC')
    _scheduler.add_job(auto_download_job,      'cron',     hour=10, minute=0, id='auto_dl')
    _scheduler.add_job(process_completed_files, 'interval', minutes=5,         id='file_mover')
    _scheduler.start()
    logger.info('Scheduler started — auto-download at 10:00 UTC, file check every 5 min')


def trigger_now():
    """Manually trigger the auto-download job (for testing)."""
    import threading
    t = threading.Thread(target=auto_download_job, daemon=True)
    t.start()
    return 'Auto-download job triggered'


def trigger_process_files():
    """Manually trigger the file mover (for testing)."""
    import threading
    t = threading.Thread(target=process_completed_files, daemon=True)
    t.start()
    return 'File mover triggered'


def record_queued_manual(title: str, year, language: str, nzb_title: str, nzbget_id: int):
    """Record a manually queued movie (via the UI Queue button) into the tracking DB."""
    _record_queued(title, year, language, nzb_title, nzbget_id)
