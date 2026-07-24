#!/usr/bin/env python3
"""BIM Sign Bank lookup tool.

Reads the BIM Sign Bank URL from url.txt, looks up each word in a
user-provided phrase against the BIM Sign Bank Strapi API, and renders the
sign (embedded YouTube video + thumbnail) for each matched word.

Two modes:

  * One-shot report (default) -- a phrase is passed on the command line and
    a self-contained HTML report is written to bim_report.html and opened:

        python bim_lookup.py "good morning"

  * Interactive web app -- a local server with a search box; type a phrase
    and the matching signs are shown inline, videos click-to-play:

        python bim_lookup.py serve            # http://127.0.0.1:8000/
        python bim_lookup.py serve --port 8080

Stdlib only -- no pip installs required.
"""

import argparse
import html as html_mod
import json
import os
import re
import socket
import ssl
import sys
import threading
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
URL_FILE = HERE / "url.txt"
# On Vercel the filesystem is read-only except for /tmp, so cache writes there
# when running serverless. Locally we keep the cache next to the script.
if os.environ.get("VERCEL"):
    CACHE_FILE = Path("/tmp/bim_cache.json")
else:
    CACHE_FILE = HERE / "bim_cache.json"
REPORT_FILE = HERE / "bim_report.html"

USER_AGENT = "BIM-Lookup/1.0 (personal use; contact: user)"

# Discovered from the site's JS bundle. The API/image hosts are fixed
# regardless of the page path in url.txt, but we still read url.txt so the
# input file remains the source of the site identity.
DEFAULT_PAGE = "https://bimsignbank.org/home"
API_HOST = "https://api.bimsignbank.org"
IMG_HOST = "https://images.bimsignbank.org"

_ssl_warned = False
_cache = None  # lazy-loaded dict


# --------------------------------------------------------------------------- #
# url.txt
# --------------------------------------------------------------------------- #
def read_site_url() -> str:
    """Return the URL recorded in url.txt (trimmed), or a sensible default."""
    try:
        text = URL_FILE.read_text(encoding="utf-8").strip()
        if text:
            return text
    except OSError:
        pass
    print(f"[warn] could not read {URL_FILE.name}; using {DEFAULT_PAGE}",
          file=sys.stderr)
    return DEFAULT_PAGE


# --------------------------------------------------------------------------- #
# HTTP (with the site's expired intermediate cert worked around)
# --------------------------------------------------------------------------- #
def _ssl_context_for(host: str) -> ssl.SSLContext | None:
    """Return an unverified SSL context only for the bimsignbank host.

    The BIM Sign Bank site currently serves an expired intermediate
    certificate, so the default verification fails. We disable verification
    narrowly for that host (and warn once) rather than globally.
    """
    global _ssl_warned
    if "bimsignbank.org" in host:
        if not _ssl_warned:
            print("[warn] bimsignbank.org SSL chain is expired on the server; "
                  "certificate verification disabled for this host only.",
                  file=sys.stderr)
            _ssl_warned = True
        return ssl._create_unverified_context()
    return None


def _load_cache() -> dict:
    global _cache
    if _cache is None:
        try:
            _cache = json.loads(CACHE_FILE.read_text(encoding="utf-8"))
            if not isinstance(_cache, dict):
                _cache = {}
        except (OSError, json.JSONDecodeError):
            _cache = {}
    return _cache


def _save_cache() -> None:
    try:
        CACHE_FILE.write_text(
            json.dumps(_load_cache(), ensure_ascii=False, indent=0),
            encoding="utf-8",
        )
    except OSError as exc:
        print(f"[warn] could not write cache: {exc}", file=sys.stderr)


def http_get_json(url: str, *, use_cache: bool = True) -> dict:
    """GET a JSON document, with a local cache and a narrow SSL workaround."""
    cache = _load_cache()
    if use_cache and url in cache:
        return cache[url]

    parsed = urllib.parse.urlsplit(url)
    ctx = _ssl_context_for(parsed.netloc)
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=20, context=ctx) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.URLError as exc:
        # Serve stale cache if we somehow have it, else re-raise.
        if url in cache:
            return cache[url]
        raise RuntimeError(f"network error fetching {url}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"bad JSON from {url}: {exc}") from exc

    if use_cache:
        cache[url] = data
        _save_cache()
    return data


def http_head_ok(url: str) -> bool:
    """True if a HEAD request to `url` returns 2xx/3xx (best-effort)."""
    parsed = urllib.parse.urlsplit(url)
    ctx = _ssl_context_for(parsed.netloc)
    req = urllib.request.Request(url, method="HEAD",
                                 headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=15, context=ctx) as resp:
            return 200 <= resp.status < 400
    except Exception:
        return False


# --------------------------------------------------------------------------- #
# Strapi query helpers
# --------------------------------------------------------------------------- #
def _query(filters: list[tuple[str, str, str]]) -> str:
    """Build a /api/bims URL with a list of (field, op, value) filters.

    First filter whose response is non-empty wins.
    """
    base = f"{API_HOST}/api/bims?populate=category_group&pagination[pageSize]=10"
    for field, op, value in filters:
        url = (f"{base}&filters[{field}][{op}]="
               f"{urllib.parse.quote(value, safe='')}")
        data = http_get_json(url)
        rows = data.get("data") or []
        if rows:
            return _first_record(rows)
    return None


def _first_record(rows: list) -> dict | None:
    """Normalize a Strapi record to a small dict the renderer understands."""
    if not rows:
        return None
    r = rows[0]
    attrs = {k: v for k, v in r.items() if k not in ("id", "documentId")}
    cg = attrs.get("category_group") or {}
    return {
        "id": r.get("id"),
        "word": attrs.get("Word") or "",
        "perkataan": attrs.get("Perkataan") or "",
        "video": attrs.get("Video") or "",
        "img_status": attrs.get("Image_Status") or "",
        "group_category": cg.get("GroupCategory") or "",
    }


def lookup_word(token: str) -> dict | None:
    """Look up a single token, trying exact EN, prefix EN, then prefix Malay."""
    return _query([
        ("Word", "$eqi", token),
        ("Word", "$startsWithi", token),
        ("Perkataan", "$startsWithi", token),
    ])


# --------------------------------------------------------------------------- #
# Phrase -> tokens
# --------------------------------------------------------------------------- #
def tokenize(phrase: str) -> list[str]:
    """Split a phrase into lookup tokens (words), dropping punctuation."""
    return [t for t in re.split(r"[^0-9A-Za-z']+", phrase) if t]


# --------------------------------------------------------------------------- #
# Media
# --------------------------------------------------------------------------- #
_YT_PATTERNS = (
    re.compile(r"(?:youtu\.be/)([A-Za-z0-9_-]{6,})"),
    re.compile(r"(?:[?&]v=)([A-Za-z0-9_-]{6,})"),
    re.compile(r"(?:embed/)([A-Za-z0-9_-]{6,})"),
)


def youtube_id(video_url: str) -> str | None:
    if not video_url:
        return None
    for pat in _YT_PATTERNS:
        m = pat.search(video_url)
        if m:
            return m.group(1)
    return None


def youtube_embed_url(video_url: str) -> str | None:
    """A privacy-friendly embeddable YouTube URL, or None if not a YT link."""
    yt_id = youtube_id(video_url)
    return f"https://www.youtube-nocookie.com/embed/{yt_id}" if yt_id else None


def sanitize_vocab(word: str) -> str:
    """Replicate the frontend's vocab-image filename sanitization."""
    s = word or ""
    s = s.strip()
    s = re.sub(r"[!/]", "-", s)          # ! and / -> -
    s = re.sub(r"\?", "", s)             # drop ?
    s = re.sub(r'[<>:"\\|*]', "", s)     # drop forbidden filename chars
    s = re.sub(r"[. ]+$", "", s)         # strip trailing . and space
    return s


def media_for(record: dict) -> dict:
    """Pick the thumbnail/video URLs for a record.

    Primary visual is the YouTube thumbnail (per chosen output mode). The
    site's vocab webp is computed too and wired as an <img onerror> fallback
    in the HTML so a real image is shown if/when one exists.
    """
    video = record.get("video") or ""
    yt_id = youtube_id(video)
    thumbnail = f"https://img.youtube.com/vi/{yt_id}/hqdefault.jpg" if yt_id else None

    vocab_word = record.get("word") or record.get("perkataan") or ""
    vocab = (f"{IMG_HOST}/vocab/"
             f"{urllib.parse.quote(sanitize_vocab(vocab_word), safe='')}.webp"
             if vocab_word else None)

    return {
        "video": video or None,
        "embed": youtube_embed_url(video),
        "thumbnail": thumbnail,
        "vocab_image": vocab,
    }


# --------------------------------------------------------------------------- #
# HTML report
# --------------------------------------------------------------------------- #
def render_report(phrase: str,
                  results: list[tuple[str, dict | None]]) -> str:
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    cards = []
    for token, rec in results:
        if rec is None:
            cards.append(f"""
        <article class="card no-match">
          <div class="thumb thumb-empty" aria-hidden="true">—</div>
          <h3>{html_mod.escape(token)}</h3>
          <p class="gloss">No match found</p>
        </article>""")
            continue

        media = media_for(rec)
        word = html_mod.escape(rec.get("word") or token)
        perkataan = html_mod.escape(rec.get("perkataan") or "")
        category = html_mod.escape(rec.get("group_category") or "")
        video = media["video"]
        thumb = media["thumbnail"]
        vocab = media["vocab_image"]

        # Prefer the site vocab image, fall back to the YouTube thumbnail
        # client-side via onerror.
        if vocab and thumb:
            img_tag = (
                f'<img class="thumb" src="{vocab}" '
                f'onerror="this.onerror=null;this.src=\'{thumb}\'" alt="{word}">'
            )
        elif thumb:
            img_tag = f'<img class="thumb" src="{thumb}" alt="{word}">'
        else:
            img_tag = '<div class="thumb thumb-empty" aria-hidden="true">no video</div>'

        video_link = (
            f'<a class="video-link" href="{html_mod.escape(video)}" '
            f'target="_blank" rel="noopener">▶ Watch the sign</a>'
            if video else '<span class="muted">no video</span>'
        )

        cards.append(f"""
        <article class="card">
          {img_tag}
          <h3>{word}</h3>
          <p class="gloss">{perkataan}</p>
          <p class="category">{category}</p>
          {video_link}
        </article>""")

    cards_html = "\n".join(cards)
    matched = sum(1 for _, r in results if r is not None)
    summary = (f"{matched}/{len(results)} word"
               f"{'s' if len(results) != 1 else ''} matched")

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>BIM signs for &ldquo;{html_mod.escape(phrase)}&rdquo;</title>
<style>
  :root {{
    --bg:#0f1115; --panel:#1b1f27; --ink:#e8e8ea; --muted:#8b93a1;
    --accent:#5b9dff; --line:#2a2f3a;
  }}
  * {{ box-sizing:border-box; }}
  body {{
    margin:0; background:var(--bg); color:var(--ink);
    font:15px/1.45 -apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;
  }}
  header {{
    padding:28px 24px 12px; max-width:1100px; margin:0 auto;
  }}
  header h1 {{ font-size:22px; margin:0 0 4px; }}
  header .phrase {{ color:var(--accent); }}
  header .meta {{ color:var(--muted); font-size:13px; }}
  main {{ padding:8px 24px 48px; max-width:1100px; margin:0 auto; }}
  .grid {{
    display:grid; gap:16px;
    grid-template-columns:repeat(auto-fill,minmax(220px,1fr));
  }}
  .card {{
    background:var(--panel); border:1px solid var(--line); border-radius:12px;
    padding:14px; display:flex; flex-direction:column; gap:6px;
  }}
  .card.no-match {{ opacity:.7; }}
  .thumb {{
    width:100%; aspect-ratio:16/9; object-fit:cover; border-radius:8px;
    background:#000; display:flex; align-items:center; justify-content:center;
  }}
  .thumb-empty {{
    color:var(--muted); font-size:13px;
    background:repeating-linear-gradient(45deg,#161a21,#161a21 10px,#12151b 10px,#12151b 20px);
  }}
  .card h3 {{ margin:4px 0 0; font-size:17px; }}
  .gloss {{ margin:0; color:#cfcfd4; font-style:italic; }}
  .category {{ margin:0; color:var(--muted); font-size:12px; }}
  .video-link {{
    margin-top:auto; display:inline-block; color:var(--bg);
    background:var(--accent); text-decoration:none; padding:8px 12px;
    border-radius:8px; font-weight:600; text-align:center;
  }}
  .video-link:hover {{ filter:brightness(1.1); }}
  .muted {{ color:var(--muted); }}
</style>
</head>
<body>
<header>
  <h1>BIM signs for <span class="phrase">&ldquo;{html_mod.escape(phrase)}&rdquo;</span></h1>
  <p class="meta">{html_mod.escape(summary)} &middot; generated {now} &middot; source <a style="color:var(--accent)" href="https://bimsignbank.org" target="_blank" rel="noopener">bimsignbank.org</a></p>
</header>
<main>
  <div class="grid">{cards_html}
  </div>
</main>
</body>
</html>
"""


# --------------------------------------------------------------------------- #
# Web server
# --------------------------------------------------------------------------- #
def render_search_page(initial_q: str = "") -> str:
    """The empty search page shell. Results are injected by render_results_html."""
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>BIM Sign Lookup</title>
<style>{_SHARED_CSS}{_INDEX_CSS}</style>
</head>
<body>
<div class="wrap">
  <h1>BIM Sign Lookup</h1>
  <p class="subtitle">Type a phrase to find the {html_mod.escape("Bahasa Isyarat Malaysia (BIM)")} signs</p>
  <form class="search" id="form" action="/search" method="get">
    <input type="text" id="q" name="q" value="{html_mod.escape(initial_q)}"
           placeholder="e.g. chicken pox" autofocus
           aria-label="phrase to look up">
    <button type="submit">Find signs</button>
  </form>
  <div id="results"></div>
  <footer>
    <span>Powered by <a href="https://bimsignbank.org" target="_blank" rel="noopener">bimsignbank.org</a></span>
  </footer>
</div>
<script>{_JS}</script>
</body>
</html>
"""


def render_results_html(phrase: str, results: list[tuple[str, dict | None]],
                        mode: str) -> str:
    """Render only the results section (an HTML fragment) for a search.

    Used both server-side (first paint) and client-side (HTMX-less fetch).
    """
    cards = []
    for token, rec in results:
        if rec is None:
            cards.append(
                f'<article class="card no-match">'
                f'<div class="thumb thumb-empty" aria-hidden="true">—</div>'
                f'<h3>{html_mod.escape(token)}</h3>'
                f'<p class="gloss">No match found</p>'
                f'</article>'
            )
            continue

        media = media_for(rec)
        word = html_mod.escape(rec.get("word") or token)
        perkataan = html_mod.escape(rec.get("perkataan") or "")
        category = html_mod.escape(rec.get("group_category") or "")
        embed = media["embed"]
        video = media["video"]
        vocab = media["vocab_image"]

        if embed:
            # Click-to-play overlay on the thumbnail; loads the iframe only
            # when clicked so the page doesn't fetch N YouTube players at once.
            thumb = media["thumbnail"] or ""
            video_html = (
                f'<div class="player" data-embed="{embed}">'
                f'<img class="thumb" src="{thumb}" alt="{word}">'
                f'<button class="play" aria-label="Play video">&#9658;</button>'
                f'</div>'
            )
        elif vocab:
            video_html = (
                f'<img class="thumb" src="{vocab}" alt="{word}">'
            )
        else:
            video_html = (
                '<div class="thumb thumb-empty" aria-hidden="true">no video</div>'
            )

        watch_link = (
            f'<a class="video-link" href="{html_mod.escape(video)}" '
            f'target="_blank" rel="noopener">&#9658; Watch on YouTube</a>'
            if video else ""
        )

        cards.append(
            f'<article class="card">'
            f'{video_html}'
            f'<h3>{word}</h3>'
            f'<p class="gloss">{perkataan}</p>'
            f'<p class="category">{category}</p>'
            f'{watch_link}'
            f'</article>'
        )

    cards_html = "\n".join(cards)
    matched = sum(1 for _, r in results if r is not None)
    total = len(results)
    summary = (f"{matched}/{total} word{'s' if total != 1 else ''} matched"
               if total else "no words to look up")

    header = (
        f'<p class="result-head">Signs for '
        f'<span class="q">{html_mod.escape(phrase)}</span> '
        f'<span class="count">{html_mod.escape(summary)}</span></p>'
    )
    empty = '<p class="empty">No words to look up.</p>' if not results else ""
    return (
        f'{header}{empty}'
        f'<div class="grid">{cards_html}</div>'
    )


# A single CSS string, split so the index page and the (already existing)
# report page can share the visual language without duplicating it.
_SHARED_CSS = """
  :root {
    --bg:#0f1115; --panel:#1b1f27; --ink:#e8e8ea; --muted:#8b93a1;
    --accent:#5b9dff; --accent-dim:#3a6fb8; --line:#2a2f3a;
  }
  * { box-sizing:border-box; }
  body {
    margin:0; background:var(--bg); color:var(--ink);
    font:15px/1.45 -apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;
  }
  a { color:var(--accent); }
  .wrap { max-width:1100px; margin:0 auto; padding:24px; }
  .grid {
    display:grid; gap:16px;
    grid-template-columns:repeat(auto-fill,minmax(240px,1fr));
  }
  .card {
    background:var(--panel); border:1px solid var(--line); border-radius:12px;
    padding:14px; display:flex; flex-direction:column; gap:6px;
  }
  .card.no-match { opacity:.7; }
  .thumb {
    width:100%; aspect-ratio:16/9; object-fit:cover; border-radius:8px;
    background:#000; display:flex; align-items:center; justify-content:center;
  }
  .thumb-empty {
    color:var(--muted); font-size:13px;
    background:repeating-linear-gradient(45deg,#161a21,#161a21 10px,#12151b 10px,#12151b 20px);
  }
  .card h3 { margin:4px 0 0; font-size:17px; }
  .gloss { margin:0; color:#cfcfd4; font-style:italic; }
  .category { margin:0; color:var(--muted); font-size:12px; }
"""

_INDEX_CSS = """
  h1 { font-size:26px; margin:8px 0 4px; }
  .subtitle { color:var(--muted); margin:0 0 18px; }
  .search {
    display:flex; gap:8px; margin-bottom:20px;
  }
  .search input {
    flex:1; padding:12px 14px; font-size:16px;
    background:var(--panel); color:var(--ink);
    border:1px solid var(--line); border-radius:10px; outline:none;
  }
  .search input:focus { border-color:var(--accent); }
  .search button {
    padding:12px 18px; font-size:15px; font-weight:600;
    background:var(--accent); color:var(--bg); border:none; border-radius:10px;
    cursor:pointer;
  }
  .search button:hover { filter:brightness(1.1); }
  .result-head { color:var(--muted); margin:0 0 14px; font-size:14px; }
  .result-head .q { color:var(--ink); font-weight:600; }
  .result-head .count { color:var(--accent); margin-left:6px; }
  .empty { color:var(--muted); }
  .player { position:relative; cursor:pointer; }
  .player .play {
    position:absolute; top:50%; left:50%; transform:translate(-50%,-50%);
    width:54px; height:54px; border:none; border-radius:50%;
    background:rgba(91,157,255,.92); color:#fff; font-size:20px;
    cursor:pointer; display:flex; align-items:center; justify-content:center;
    transition:transform .1s ease;
  }
  .player:hover .play { transform:translate(-50%,-50%) scale(1.08); }
  .player iframe { width:100%; aspect-ratio:16/9; border:none; border-radius:8px; }
  .video-link {
    margin-top:auto; display:inline-block; color:var(--bg);
    background:var(--accent); text-decoration:none; padding:8px 12px;
    border-radius:8px; font-weight:600; text-align:center; font-size:14px;
  }
  .video-link:hover { filter:brightness(1.1); }
  footer { margin-top:36px; color:var(--muted); font-size:12px; }
"""

_JS = """
  // Click-to-play: swap the thumbnail overlay for the actual YouTube iframe.
  document.addEventListener('click', function (e) {
    var btn = e.target.closest('.player .play, .player');
    if (!btn) return;
    var player = e.target.closest('.player');
    if (!player || player.dataset.loaded === '1') return;
    player.dataset.loaded = '1';
    var src = player.dataset.embed;
    player.innerHTML =
      '<iframe src="' + src + '?autoplay=1&rel=0" title="sign video" '
      + 'allow="autoplay; encrypted-media; picture-in-picture" '
      + 'allowfullscreen></iframe>';
  });

  // AJAX submit so we don't do a full page reload on each search.
  var form = document.getElementById('form');
  var results = document.getElementById('results');
  form.addEventListener('submit', function (e) {
    e.preventDefault();
    var q = document.getElementById('q').value.trim();
    if (!q) { results.innerHTML = ''; return; }
    results.innerHTML = '<p class="empty">Searching…</p>';
    fetch('/search?q=' + encodeURIComponent(q), { headers: {'X-Requested-With':'fetch'} })
      .then(function (r) { return r.ok ? r.text() : Promise.reject(r.status); })
      .then(function (html) {
        results.innerHTML = html;
        document.title = 'BIM signs for "' + q + '"';
      })
      .catch(function () {
        results.innerHTML = '<p class="empty">Something went wrong. Try again.</p>';
      });
  });
"""


class _Handler(BaseHTTPRequestHandler):
    server_version = "BIMLookup/1.0"

    def log_message(self, fmt, *args):  # quieter logging
        sys.stderr.write("%s - - %s\n" % (self.address_string(), fmt % args))

    def _send(self, body: bytes, status: int = 200,
              content_type: str = "text/html; charset=utf-8") -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        parsed = urllib.parse.urlsplit(self.path)
        if parsed.path == "/":
            body = render_search_page().encode("utf-8")
            self._send(body)
            return

        if parsed.path == "/search":
            qs = urllib.parse.parse_qs(parsed.query)
            phrase = (qs.get("q", [""])[0]).strip()
            if not phrase:
                self._send(b"", status=302)
                self.send_header("Location", "/")
                self.end_headers()
                return
            try:
                _, mode, results = search_phrase(phrase)
            except Exception as exc:  # never crash the server on a lookup error
                body = (f'<p class="empty">Lookup failed: '
                        f'{html_mod.escape(str(exc))}</p>').encode("utf-8")
                self._send(body, status=500)
                return
            fragment = render_results_html(phrase, results, mode)
            is_fetch = self.headers.get("X-Requested-With", "") == "fetch"
            if is_fetch:
                # AJAX request: just the results fragment.
                self._send(fragment.encode("utf-8"))
            else:
                # Full page (first paint, direct link, reload).
                page = render_search_page(initial_q=phrase)
                page = page.replace(
                    '<div id="results"></div>',
                    f'<div id="results">{fragment}</div>',
                )
                self._send(page.encode("utf-8"))
            return

        if parsed.path == "/healthz":
            self._send(b"ok", content_type="text/plain; charset=utf-8")
            return

        self._send(b"not found", status=404)


class _ReusableHTTPServer(ThreadingHTTPServer):
    # NOTE: deliberately NOT setting allow_reuse_address=True here. Python's
    # HTTPServer sets SO_REUSEADDR by default, but on Windows that flag lets
    # *two different processes* bind the same port simultaneously -- so a
    # second `serve` would silently grab 8000 too and both would fight over
    # connections (browser sees a blank/dropped page). Disabling it makes
    # bind() strict, so a live listener reliably raises OSError and we bump
    # to the next free port.
    allow_reuse_address = False
    daemon_threads = True


def _port_has_live_server(port: int) -> bool:
    """True if something is actively accepting connections on 127.0.0.1:port.

    We probe rather than rely on bind() because SO_REUSEADDR semantics differ
    between Windows and Unix; a connect attempt is an unambiguous signal that
    a real server is already there.
    """
    s = socket.socket()
    s.settimeout(0.2)
    try:
        s.connect(("127.0.0.1", port))
        return True
    except OSError:
        return False
    finally:
        s.close()


def _bind_or_next_port(port: int) -> "_ReusableHTTPServer | None":
    """Bind on the first free port at or after `port`.

    Skips any port that already has a live server (a previous `serve` still
    running) and any port that fails to bind. Returns the bound server, or
    None if port..port+20 are all unavailable.
    """
    for candidate in range(port, port + 20):
        if _port_has_live_server(candidate):
            continue  # another instance is already serving here
        try:
            return _ReusableHTTPServer(("127.0.0.1", candidate), _Handler)
        except OSError:
            continue  # e.g. socket in a lingering state -> try next
    return None


def serve(port: int, open_browser: bool) -> int:
    """Run the local web server until interrupted."""
    httpd = _bind_or_next_port(port)
    if httpd is None:
        print(f"[error] could not bind to any port in {port}..{port+19}; "
              f"is another instance already running? Try a different "
              f"--port, or stop the other server first.", file=sys.stderr)
        return 1
    bound_port = httpd.server_address[1]
    url = f"http://127.0.0.1:{bound_port}/"
    if bound_port != port:
        print(f"[info] port {port} was busy; using {bound_port} instead",
              file=sys.stderr)
    print(f"[info] serving BIM sign lookup on {url}  (Ctrl-C to stop)",
          file=sys.stderr)

    # Open the browser only AFTER the socket is actually accepting
    # connections, so the first page load never races the server startup
    # (which otherwise can land on a blank/cached page on Windows).
    def _open_when_ready() -> None:
        import socket as _sock
        for _ in range(50):  # up to ~5s
            s = _sock.socket()
            s.settimeout(0.2)
            try:
                s.connect(("127.0.0.1", bound_port))
                s.close()
                break
            except OSError:
                s.close()
        else:
            return
        webbrowser.open(url)

    if open_browser:
        threading.Thread(target=_open_when_ready, daemon=True).start()

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n[info] shutting down", file=sys.stderr)
    finally:
        httpd.server_close()
    return 0


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def search_phrase(phrase: str) -> tuple[str, str,
                                        list[tuple[str, dict | None]]]:
    """Run a phrase/word lookup. Shared by the CLI and the web server.

    Tries the whole phrase as a single dictionary entry first (for entries
    like "Chicken Pox"), then falls back to per-word lookup. Returns
    (phrase, mode, results) where mode is "phrase" or "words".
    """
    tokens = tokenize(phrase)
    if not tokens:
        return phrase, "words", []

    results: list[tuple[str, dict | None]] = []
    full = " ".join(tokens) if len(tokens) > 1 else None

    # Phrase-as-single-entry attempt.
    if full:
        try:
            rec = lookup_word(full)
        except RuntimeError:
            rec = None
        if rec is not None:
            results.append((full, rec))
            return phrase, "phrase", results

    # Per-word fallback.
    for token in tokens:
        try:
            rec = lookup_word(token)
        except RuntimeError:
            rec = None
        results.append((token, rec))
    return phrase, "words", results


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Look up BIM (Malaysian Sign Language) signs for a phrase.",
        epilog='examples:\n'
               '  python bim_lookup.py "good morning"   # one-shot HTML report\n'
               '  python bim_lookup.py serve             # interactive web app',
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("phrase", nargs="*",
                        help='the phrase to look up, e.g. "good morning" '
                             '(omit when using "serve")')
    parser.add_argument("--no-open", action="store_true",
                        help="one-shot mode: write the report but don't open "
                             "the browser")
    parser.add_argument("--clear-cache", action="store_true",
                        help="clear the response cache before running")
    # serve options (parsed only when the first positional is "serve")
    parser.add_argument("--port", type=int, default=8000,
                        help="port for 'serve' mode (default: 8000)")
    parser.add_argument("--no-browser", action="store_true",
                        help="serve mode: don't auto-open the browser")
    args = parser.parse_args(argv)

    if args.clear_cache:
        try:
            CACHE_FILE.unlink()
        except FileNotFoundError:
            pass
        global _cache
        _cache = {}
        print("[info] cache cleared", file=sys.stderr)

    # Read url.txt (kept as the source of the site identity).
    read_site_url()

    # `serve` subcommand: run the interactive web app.
    if args.phrase and args.phrase[0] == "serve":
        return serve(args.port, open_browser=not args.no_browser)

    if not args.phrase:
        # No phrase and not serving -> show help.
        parser.print_help()
        return 0

    phrase = " ".join(args.phrase).strip()
    if not phrase:
        parser.error("a non-empty phrase is required")

    tokens = tokenize(phrase)
    if not tokens:
        parser.error(f"no usable words in phrase: {phrase!r}")

    results: list[tuple[str, dict | None]] = []

    # Multi-word phrase: try the whole phrase as a single dictionary entry
    # first (entries like "Chicken Pox", "Swollen Eye" are stored as one
    # sign). Only if that misses do we fall back to per-word lookup, so
    # "chicken pox" no longer silently splits into (wrong) "Chicken" + miss.
    full = " ".join(tokens) if len(tokens) > 1 else None
    phrase_hit = False
    if full:
        print(f"[info] trying phrase {full!r} as a single entry...",
              file=sys.stderr)
        try:
            rec = lookup_word(full)
        except RuntimeError as exc:
            print(f"[warn] phrase lookup failed for {full!r}: {exc}",
                  file=sys.stderr)
            rec = None
        phrase_hit = rec is not None
        if phrase_hit:
            results.append((full, rec))
            print(f"  {full:<20} -> {rec['word']!r} "
                  f"({rec.get('group_category') or 'no category'}) [phrase match]",
                  file=sys.stderr)

    if not phrase_hit:
        if full:
            print(f"[info] no single entry for {full!r}; "
                  f"looking up words individually.", file=sys.stderr)
        print(f"[info] looking up {len(tokens)} word"
              f"{'s' if len(tokens) != 1 else ''}: {', '.join(tokens)}",
              file=sys.stderr)
        for token in tokens:
            try:
                rec = lookup_word(token)
            except RuntimeError as exc:
                print(f"[warn] lookup failed for {token!r}: {exc}",
                      file=sys.stderr)
                rec = None
            results.append((token, rec))
            status = (f"{rec['word']!r} "
                      f"({rec.get('group_category') or 'no category'})"
                      if rec else "no match")
            print(f"  {token:<20} -> {status}", file=sys.stderr)

    html_out = render_report(phrase, results)
    REPORT_FILE.write_text(html_out, encoding="utf-8")
    print(f"\n[done] report written to {REPORT_FILE}", file=sys.stderr)

    if not args.no_open:
        webbrowser.open(REPORT_FILE.as_uri())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
