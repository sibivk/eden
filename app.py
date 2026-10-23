import os
import re
import time
import logging
import threading
import xml.etree.ElementTree as ET
from datetime import datetime as _dt
from flask import Flask, request, jsonify, render_template, Response
from urllib.parse import urljoin, quote as urlquote
import requests
from bs4 import BeautifulSoup

os.makedirs("logs", exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[
        logging.FileHandler("logs/app.log"),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger(__name__)

app = Flask(__name__)

# Start background scheduler (daily auto-download + file mover)
try:
    from scheduler import start_scheduler
    start_scheduler()
except Exception as _sched_err:
    logger.error("Scheduler failed to start: %s", _sched_err)

NZB_SEARCH_KEY = os.getenv("NZB_SEARCH_KEY", "") or os.getenv("NZBS_API_KEY", "")
NZB_SEARCH_URL = os.getenv("NZB_SEARCH_URL", "")
NZB_SEARCH_KEY2 = os.getenv("NZB_SEARCH_KEY2", "")
NZB_SEARCH_URL2 = os.getenv("NZB_SEARCH_URL2", "")
RADARR_URL     = os.getenv("RADARR_URL", "").rstrip("/")
RADARR_API_KEY = os.getenv("RADARR_API_KEY", "")
NZBGET_URL = os.getenv("NZBGET_URL", "http://localhost:6789")
NZBGET_USER = os.getenv("NZBGET_USERNAME", "nzbget")
NZBGET_PASS = os.getenv("NZBGET_PASSWORD", "")
NZB_CATEGORY = os.getenv("NZB_CATEGORY", "Evaluate")
MAX_SIZE_BYTES = int(os.getenv("MAX_SIZE_GB", "10")) * 1024 ** 3

# External API access
EDEN_API_KEY    = os.getenv("EDEN_API_KEY", "")       # set to lock the API with a key
EDEN_CORS_ORIGINS = os.getenv("EDEN_CORS_ORIGINS", "*")  # e.g. "https://myapp.example.com"

NEWZNAB_NS = "{http://www.newznab.com/DTD/2010/feeds/attributes/}"

_SAFE_TITLE_RE = re.compile(r"[^\w\s\-\(\)\.]")
_YEAR_RE = re.compile(r"^(.*?)[. _]\(?(\d{4})\)?")

_HEVC_RE = re.compile(r'(?i)\b(HEVC|x265|h\.?265)\b')
_5_1_RE  = re.compile(r'(?i)\b(DD[P]?[.\s]?5\.1|DTS(?:[-.\s](?:HD|MA|HD[-.]MA))?|AC3|5\.1|Atmos|TrueHD)\b')
_1080_RE = re.compile(r'(?i)\b1080[pi]\b')
_4K_RE   = re.compile(r'(?i)\b(2160p|4K|UHD)\b')
_720_RE  = re.compile(r'(?i)\b720p\b')


def _parse_quality(title: str) -> dict:
    is_4k   = bool(_4K_RE.search(title))
    is_1080 = bool(_1080_RE.search(title))
    is_720  = bool(_720_RE.search(title))
    resolution = '4K' if is_4k else ('1080p' if is_1080 else ('720p' if is_720 else 'SD'))
    return {
        'resolution': resolution,
        'is_hevc': bool(_HEVC_RE.search(title)),
        'has_5_1': bool(_5_1_RE.search(title)),
    }


@app.before_request
def check_api_key():
    """Require X-Eden-API-Key header (or ?api_key=) on /api/* when EDEN_API_KEY is set."""
    if not request.path.startswith("/api/"):
        return
    if request.method == "OPTIONS":
        return  # let preflight through
    if request.path == "/api/health":
        return  # health is always public
    if not EDEN_API_KEY:
        return  # no key configured → open API (default for local use)
    provided = (request.headers.get("X-Eden-API-Key") or
                request.args.get("api_key", ""))
    if provided != EDEN_API_KEY:
        return jsonify({"error": "Unauthorized — missing or invalid X-Eden-API-Key"}), 401


@app.after_request
def apply_headers(response):
    # Security headers
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-XSS-Protection"] = "1; mode=block"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    # Only set X-Frame-Options on non-API responses (APIs don't render in frames)
    if not request.path.startswith("/api/"):
        response.headers["X-Frame-Options"] = "DENY"
    # CORS — allow external webapps to call Eden's API
    if request.path.startswith("/api/"):
        response.headers["Access-Control-Allow-Origin"]  = EDEN_CORS_ORIGINS
        response.headers["Access-Control-Allow-Methods"] = "GET, POST, DELETE, OPTIONS"
        response.headers["Access-Control-Allow-Headers"] = "Content-Type, X-Eden-API-Key"
        response.headers["Access-Control-Max-Age"]       = "86400"
    return response


@app.route("/api/<path:subpath>", methods=["OPTIONS"])
def api_preflight(subpath):
    """Handle CORS preflight for all /api/* routes."""
    return Response(status=204)


@app.route("/")
def home():
    return render_template("index.html")


def _search_one_indexer(query, base_url, api_key, label):
    """Query one Newznab indexer and return parsed result list."""
    params = {"t": "movie", "apikey": api_key, "q": query, "extended": 1}
    headers = {"User-Agent": "EdenMediaManager/1.0"}
    try:
        resp = requests.get(base_url, params=params, headers=headers, timeout=15)
        resp.raise_for_status()
        root = ET.fromstring(resp.text)
    except Exception as e:
        logger.warning("UI search [%s] error for '%s': %s", label, query, e)
        return []

    items = []
    for item in root.findall(".//item"):
        title = item.findtext("title") or ""
        link  = item.findtext("link") or ""
        size = grabs = imdb_votes = 0
        imdb_rating = None
        for attr in item.findall(f"{NEWZNAB_NS}attr"):
            name  = attr.attrib.get("name", "")
            value = attr.attrib.get("value", "")
            if name == "size":
                size = int(value) if value.isdigit() else 0
            elif name == "grabs":
                grabs = int(value) if value.isdigit() else 0
            elif name == "imdb_rating":
                try: imdb_rating = float(value)
                except ValueError: pass
            elif name == "imdb_votes":
                try: imdb_votes = int(value)
                except ValueError: pass
        if size == 0:
            enc = item.find("enclosure")
            if enc is not None:
                length = enc.attrib.get("length", "0")
                size = int(length) if length.isdigit() else 0
        items.append({
            "title": title, "download_url": link,
            "size_bytes": size, "grabs": grabs,
            "imdb_rating": imdb_rating, "imdb_votes": imdb_votes,
            "quality": _parse_quality(title),
            "source": label,
        })
    logger.info("UI search [%s] '%s' → %d results", label, query, len(items))
    return items


@app.route("/api/search", methods=["GET"])
def search_nzb():
    query = request.args.get("q", "").strip()
    if not query:
        return jsonify({"error": "Query parameter 'q' is required"}), 400
    if len(query) > 200:
        return jsonify({"error": "Query too long"}), 400
    if not NZB_SEARCH_KEY or not NZB_SEARCH_URL:
        return jsonify({"error": "NZB indexer is not configured in .env"}), 500

    # Direct Newznab indexers (NZBGeek + NZBFinder)
    indexers = [("NZBGeek", NZB_SEARCH_URL, NZB_SEARCH_KEY)]
    if NZB_SEARCH_URL2 and NZB_SEARCH_KEY2:
        indexers.append(("NZBFinder", NZB_SEARCH_URL2, NZB_SEARCH_KEY2))

    seen_titles = set()
    results = []
    for label, url, key in indexers:
        for r in _search_one_indexer(query, url, key, label):
            if r["title"] not in seen_titles:
                seen_titles.add(r["title"])
                results.append(r)

    # Radarr — always run; covers nzbs.in without direct contact
    if RADARR_URL and RADARR_API_KEY:
        try:
            from scheduler import _radarr_search
            for r in _radarr_search(query):
                if r["title"] not in seen_titles:
                    seen_titles.add(r["title"])
                    results.append({
                        "title":        r["title"],
                        "download_url": r["url"],
                        "size_bytes":   r["size_bytes"],
                        "grabs":        0,
                        "imdb_rating":  None,
                        "imdb_votes":   0,
                        "quality":      r["quality"],
                        "source":       "nzbs.in (Radarr)",
                    })
        except Exception as e:
            logger.warning("Radarr UI search failed: %s", e)

    def _sort_key(r):
        res = r["quality"]["resolution"]
        prio = {"1080p": 0, "4K": 1}.get(res, 3)
        if prio == 3 and r["quality"]["is_hevc"] and r["quality"]["has_5_1"]:
            prio = 2
        return (prio, -(r["imdb_rating"] or 0), -r["imdb_votes"])

    results.sort(key=_sort_key)
    logger.info("UI search '%s' → %d results", query, len(results))
    return jsonify({"results": results})


VALID_LANGUAGES = {"malayalam", "hindi", "tamil", "english"}

@app.route("/api/queue", methods=["POST"])
def queue_nzb():
    data = request.get_json(silent=True) or {}
    nzb_url = data.get("url", "").strip()
    title = data.get("title", "Movie Download").strip()
    language = data.get("language", "").strip().lower()

    if not nzb_url:
        return jsonify({"error": "Missing 'url' parameter"}), 400
    if not nzb_url.startswith(("http://", "https://")):
        return jsonify({"error": "Invalid URL"}), 400
    if language and language not in VALID_LANGUAGES:
        return jsonify({"error": f"Invalid language '{language}' — must be malayalam, hindi or tamil"}), 400
    # Reject NZBs whose title contains a year older than current_year - 1
    _current_year = __import__('datetime').datetime.utcnow().year
    _year_in_title = _YEAR_RE.search(title)
    if _year_in_title:
        try:
            nzb_year = int(_year_in_title.group(2))
            if nzb_year < _current_year - 1:
                logger.warning("Queue rejected — NZB year %d too old: %s", nzb_year, title[:80])
                return jsonify({"error": f"NZB year {nzb_year} is too old — Eden is for recent releases only"}), 400
        except (ValueError, IndexError):
            pass

    # Lock category to the configured value — ignore whatever the caller sends
    category = NZB_CATEGORY

    safe_title = _SAFE_TITLE_RE.sub("", title)[:200].strip()
    clean_name = clean_movie_title(safe_title)

    payload = {
        "method": "append",
        "params": [f"{clean_name}.nzb", nzb_url, category, 0, False, False, "", 0, "FORCE"],
    }

    rpc_endpoint = urljoin(NZBGET_URL, "/jsonrpc")
    try:
        auth = (NZBGET_USER, NZBGET_PASS) if NZBGET_USER else None
        rpc_resp = requests.post(rpc_endpoint, json=payload, auth=auth, timeout=10)
        rpc_resp.raise_for_status()
        result = rpc_resp.json()

        nzbget_id = result.get("result", 0)
        if nzbget_id > 0:
            logger.info("Queued '%s' [%s] → NZBGet id=%s category=%s", clean_name, language or "unknown", nzbget_id, category)
            # Record in tracking DB so file mover knows which library to use
            if language:
                try:
                    from scheduler import record_queued_manual
                    m = _YEAR_RE.search(clean_name)
                    movie_title = m.group(1).replace(".", " ").strip() if m else clean_name
                    movie_year  = m.group(2) if m else None
                    record_queued_manual(movie_title, movie_year, language, title, nzbget_id)
                except Exception as rec_err:
                    logger.warning("Could not record queue entry: %s", rec_err)
            return jsonify({"status": "success", "nzbget_id": nzbget_id, "name": clean_name})
        else:
            return jsonify({"error": "NZBGet rejected the request", "details": result}), 500

    except requests.Timeout:
        return jsonify({"error": "NZBGet connection timed out"}), 504
    except Exception as e:
        logger.error("Queue error: %s", e)
        return jsonify({"error": f"Failed to connect to NZBGet: {str(e)}"}), 500


@app.route("/api/health", methods=["GET"])
def health():
    return jsonify({"status": "ok", "category": NZB_CATEGORY, "max_size_gb": MAX_SIZE_BYTES // 1024 ** 3})


@app.route("/api/activity", methods=["GET"])
def activity():
    try:
        from scheduler import get_activity, get_stats
        return jsonify({"activity": get_activity(50), "stats": get_stats()})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/scheduler/trigger", methods=["POST"])
def scheduler_trigger():
    try:
        from scheduler import trigger_now
        msg = trigger_now()
        logger.info("Manual scheduler trigger by user")
        return jsonify({"status": "ok", "message": msg})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/watchlist", methods=["GET"])
def api_get_watchlist():
    try:
        from scheduler import get_watchlist
        return jsonify({"watchlist": get_watchlist()})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/watchlist", methods=["POST"])
def api_add_watchlist():
    data = request.get_json(silent=True) or {}
    title = data.get("title", "").strip()[:200]
    language = data.get("language", "").strip().lower()
    year = data.get("year")
    if not title:
        return jsonify({"error": "Missing title"}), 400
    if language and language not in VALID_LANGUAGES:
        return jsonify({"error": f"Invalid language '{language}'"}), 400
    try:
        from scheduler import add_to_watchlist
        item_id = add_to_watchlist(title, language, str(year) if year else None)
        return jsonify({"status": "ok", "id": item_id})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


_WL_ID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


@app.route("/api/watchlist/<item_id>", methods=["DELETE"])
def api_remove_watchlist(item_id):
    if not _WL_ID_RE.match(item_id):
        return jsonify({"error": "Invalid id"}), 400
    try:
        from scheduler import remove_from_watchlist
        removed = remove_from_watchlist(item_id)
        return jsonify({"status": "ok" if removed else "not_found"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/scheduler/process-files", methods=["POST"])
def scheduler_process_files():
    try:
        from scheduler import trigger_process_files
        msg = trigger_process_files()
        logger.info("Manual file-mover trigger by user")
        return jsonify({"status": "ok", "message": msg})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


TMDB_API_KEY = os.getenv('TMDB_API_KEY', '')
PLEX_URL     = os.getenv('PLEX_URL', '').rstrip('/')
PLEX_TOKEN   = os.getenv('PLEX_TOKEN', '')
PLEX_SECTIONS = {
    'malayalam': os.getenv('PLEX_SECTION_MALAYALAM', ''),
    'hindi':     os.getenv('PLEX_SECTION_HINDI', ''),
    'tamil':     os.getenv('PLEX_SECTION_TAMIL', ''),
    'english':   os.getenv('PLEX_SECTION_ENGLISH', ''),
}
_poster_cache: dict = {}
_detail_cache: dict = {}   # title+year → {poster, trailer_key, overview, director, writers, stars}

_YT_HEADERS = {
    'User-Agent': 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
                  '(KHTML, like Gecko) Chrome/124 Safari/537.36',
    'Accept-Language': 'en-US,en;q=0.9',
}

def _yt_key_valid(key: str) -> bool:
    """Check that a YouTube video is publicly playable (not just metadata-present)."""
    import re as _re
    try:
        # Fast oEmbed check: catches videos that no longer exist at all
        oe = requests.get(
            f'https://www.youtube.com/oembed?url=https://www.youtube.com/watch?v={key}&format=json',
            timeout=5,
        )
        if oe.status_code != 200:
            return False
        # Deeper check: oEmbed returns 200 even for ERROR-status videos (metadata
        # stays on YouTube after the video is taken down). Fetch the watch page and
        # look for playabilityStatus so we fall back to YouTube search in that case.
        wp = requests.get(
            f'https://www.youtube.com/watch?v={key}',
            headers=_YT_HEADERS, timeout=8,
        )
        m = _re.search(r'"playabilityStatus"\s*:\s*\{"status"\s*:\s*"([^"]+)"', wp.text)
        if m and m.group(1) not in ('OK',):
            logger.debug('yt key %s playabilityStatus=%s — treating as invalid', key, m.group(1))
            return False
        return True
    except Exception:
        return True  # assume valid on network error

def _youtube_search_trailer(title: str, year: str = '') -> str:
    """Scrape first YouTube video ID from search for '{title} {year} official trailer'."""
    import urllib.parse, re as _re
    query = urllib.parse.quote_plus(f'{title} {year} official trailer'.strip())
    # &sp=EgIQAQ%3D%3D filters to videos only
    url = f'https://www.youtube.com/results?search_query={query}&sp=EgIQAQ%3D%3D'
    try:
        r = requests.get(url, headers=_YT_HEADERS, timeout=10)
        r.raise_for_status()
        m = _re.search(r'"videoId":"([a-zA-Z0-9_-]{11})"', r.text)
        return m.group(1) if m else ''
    except Exception:
        return ''

# Only allow well-formed Plex metadata thumb/art paths to prevent SSRF
_PLEX_PATH_RE = re.compile(r'^/library/metadata/\d+/(?:thumb|art)(?:/\d+)?$')


def _plex_poster(title: str, year: str, language: str = '') -> tuple:
    """Return (poster_proxy_url, backdrop_proxy_url) from Plex, or ('', '')."""
    if not PLEX_URL or not PLEX_TOKEN:
        return '', ''

    section_id = PLEX_SECTIONS.get(language.lower(), '')

    def _plex_search(query: str):
        try:
            r = requests.get(
                f'{PLEX_URL}/search',
                params={'query': query, 'type': 1, 'limit': 8},
                headers={'X-Plex-Token': PLEX_TOKEN, 'Accept': 'application/json'},
                timeout=6,
            )
            r.raise_for_status()
            items = r.json().get('MediaContainer', {}).get('Metadata', [])
            # Filter to the right library section when we know it
            if section_id:
                items = [i for i in items
                         if str(i.get('librarySectionID', '')) == str(section_id)]
            if not items:
                return None
            # Prefer year match, fall back to first result
            if year:
                for i in items:
                    if str(i.get('year', '')) == year:
                        return i
            return items[0]
        except Exception as exc:
            logger.warning('Plex search "%s": %s', query, exc)
            return None

    # 1. Try full title
    match = _plex_search(title)

    # 2. Word-by-word fallback — handles title mismatches between movies.json and Plex
    if not match:
        words = sorted([w for w in title.split() if len(w) >= 4], key=len, reverse=True)
        for word in words[:3]:
            match = _plex_search(word)
            if match:
                break

    if not match:
        logger.info('Plex: no match for "%s" (lang=%s)', title, language)
        return '', ''

    thumb = match.get('thumb', '')
    art   = match.get('art', '')

    def _proxy(path):
        return f'/api/plex-img?path={urlquote(path)}' if path and _PLEX_PATH_RE.match(path) else ''

    logger.info('Plex art found for "%s" → "%s"', title, match.get('title', ''))
    return _proxy(thumb), _proxy(art)

PINKVILLA_NEWS_URL = 'https://www.pinkvilla.com/latest'
_news_cache: dict = {'data': None, 'at': 0.0}
NEWS_CACHE_TTL = 900  # 15 minutes

_SAFE_URL_RE = re.compile(r'^https?://')


@app.route('/api/news', methods=['GET'])
def get_news():
    now = time.time()
    if _news_cache['data'] is not None and now - _news_cache['at'] < NEWS_CACHE_TTL:
        return jsonify({'news': _news_cache['data']})
    try:
        resp = requests.get(
            PINKVILLA_NEWS_URL, timeout=12,
            headers={
                'User-Agent': 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
                              '(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
                'Accept': 'text/html,application/xhtml+xml',
            },
        )
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, 'html.parser')
        items = []
        for card in soup.find_all('div', class_='mv--cards--sec-s1'):
            link_el = card.find('a', href=True)
            if not link_el:
                continue
            # Full title is in the <a title="..."> attribute; <p> text is truncated
            title = link_el.get('title', '').strip()
            if not title:
                p = link_el.find('p', class_='card--content--style-s1')
                title = p.get_text(strip=True) if p else ''
            if not title:
                continue
            url = link_el['href'].strip()
            if not _SAFE_URL_RE.match(url):
                continue
            # Image: lazy-loaded, real URL is in data-src
            img_el = card.find('img')
            image = ''
            if img_el:
                image = img_el.get('data-src', '') or img_el.get('src', '')
                if not _SAFE_URL_RE.match(image):
                    image = ''
            items.append({'title': title, 'url': url, 'image': image})
            if len(items) >= 12:
                break
        _news_cache['data'] = items
        _news_cache['at'] = now
        logger.info('PinkVilla news scraped: %d items', len(items))
        return jsonify({'news': items})
    except requests.Timeout:
        logger.warning('News scrape timed out')
        return jsonify({'error': 'News fetch timed out'}), 504
    except Exception as e:
        logger.error('News scrape failed: %s', e)
        return jsonify({'error': 'Could not load news'}), 502


BOLLYWOOD_CAL_URL = 'https://www.bollywoodmdb.com/movies/calendar-{year}'
_MONTH_ABBR_MAP = {
    'jan': 1, 'feb': 2, 'mar': 3, 'apr': 4, 'may': 5, 'jun': 6,
    'jul': 7, 'aug': 8, 'sep': 9, 'oct': 10, 'nov': 11, 'dec': 12,
}
_MONTH_FULL_MAP = {
    'january': 1, 'february': 2, 'march': 3, 'april': 4, 'may': 5, 'june': 6,
    'july': 7, 'august': 8, 'september': 9, 'october': 10, 'november': 11, 'december': 12,
}
_CAL_DATE_RE = re.compile(
    r'\b(\d{1,2})\s*(?:st|nd|rd|th)?\s+'
    r'(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|'
    r'jul(?:y)?|aug(?:ust)?|sep(?:tember)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)',
    re.IGNORECASE,
)
_cal_cache: dict = {'data': None, 'at': 0.0, 'year': 0}
CAL_CACHE_TTL = 6 * 3600  # 6 hours
_cal_bg_running = False
_cal_bg_lock = threading.Lock()

_LANG_CODE_MAP = {
    'hi': 'Hindi', 'ml': 'Malayalam', 'ta': 'Tamil', 'te': 'Telugu',
    'kn': 'Kannada', 'bn': 'Bengali', 'mr': 'Marathi', 'pa': 'Punjabi',
    'en': 'English', 'gu': 'Gujarati', 'or': 'Odia', 'as': 'Assamese',
    'ur': 'Urdu', 'sa': 'Sanskrit', 'ne': 'Nepali', 'si': 'Sinhala',
    'ko': 'Korean', 'ja': 'Japanese', 'zh': 'Chinese', 'fr': 'French',
    'es': 'Spanish', 'de': 'German', 'it': 'Italian', 'pt': 'Portuguese',
    'ru': 'Russian', 'ar': 'Arabic', 'tr': 'Turkish', 'th': 'Thai',
    'id': 'Indonesian', 'ms': 'Malay', 'vi': 'Vietnamese',
}
_lang_cache: dict = {}  # title.lower() -> language string


def _tmdb_language(title: str) -> str:
    """Return display-name language for a movie title via TMDB original_language."""
    key = title.lower().strip()
    if key in _lang_cache:
        return _lang_cache[key]
    if not TMDB_API_KEY:
        return ''
    try:
        resp = requests.get(
            'https://api.themoviedb.org/3/search/movie',
            params={'api_key': TMDB_API_KEY, 'query': title, 'language': 'en-US', 'page': 1},
            timeout=8,
        )
        resp.raise_for_status()
        results = resp.json().get('results', [])
        lang_code = results[0].get('original_language', '') if results else ''
        lang = _LANG_CODE_MAP.get(lang_code, lang_code.upper() if lang_code else '')
        _lang_cache[key] = lang
        return lang
    except Exception as e:
        logger.warning('TMDB language lookup for %s: %s', title, e)
        _lang_cache[key] = ''
        return ''


def _scrape_calendar(year: int) -> list:
    """
    Scrape bollywoodmdb.com calendar.
    Structure: <section aria-labelledby="date-YYYY-MM-DD">
      <ol><li>
        <p class="bmdb-body-emphasis …">Movie Title</p>
        <img src="https://cdn.bollywoodmdb.com/…">
      </li></ol>
    </section>
    """
    resp = requests.get(
        BOLLYWOOD_CAL_URL.format(year=year), timeout=15,
        headers={
            'User-Agent': 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
                          '(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Accept': 'text/html,application/xhtml+xml',
        },
    )
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, 'html.parser')

    movies = []
    seen: set = set()

    # Each date block: <section aria-labelledby="date-2026-09-03">
    for section in soup.find_all('section', attrs={'aria-labelledby': True}):
        lid = section.get('aria-labelledby', '')
        parts = lid.split('-')          # ['date', '2026', '09', '03']
        if len(parts) != 4 or parts[0] != 'date':
            continue
        try:
            yr, mo, dy = int(parts[1]), int(parts[2]), int(parts[3])
        except ValueError:
            continue
        if yr != year:
            continue

        ol = section.find('ol')
        if not ol:
            continue

        for li in ol.find_all('li'):
            # Title: <p class="bmdb-body-emphasis …"> — not the genre/cast line
            title_el = li.find('p', class_=lambda c: c and 'bmdb-body-emphasis' in c)
            if not title_el:
                continue
            title = title_el.get_text(strip=True)
            if not title:
                continue

            key = (title.lower(), mo)
            if key in seen:
                continue
            seen.add(key)

            img = li.find('img')
            poster = ''
            if img:
                poster = (img.get('src') or img.get('data-src') or '').strip()
                if not poster.startswith('http'):
                    poster = ''

            movies.append({'title': title, 'month': mo, 'day': dy, 'poster': poster, 'language': ''})

    movies.sort(key=lambda x: (x['month'], x['day']))

    # Enrich with TMDB original_language (rate-limited to avoid hammering the API)
    for i, m in enumerate(movies):
        if i > 0:
            time.sleep(0.12)   # ~8 req/s, well within TMDB free-tier limits
        m['language'] = _tmdb_language(m['title'])

    logger.info('Calendar: scraped %d movies for %d', len(movies), year)
    return movies


# ── Filmibeat scraper ──────────────────────────────────────────────────────────

FILMIBEAT_URLS = {
    'Malayalam': 'https://www.filmibeat.com/malayalam/movies.html',
    'Hindi':     'https://www.filmibeat.com/bollywood/movies.html',
    'Tamil':     'https://www.filmibeat.com/tamil/movies.html',
}
_FB_DATE_RE = re.compile(
    r'(\d{1,2})\s+(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*'
    r'[,\s]+(\d{4})',
    re.IGNORECASE,
)
_FB_MONTH_MAP = {
    'jan':1,'feb':2,'mar':3,'apr':4,'may':5,'jun':6,
    'jul':7,'aug':8,'sep':9,'oct':10,'nov':11,'dec':12,
}

def _scrape_filmibeat(year: int) -> list:
    """Scrape filmibeat.com for Malayalam, Hindi (bollywood), and Kannada releases."""
    movies = []
    seen: set = set()
    headers = {
        'User-Agent': 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
                      '(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
        'Accept': 'text/html,application/xhtml+xml',
    }
    for language, url in FILMIBEAT_URLS.items():
        try:
            resp = requests.get(url, headers=headers, timeout=15)
            resp.raise_for_status()
            soup = BeautifulSoup(resp.text, 'html.parser')
            count = 0
            for card in soup.find_all('div', class_='fb-movie-details'):
                h3 = card.find('h3')
                if not h3:
                    continue
                title = h3.get_text(strip=True)
                if not title:
                    continue
                date_div = card.find('div', class_='fb-movie-rels-date')
                if not date_div:
                    continue
                m = _FB_DATE_RE.search(date_div.get_text(strip=True))
                if not m:
                    continue
                dy  = int(m.group(1))
                mo  = _FB_MONTH_MAP[m.group(2).lower()[:3]]
                yr  = int(m.group(3))
                if yr != year:
                    continue
                parent = card.parent
                img = parent.find('img') if parent else None
                poster = ''
                if img:
                    poster = (img.get('src') or img.get('data-src', '')).strip()
                    if not poster.startswith('http'):
                        poster = ''
                key = (re.sub(r'[^\w]', '', title.lower()), mo)
                if key in seen:
                    continue
                seen.add(key)
                movies.append({
                    'title': title, 'month': mo, 'day': dy,
                    'poster': poster, 'language': language,
                })
                count += 1
            logger.info('Filmibeat [%s]: %d movies', language, count)
        except Exception as e:
            logger.error('Filmibeat scrape failed [%s]: %s', language, e)
    return movies


def _fetch_all_calendar(year: int) -> list:
    """Merge bollywoodmdb + filmibeat calendars, deduplicated by (title, month)."""
    primary = []
    try:
        primary = _scrape_calendar(year)
    except Exception as e:
        logger.warning('bollywoodmdb calendar failed: %s', e)

    secondary = _scrape_filmibeat(year)

    seen = {(re.sub(r'[^\w]', '', m['title'].lower()), m['month']) for m in primary}
    for m in secondary:
        key = (re.sub(r'[^\w]', '', m['title'].lower()), m['month'])
        if key not in seen:
            primary.append(m)
            seen.add(key)

    primary.sort(key=lambda x: (x['month'], x['day']))
    logger.info('Combined calendar: %d movies for %d', len(primary), year)
    return primary


def _refresh_calendar_bg():
    global _cal_bg_running
    year = _dt.now().year
    try:
        all_movies = _fetch_all_calendar(year)
        _cal_cache.update({'data': all_movies, 'at': time.time(), 'year': year})
        logger.info('Calendar background refresh done: %d movies', len(all_movies))
    except Exception as e:
        logger.warning('Calendar background refresh failed: %s', e)
    finally:
        _cal_bg_running = False


@app.route('/api/calendar', methods=['GET'])
def api_calendar():
    global _cal_bg_running
    now = _dt.now()
    year, month = now.year, now.month
    label = now.strftime('%B %Y')

    has_data = _cal_cache['data'] is not None and _cal_cache['year'] == year
    is_fresh  = has_data and time.time() - _cal_cache['at'] < CAL_CACHE_TTL

    if has_data:
        # Return stale-or-fresh data immediately; kick off background refresh if stale
        if not is_fresh:
            with _cal_bg_lock:
                if not _cal_bg_running:
                    _cal_bg_running = True
                    threading.Thread(target=_refresh_calendar_bg, daemon=True).start()
        movies = [m for m in _cal_cache['data'] if m['month'] == month]
        return jsonify({'movies': movies, 'month': month, 'year': year, 'label': label})

    # Cold start — no data at all; scrape synchronously
    try:
        all_movies = _fetch_all_calendar(year)
        _cal_cache.update({'data': all_movies, 'at': time.time(), 'year': year})
        movies = [m for m in all_movies if m['month'] == month]
        return jsonify({'movies': movies, 'month': month, 'year': year, 'label': label})
    except Exception as e:
        logger.error('Calendar scrape: %s', e)
        return jsonify({
            'error': str(e), 'movies': [], 'month': month, 'year': year, 'label': label,
        }), 502


def _prewarm_calendar():
    """Pre-populate the calendar cache at startup so the first open is instant."""
    year = _dt.now().year
    try:
        all_movies = _fetch_all_calendar(year)
        _cal_cache.update({'data': all_movies, 'at': time.time(), 'year': year})
        logger.info('Calendar pre-warm done: %d movies', len(all_movies))
    except Exception as e:
        logger.warning('Calendar pre-warm failed: %s', e)

# Warm the calendar cache 8 seconds after startup (gives Gunicorn time to bind)
threading.Timer(8.0, _prewarm_calendar).start()


@app.route('/api/library', methods=['GET'])
def api_library():
    try:
        from scheduler import get_library
        return jsonify({'library': get_library()})
    except Exception as e:
        logger.error('Library error: %s', e)
        return jsonify({'error': str(e)}), 500


@app.route('/api/catalog', methods=['GET'])
def api_catalog():
    try:
        from scheduler import get_catalog
        movies = get_catalog()
        grouped: dict = {}
        for m in movies:
            lang = m['language']
            grouped.setdefault(lang, []).append({
                'title': m['title'],
                'year': m.get('year'),
                'language': lang,
            })
        return jsonify({'catalog': grouped})
    except Exception as e:
        logger.error('Catalog error: %s', e)
        return jsonify({'error': str(e)}), 500


@app.route('/api/poster', methods=['GET'])
def api_poster():
    title    = request.args.get('title', '').strip()[:200]
    year     = request.args.get('year',  '').strip()[:4]
    language = request.args.get('lang',  '').strip().lower()[:20]
    if not title:
        return jsonify({'poster': '', 'backdrop': ''})
    cache_key = f'{title.lower()}|{year}|{language}'
    cached = _poster_cache.get(cache_key)
    if cached and time.time() - cached['at'] < 86400:
        return jsonify({'poster': cached['poster'], 'backdrop': cached.get('backdrop', '')})

    def _tmdb_search(query, yr=None):
        p = {'api_key': TMDB_API_KEY, 'query': query, 'language': 'en-US', 'page': 1}
        if yr:
            p['primary_release_year'] = yr
        resp = requests.get('https://api.themoviedb.org/3/search/movie', params=p, timeout=8)
        resp.raise_for_status()
        return resp.json().get('results', [])

    poster = backdrop = ''

    # ── 1. Try TMDB ──
    orig_lang = ''
    if TMDB_API_KEY:
        try:
            results = _tmdb_search(title, year or None)
            if not results and year:
                results = _tmdb_search(title)
            if results:
                pp = results[0].get('poster_path', '')
                bp = results[0].get('backdrop_path', '')
                if pp: poster   = f'https://image.tmdb.org/t/p/w342{pp}'
                if bp: backdrop = f'https://image.tmdb.org/t/p/w1280{bp}'
                orig_lang = results[0].get('original_language', '')
        except Exception as e:
            logger.warning('TMDB poster lookup for %s: %s', title, e)

    # ── 2. Fall back to Plex if TMDB had no art ──
    if not poster:
        plex_p, plex_b = _plex_poster(title, year, language)
        if plex_p:
            poster   = plex_p
            backdrop = plex_b or backdrop

    _poster_cache[cache_key] = {'poster': poster, 'backdrop': backdrop,
                                'orig_lang': orig_lang, 'at': time.time()}
    return jsonify({'poster': poster, 'backdrop': backdrop, 'orig_lang': orig_lang})


@app.route('/api/movie-detail', methods=['GET'])
def api_movie_detail():
    """Return trailer key, synopsis, and credits for a movie via TMDB."""
    title = request.args.get('title', '').strip()[:200]
    year  = request.args.get('year',  '').strip()[:4]
    if not title:
        return jsonify({'error': 'missing title'}), 400

    cache_key = f'{title.lower()}|{year}'
    cached = _detail_cache.get(cache_key)
    if cached and time.time() - cached.get('_at', 0) < 86400:
        payload = {k: v for k, v in cached.items() if k != '_at'}
        return jsonify(payload)

    if not TMDB_API_KEY:
        return jsonify({'error': 'TMDB not configured'}), 503

    result = {
        'poster': '', 'backdrop': '', 'trailer_key': '',
        'overview': '', 'director': '', 'writers': [], 'stars': [],
    }

    try:
        # 1. Find movie
        p = {'api_key': TMDB_API_KEY, 'query': title, 'language': 'en-US', 'page': 1}
        if year:
            p['primary_release_year'] = year
        r = requests.get('https://api.themoviedb.org/3/search/movie', params=p, timeout=8)
        r.raise_for_status()
        results = r.json().get('results', [])
        if not results and year:
            p.pop('primary_release_year', None)
            r = requests.get('https://api.themoviedb.org/3/search/movie', params=p, timeout=8)
            r.raise_for_status()
            results = r.json().get('results', [])
        if not results:
            # No TMDB match — still try YouTube search for trailer
            try:
                result['trailer_key'] = _youtube_search_trailer(title, year) or ''
            except Exception as ye:
                logger.warning('youtube fallback (no tmdb) for %s: %s', title, ye)
            _detail_cache[cache_key] = dict(result, _at=time.time())
            return jsonify(result)

        movie = results[0]
        movie_id = movie['id']
        pp = movie.get('poster_path', '')
        bp = movie.get('backdrop_path', '')
        if pp: result['poster']   = f'https://image.tmdb.org/t/p/w500{pp}'
        if bp: result['backdrop'] = f'https://image.tmdb.org/t/p/w1280{bp}'
        result['overview'] = movie.get('overview', '')

        # 2. Credits
        cr = requests.get(
            f'https://api.themoviedb.org/3/movie/{movie_id}/credits',
            params={'api_key': TMDB_API_KEY}, timeout=8,
        )
        cr.raise_for_status()
        crew = cr.json().get('crew', [])
        cast = cr.json().get('cast', [])
        directors = [c['name'] for c in crew if c.get('job') == 'Director']
        writers   = [c['name'] for c in crew
                     if c.get('department') == 'Writing' and c.get('job') in ('Writer', 'Screenplay', 'Story')]
        result['director'] = ', '.join(directors[:2])
        result['writers']  = writers[:3]
        result['stars']    = [c['name'] for c in cast[:4]]

        # 3. Videos (YouTube trailer) — try en-US first, fall back to all languages
        def _pick_trailer(videos):
            return next(
                (v for v in videos if v.get('site') == 'YouTube' and v.get('type') == 'Trailer'),
                next((v for v in videos if v.get('site') == 'YouTube'), None),
            )

        vr = requests.get(
            f'https://api.themoviedb.org/3/movie/{movie_id}/videos',
            params={'api_key': TMDB_API_KEY, 'language': 'en-US'}, timeout=8,
        )
        vr.raise_for_status()
        trailer = _pick_trailer(vr.json().get('results', []))
        if not trailer:
            vr2 = requests.get(
                f'https://api.themoviedb.org/3/movie/{movie_id}/videos',
                params={'api_key': TMDB_API_KEY}, timeout=8,
            )
            vr2.raise_for_status()
            trailer = _pick_trailer(vr2.json().get('results', []))
        if trailer:
            key = trailer['key']
            result['trailer_key'] = key if _yt_key_valid(key) else ''

    except Exception as e:
        logger.warning('movie-detail for %s: %s', title, e)

    # Final fallback: scrape YouTube search if TMDB has no trailer or key is dead
    if not result['trailer_key']:
        try:
            result['trailer_key'] = _youtube_search_trailer(title, year) or ''
        except Exception as e:
            logger.warning('youtube trailer fallback for %s: %s', title, e)

    _detail_cache[cache_key] = dict(result, _at=time.time())
    return jsonify(result)


@app.route('/api/plex-img', methods=['GET'])
def plex_img():
    path = request.args.get('path', '').strip()
    if not _PLEX_PATH_RE.match(path) or not PLEX_URL or not PLEX_TOKEN:
        return Response(status=404)
    try:
        r = requests.get(
            f'{PLEX_URL}{path}',
            headers={'X-Plex-Token': PLEX_TOKEN},
            timeout=10,
        )
        r.raise_for_status()
        return Response(r.content, content_type=r.headers.get('Content-Type', 'image/jpeg'))
    except Exception as e:
        logger.warning('Plex image proxy %s: %s', path, e)
        return Response(status=502)


def clean_movie_title(folder_name):
    match = _YEAR_RE.search(folder_name)
    if match:
        raw_title, year = match.groups()
        clean = raw_title.replace(".", " ").strip()
        return f"{clean} ({year})"
    return folder_name


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 5000)))
