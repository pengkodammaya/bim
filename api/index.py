"""Vercel serverless entry point.

A thin Flask app that reuses all of bim_lookup's logic (search_phrase,
render_search_page, render_results_html) without duplicating it. Vercel
auto-detects Flask and routes requests here.

Locally you can run it with:
    flask --app api.index run --port 8000
or use the stdlib server directly:  python bim_lookup.py serve
"""
import html as html_mod
import sys
from pathlib import Path

# Make the repo root importable so `from bim_lookup import ...` works whether
# this file is run from the project root (local) or bundled under api/
# (Vercel).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from flask import Flask, Response, request  # noqa: E402

from bim_lookup import (  # noqa: E402
    render_results_html,
    render_search_page,
    search_phrase,
)

app = Flask(__name__)


@app.route("/")
def index():
    # Flask auto-appends "; charset=utf-8" for text/* mimetypes, so we pass
    # the bare mimetype to avoid a duplicated charset in the header.
    return Response(render_search_page(), mimetype="text/html")


@app.route("/search")
def search():
    phrase = (request.args.get("q") or "").strip()
    if not phrase:
        # Mirror the stdlib server's behaviour: bounce to the index.
        return Response(status=302, headers={"Location": "/"})
    try:
        _, mode, results = search_phrase(phrase)
    except Exception as exc:  # never 500 the whole function on a lookup error
        return Response(
            f'<p class="empty">Lookup failed: {html_mod.escape(str(exc))}</p>',
            status=500,
            mimetype="text/html",
        )
    fragment = render_results_html(phrase, results, mode)
    return Response(fragment, mimetype="text/html")


@app.route("/healthz")
def healthz():
    return Response("ok", mimetype="text/plain")
