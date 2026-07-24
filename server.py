"""
Web host for Memory Match: serves the game and the shared high score board.

Everything is self-contained: on first start the server downloads the
pygbag runtime (CPython wasm + pygame wheel, about 15 MB) into
build/web/cdn and rewrites index.html to use those local files, so no
external CDN and no service worker is involved at runtime.

Build the browser version first (requires: pip install pygbag):

    py -m pygbag --build .

Then start this server and open http://localhost:8080 in a browser:

    py server.py            # port 8080
    py server.py 8000       # any other port

NOTE: the pygbag runtime normally fetches the pygame wheel from its public
CDN - or, when the page URL contains //localhost:, from whatever server
runs on that port (which breaks the game unless it is pygbag's own test
server). This server patches the loader on the fly to always use its local
mirror instead, so the game works on any host. If you ever ran
"py -m pygbag ." (its test server), your browser may also hold a stale
service worker for localhost:8000 - the default port 8080 avoids it.

Static game files are served from build/web over HTTP/1.1 keep-alive;
big runtime files are precompressed (.gz siblings, created once) and
served gzipped when the browser accepts it. Version-pinned runtime
files are served with an immutable cache header, everything else with
no-cache so a normal reload never shows a stale build. High scores are
stored in leaderboard.json inside a data directory and shared by every
connected player. The data directory is /data (the mounted volume) when
running on Railway, and ../data next to this project folder when running
locally.

API (used by the game itself, same origin, so no CORS issues):
    GET /api/scores                                          -> the whole board
    GET /api/scores?name=..&difficulty=..&moves=..&time_ms=.. -> add + the board

Only the Python standard library is needed.
"""

import gzip
import json
import mimetypes
import os
import re
import shutil
import sys
import threading
import urllib.request
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

BASE_DIR = Path(__file__).resolve().parent
WEB_DIR = BASE_DIR / "build" / "web"

# Railway always sets RAILWAY_ENVIRONMENT; there we use the mounted /data
# volume so scores survive redeploys. Locally we use ../data next to the
# project folder.
DATA_DIR = Path("/data") if os.environ.get("RAILWAY_ENVIRONMENT") else BASE_DIR.parent / "data"
LEADERBOARD_PATH = DATA_DIR / "leaderboard.json"

RUNTIME_CDN = "https://pygame-web.github.io/cdn/"

# Injected into index.html so the game is playable on phones/tablets: a proper
# viewport (no pinch/double-tap zoom fighting the canvas), no page scrolling
# or pull-to-refresh, and no accidental text selection when tapping cards.
# Done here (not by editing build/web/index.html) because "pygbag --build"
# regenerates that file.
MOBILE_VIEWPORT = ('<meta name="viewport" content="width=device-width, '
                   'initial-scale=1.0, maximum-scale=1.0, user-scalable=no, '
                   'viewport-fit=cover">')
MOBILE_STYLE = """<style>
html, body { margin: 0; overflow: hidden; overscroll-behavior: none; }
body { position: fixed; inset: 0; }
canvas.emscripten { touch-action: none; -webkit-user-select: none; user-select: none; }
</style>"""

# WebAssembly files must be served with this type or the browser refuses
# to compile them (Windows registries often lack the mapping).
mimetypes.add_type("application/wasm", ".wasm")


# ---------------------------------------------------------------------------
# pygbag runtime mirroring (one-time download, then fully offline)
# ---------------------------------------------------------------------------

def _build_versions():
    """Detect the pygbag / cpython / pygame versions of the current build."""
    version, py, pg = "0.9.3", "312", "2.5.7"
    try:
        html = (WEB_DIR / "index.html").read_text(encoding="utf-8")
        m = re.search(r"cdn/(\d+\.\d+\.\d+)/", html)
        if m:
            version = m.group(1)
        m = re.search(r"cpython(\d+)", html)
        if m:
            py = m.group(1)
    except OSError:
        pass
    try:
        reqs = (BASE_DIR / "requirements.txt").read_text(encoding="utf-8")
        m = re.search(r"pygame-?ce\s*>?=\s*([\d.]+)", reqs)
        if m:
            pg = m.group(1)
    except OSError:
        pass
    return version, py, pg


def runtime_files():
    """Files the pygbag loader fetches, relative to the CDN root."""
    version, py, pg = _build_versions()
    return [
        f"{version}/pythons.js",
        f"{version}/empty.html",
        f"{version}/browserfs.min.js",
        f"{version}/cpythonrc.py",           # optional startup script
        f"{version}/cpython{py}/main.js",
        f"{version}/cpython{py}/main.data",
        f"{version}/cpython{py}/main.wasm",
        f"index-{version}-cp{py}.json",
        f"cp{py}/pygame_ce-{pg}-cp{py}-cp{py}-wasm32_bi_emscripten.whl",
        "vtx.js",
        "vt/xterm.css",
        "vt/xterm.js",
        "vt/xterm-addon-image.js",
    ]


def ensure_runtime():
    """Download missing runtime files into build/web/cdn (runs once)."""
    for rel in runtime_files():
        dest = WEB_DIR / "cdn" / rel
        if dest.is_file():
            continue
        url = RUNTIME_CDN + rel
        print(f"downloading {url}")
        dest.parent.mkdir(parents=True, exist_ok=True)
        try:
            with urllib.request.urlopen(url, timeout=120) as response:
                dest.write_bytes(response.read())
        except OSError as exc:
            # Optional files (loader works without them) - serve an empty
            # stub so the request still succeeds locally.
            if rel.endswith(("cpythonrc.py", "browserfs.min.js")):
                dest.write_bytes(b"")
            else:
                raise SystemExit(f"error: could not download {url}: {exc}")


# Big static files get a precompressed .gz sibling so they can be served
# with Content-Encoding: gzip (main.wasm alone shrinks from 13 MB to ~4 MB).
MIN_COMPRESS_SIZE = 64 * 1024
COMPRESSIBLE_SUFFIXES = (".wasm", ".data", ".js", ".whl", ".json", ".css")


def ensure_compressed():
    """Create .gz siblings for big static files (one-time, cached on disk)."""
    for path in WEB_DIR.rglob("*"):
        if not path.is_file() or not path.name.endswith(COMPRESSIBLE_SUFFIXES):
            continue
        try:
            if path.stat().st_size < MIN_COMPRESS_SIZE:
                continue
        except OSError:
            continue
        gz = path.with_name(path.name + ".gz")
        if gz.exists() and gz.stat().st_mtime >= path.stat().st_mtime:
            continue  # already compressed and up to date
        print(f"compressing {path.relative_to(WEB_DIR)}")
        gz.write_bytes(gzip.compress(path.read_bytes(), compresslevel=6))


def patched_index():
    """index.html with CDN URLs rewritten to the local mirror, plus mobile tweaks.

    The replacement must be an absolute path (leading slash): the pygbag
    loader passes it to dynamic import(), and import("cdn/...") without a
    slash would be an invalid bare module specifier.

    The viewport/style injection makes the page behave like an app on touch
    devices (no zooming, scrolling or text selection while playing).

    ume_block=0 skips pygbag's "Ready to start ! Please click/touch page"
    gate so the game starts by itself once loading finishes. Browsers may
    still keep audio muted until the first tap; SDL resumes the audio
    context automatically on that first interaction, and the game is
    fully tap-driven anyway.

    The game archive URL gets a ?v=<mtime> query (cache busting): after a
    rebuild the URL changes, so even a browser holding a stale cached copy
    of the old archive fetches the new one on the next load. index.html
    itself is always served fresh (see Handler.end_headers), so the new
    URL reaches every client right away.
    """
    html = (WEB_DIR / "index.html").read_text(encoding="utf-8")
    m = re.search(r'archive\s*:\s*"([^"]+)"', html)
    if m:
        name = m.group(1)
        for ext in (".apk", ".tar.gz"):
            try:
                version = int((WEB_DIR / (name + ext)).stat().st_mtime)
            except OSError:
                continue  # archive variant not present - leave the URL alone
            html = html.replace(f'"{name}{ext}"', f'"{name}{ext}?v={version}"')
    html = html.replace(RUNTIME_CDN, "/cdn/")
    html = html.replace(
        '<meta name="viewport" content="width=device-width, initial-scale=1.0">',
        MOBILE_VIEWPORT)
    html = html.replace("</head>", MOBILE_STYLE + "\n</head>", 1)
    html, replaced = re.subn(r"ume_block\s*:\s*1", "ume_block : 0", html, count=1)
    if not replaced and "ume_block" not in html:
        html = html.replace("config = {", "config = {\n    ume_block : 0,", 1)
    return html.encode("utf-8")


def patched_pythons_js():
    """pythons.js patched so every download goes to the local mirror.

    cross_file() is the single funnel for all runtime downloads (package
    index, wheels, game archive). The Python side decides per repo whether
    to use the public CDN or the page origin - rewriting the URL here
    catches every case, no matter which repo was picked.
    """
    version, _py, _pg = _build_versions()
    path = WEB_DIR / "cdn" / version / "pythons.js"
    text = path.read_text(encoding="utf-8")
    needle = "window.cross_file = function * cross_file(url, store, flags) {"
    inject = (needle + '\n    url = String(url)'
              '.replace(/^https?:\\/\\/[^/]+\\/cdn\\//, "/cdn/");')
    if needle not in text:
        raise SystemExit("error: could not patch pythons.js (pygbag changed?)")
    return text.replace(needle, inject).encode("utf-8")


def patched_cpythonrc():
    """cpythonrc.py patched to fetch packages from the local mirror only.

    The original enables "pygbag dev mode" (wheels loaded from the page's
    own origin under /cdn/) only when the URL contains //localhost: - and
    uses the public CDN everywhere else. Our mirror has all files locally,
    so always use it: this is what makes the game work on any host, not
    just on pygbag's own test server.
    """
    version, _py, _pg = _build_versions()
    path = WEB_DIR / "cdn" / version / "cpythonrc.py"
    text = path.read_text(encoding="utf-8")
    text = text.replace(
        'PYCONFIG_PKG_INDEXES_DEV = ["http://localhost:<port>/cdn/"]',
        'PYCONFIG_PKG_INDEXES_DEV = ["/cdn/"]')
    text = text.replace(
        "if (PyConfig.dev_mode > 0) or PyConfig.pygbag:",
        "if True:  # patched by server.py: always use the local package mirror")
    return text.encode("utf-8")


# ---------------------------------------------------------------------------
# Shared leaderboard
# ---------------------------------------------------------------------------

DIFFICULTIES = ("Easy", "Medium", "Hard", "Expert", "Soulless")
MAX_HIGHSCORES = 10
MAX_NAME_LENGTH = 12
MAX_MOVES = 100_000        # sanity limits against junk submissions
MAX_TIME_MS = 3_600_000    # one hour

_lock = threading.Lock()   # leaderboard file access


def empty_board():
    """One empty list per difficulty."""
    return {label: [] for label in DIFFICULTIES}


def normalize_board(data):
    """Keep only known difficulties and well-formed entries, sorted and capped."""
    board = empty_board()
    if not isinstance(data, dict):
        return board
    for label in board:
        for entry in data.get(label, []):
            try:
                board[label].append({
                    "name": str(entry["name"]),
                    "moves": int(entry["moves"]),
                    "time_ms": int(entry["time_ms"]),
                })
            except (KeyError, TypeError, ValueError):
                continue  # skip malformed entries
        board[label].sort(key=lambda e: (e["moves"], e["time_ms"]))
        del board[label][MAX_HIGHSCORES:]
    return board


def load_board():
    """Read leaderboard.json; start fresh if it is missing or corrupt."""
    try:
        data = json.loads(LEADERBOARD_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = None
    return normalize_board(data)


def add_entry(name, difficulty, moves, time_ms):
    """Insert an entry, keep the best ones, save, and return the whole board."""
    with _lock:
        board = load_board()
        entries = board[difficulty]
        entries.append({"name": name, "moves": moves, "time_ms": time_ms})
        entries.sort(key=lambda e: (e["moves"], e["time_ms"]))
        del entries[MAX_HIGHSCORES:]
        LEADERBOARD_PATH.write_text(json.dumps(board, indent=2), encoding="utf-8")
        return board


def parse_submission(query):
    """Validate query string parameters; return (name, difficulty, moves,
    time_ms) or None if anything is missing or out of range."""
    params = parse_qs(query)
    try:
        name = params.get("name", [""])[0].strip() or "Player"
        difficulty = params["difficulty"][0]
        moves = int(params["moves"][0])
        time_ms = int(params["time_ms"][0])
    except (KeyError, IndexError, ValueError):
        return None
    if difficulty not in DIFFICULTIES:
        return None
    if not 1 <= moves <= MAX_MOVES or not 0 <= time_ms <= MAX_TIME_MS:
        return None
    return name[:MAX_NAME_LENGTH], difficulty, moves, time_ms


# ---------------------------------------------------------------------------
# HTTP handler: API + patched index.html + static files
# ---------------------------------------------------------------------------

class Handler(SimpleHTTPRequestHandler):
    """Static files from build/web plus the /api/scores endpoints."""

    # HTTP/1.1 keep-alive: all runtime files load over one connection
    # instead of a new TCP connection per file.
    protocol_version = "HTTP/1.1"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(WEB_DIR), **kwargs)

    def end_headers(self):
        # Version-pinned runtime files (the path embeds the pygbag /
        # cpython / pygame versions) never change for a given URL, so the
        # browser may keep them forever without even revalidating. The two
        # files patched per request and the unversioned vt/* files must
        # still revalidate, like everything else: a normal reload must
        # never show a stale build. Revalidation is cheap - unchanged
        # files answer 304 via Last-Modified.
        if (self.path.startswith("/cdn/")
                and not self.path.startswith(("/cdn/vt/", "/cdn/vtx.js"))
                and not self.path.endswith(("pythons.js", "cpythonrc.py"))):
            self.send_header("Cache-Control", "max-age=31536000, immutable")
        else:
            self.send_header("Cache-Control", "no-cache")
        super().end_headers()

    def _send_bytes(self, body, content_type, status=200):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        # Allow the API to be used even when the game is hosted elsewhere.
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, obj, status=200):
        self._send_bytes(json.dumps(obj).encode("utf-8"), "application/json", status)

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/api/scores":
            if parsed.query:
                submission = parse_submission(parsed.query)
                if submission is None:
                    self._send_json({"error": "invalid submission"}, status=400)
                    return
                board = add_entry(*submission)
            else:
                with _lock:
                    board = load_board()
            self._send_json(board)
        elif parsed.path in ("/", "/index.html"):
            try:
                self._send_bytes(patched_index(), "text/html; charset=utf-8")
            except OSError:
                self.send_error(404, "index.html not found - run: py -m pygbag --build .")
        elif parsed.path.endswith("/cpythonrc.py"):
            try:
                self._send_bytes(patched_cpythonrc(), "text/x-python; charset=utf-8")
            except OSError:
                self.send_error(404)
        elif parsed.path.endswith("/pythons.js"):
            try:
                self._send_bytes(patched_pythons_js(), "text/javascript; charset=utf-8")
            except OSError:
                self.send_error(404)
        else:
            # Static game/runtime file: serve the precompressed copy when
            # the browser accepts gzip and one exists on disk.
            if "gzip" in self.headers.get("Accept-Encoding", ""):
                local = Path(self.translate_path(self.path))
                gz = local.with_name(local.name + ".gz")
                if gz.is_file():
                    self.send_response(200)
                    self.send_header("Content-Type", self.guess_type(str(local)))
                    self.send_header("Content-Encoding", "gzip")
                    self.send_header("Content-Length", str(gz.stat().st_size))
                    self.send_header("Vary", "Accept-Encoding")
                    self.end_headers()
                    with gz.open("rb") as f:
                        shutil.copyfileobj(f, self.wfile)
                    return
            super().do_GET()  # static game/runtime file


def main():
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8080
    if not WEB_DIR.is_dir():
        raise SystemExit(f"error: {WEB_DIR} not found - build the game first with:\n"
                         "    py -m pygbag --build .")
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    ensure_runtime()
    ensure_compressed()
    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    print(f"Serving Memory Match on http://localhost:{port} (Ctrl+C to stop)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
