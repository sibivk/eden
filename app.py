import os
import re
import time
import logging
import xml.etree.ElementTree as ET
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

NZBS_API_KEY = os.getenv("NZBS_API_KEY", "")
NZBS_BASE_URL = "https://nzbs.in/api"
NZBGET_URL = os.getenv("NZBGET_URL", "http://localhost:6789")
NZBGET_USER = os.getenv("NZBGET_USERNAME", "nzbget")
NZBGET_PASS = os.getenv("NZBGET_PASSWORD", "")
NZB_CATEGORY = os.getenv("NZB_CATEGORY", "Evaluate")
MAX_SIZE_BYTES = int(os.getenv("MAX_SIZE_GB", "10")) * 1024 ** 3

NEWZNAB_NS = "{http://www.newznab.com/DTD/2010/feeds/attributes/}"

_SAFE_TITLE_RE = re.compile(r"[^\w\s\-\(\)\.]")
_YEAR_RE = re.compile(r"^(.*?)[. _]\(?(\d{4})\)?")


@app.after_request
def security_headers(response):
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["X-XSS-Protection"] = "1; mode=block"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    return response


@app.route("/")
def home():
    return render_template("index.html")


@app.route("/api/search", methods=["GET"])
def search_nzb():
    query = request.args.get("q", "").strip()
    if not query:
        return jsonify({"error": "Query parameter 'q' is required"}), 400
    if len(query) > 200:
        return jsonify({"error": "Query too long"}), 400
    if not NZBS_API_KEY or NZBS_API_KEY == "your_nzbs_in_api_key_here":
        return jsonify({"error": "NZBS_API_KEY is not configured in .env"}), 500

    params = {"t": "movie", "apikey": NZBS_API_KEY, "q": query, "extended": 1}

    try:
        resp = requests.get(NZBS_BASE_URL, params=params, timeout=15)
        resp.raise_for_status()
        root = ET.fromstring(resp.text)
        results = []

        for item in root.findall(".//item"):
            title = item.findtext("title") or ""
            link = item.findtext("link") or ""

            size = 0
            grabs = 0
            imdb_rating = None
            imdb_votes = 0

            for attr in item.findall(f"{NEWZNAB_NS}attr"):
                name = attr.attrib.get("name", "")
                value = attr.attrib.get("value", "")
                if name == "size":
                    size = int(value) if value.isdigit() else 0
                elif name == "grabs":
                    grabs = int(value) if value.isdigit() else 0
                elif name == "imdb_rating":
                    try:
                        imdb_rating = float(value)
                    except ValueError:
                        pass
                elif name == "imdb_votes":
                    try:
                        imdb_votes = int(value)
                    except ValueError:
                        pass

            # Fall back to enclosure length if newznab:attr size is missing
            if size == 0:
                enc = item.find("enclosure")
                if enc is not None:
                    length = enc.attrib.get("length", "0")
                    size = int(length) if length.isdigit() else 0

            if size > MAX_SIZE_BYTES:
                continue

            results.append({
                "title": title,
                "download_url": link,
                "size_bytes": size,
                "grabs": grabs,
                "imdb_rating": imdb_rating,
                "imdb_votes": imdb_votes,
            })

        results.sort(key=lambda x: (x["imdb_rating"] or 0, x["imdb_votes"]), reverse=True)
        logger.info("Search '%s' → %d results (after %dGB filter)", query, len(results), MAX_SIZE_BYTES // 1024 ** 3)
        return jsonify({"results": results})

    except requests.Timeout:
        logger.error("Timeout searching nzbs.in for '%s'", query)
        return jsonify({"error": "Search timed out — try again"}), 504
    except ET.ParseError as e:
        logger.error("XML parse error: %s", e)
        return jsonify({"error": "Unexpected response from nzbs.in"}), 502
    except Exception as e:
        logger.error("Search error: %s", e)
        return jsonify({"error": f"Search failed: {str(e)}"}), 500


VALID_LANGUAGES = {"malayalam", "hindi", "tamil", "english"}

@app.route("/api/queue", methods=["POST"])
def queue_nzb():
    data = request.get_json(silent=True) or {}
    nzb_url = data.get("url", "").strip()
    title = data.get("title", "Movie Download").strip()
    category = data.get("category", NZB_CATEGORY).strip()
    language = data.get("language", "").strip().lower()

    if not nzb_url:
        return jsonify({"error": "Missing 'url' parameter"}), 400
    if not nzb_url.startswith(("http://", "https://")):
        return jsonify({"error": "Invalid URL"}), 400
    if language and language not in VALID_LANGUAGES:
        return jsonify({"error": f"Invalid language '{language}' — must be malayalam, hindi or tamil"}), 400

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
        except Exception as e:
            logger.warning('TMDB poster lookup for %s: %s', title, e)

    # ── 2. Fall back to Plex if TMDB had no art ──
    if not poster:
        plex_p, plex_b = _plex_poster(title, year, language)
        if plex_p:
            poster   = plex_p
            backdrop = plex_b or backdrop

    _poster_cache[cache_key] = {'poster': poster, 'backdrop': backdrop, 'at': time.time()}
    return jsonify({'poster': poster, 'backdrop': backdrop})


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
