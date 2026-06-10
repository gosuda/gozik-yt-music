"""
handlers/webui.py — Stand-alone web UI for gozik-yt-music.

Serves a minimal browser-based dashboard on a dedicated HTTP port so users
can authenticate, view status, and manage credentials without the gozik
desktop application.

Uses only the Python standard library (http.server) — no extra dependencies.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any, Dict, Optional

import grpc

from generated import music_provider_pb2 as pb

logger = logging.getLogger("gozik.ytmusic.webui")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
DEFAULT_WEBUI_PORT = 50052

HTML_PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>gozik YouTube Music Plugin</title>
<style>
:root{--bg:#0f0f0f;--card:#1a1a1a;--text:#e8e8e8;--muted:#999;--accent:#ff0033;--accent2:#1db954;--border:#2a2a2a;}
*{box-sizing:border-box}
body{margin:0;font-family:system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;background:var(--bg);color:var(--text);display:flex;justify-content:center;padding:2rem 1rem}
.container{width:100%;max-width:640px}
h1{margin:0 0 .25rem;font-size:1.6rem}
.sub{color:var(--muted);margin-bottom:1.5rem}
.card{background:var(--card);border:1px solid var(--border);border-radius:12px;padding:1.25rem;margin-bottom:1rem}
.card h2{margin:0 0 .75rem;font-size:1.1rem}
.status{display:inline-flex;align-items:center;gap:.5rem;padding:.35rem .7rem;border-radius:999px;font-size:.85rem;font-weight:600}
.status.ok{background:rgba(29,185,84,.15);color:var(--accent2)}
.status.no{background:rgba(255,0,51,.15);color:var(--accent)}
pre{background:#111;border:1px solid var(--border);border-radius:8px;padding:.75rem;overflow-x:auto;font-size:.9rem;color:var(--text)}
.code{font-family:ui-monospace,SFMono-Regular,Menlo,Monaco,Consolas,monospace;font-size:1.1rem;letter-spacing:.05em;background:#111;padding:.5rem .75rem;border-radius:8px;border:1px dashed var(--border);display:inline-block;margin:.25rem 0}
button{cursor:pointer;border:none;border-radius:8px;padding:.6rem 1.2rem;font-size:.9rem;font-weight:600;background:var(--accent);color:#fff;transition:opacity .15s}
button:hover{opacity:.9}
button:disabled{opacity:.5;cursor:not-allowed}
textarea{width:100%;min-height:120px;background:#111;border:1px solid var(--border);border-radius:8px;padding:.75rem;color:var(--text);font-family:ui-monospace,monospace;font-size:.85rem;resize:vertical}
.label{display:block;margin-bottom:.35rem;font-size:.85rem;color:var(--muted)}
.row{display:flex;gap:.75rem;flex-wrap:wrap;align-items:center}
.footer{text-align:center;color:var(--muted);font-size:.8rem;margin-top:1rem}
a{color:var(--accent)}
hr{border:0;border-top:1px solid var(--border);margin:1rem 0}
</style>
</head>
<body>
<div class="container">
  <h1>🎵 gozik YouTube Music</h1>
  <p class="sub">Standalone plugin web console</p>
  {{content}}
  <p class="footer">gozik-yt-music gRPC plugin &middot; <a href="/api/status">Status JSON</a></p>
</div>
<script>
async function post(url,body){const r=await fetch(url,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});return r.json();}
</script>
</body>
</html>
"""


def _render(content: str) -> bytes:
    return HTML_PAGE.replace("{{content}}", content).encode("utf-8")


def _cap_name(cap: int) -> str:
    names = {
        pb.PROVIDER_CAPABILITY_SEARCH: "Search",
        pb.PROVIDER_CAPABILITY_STREAM_TRACK: "Stream Track",
        pb.PROVIDER_CAPABILITY_LIBRARY_MANAGEMENT: "Library Management",
    }
    return names.get(cap, f"Capability({cap})")


# ---------------------------------------------------------------------------
# Fake gRPC context for calling servicer methods directly
# ---------------------------------------------------------------------------

class _FakeContext:
    """Minimal stand-in for grpc.ServicerContext used by the web UI layer."""

    def abort(self, code: grpc.StatusCode, details: str) -> None:
        raise RuntimeError(f"[{code.name}] {details}")

    def is_active(self) -> bool:
        return True


# ---------------------------------------------------------------------------
# HTTP handler factory
# ---------------------------------------------------------------------------

def make_handler(servicer: Any):
    class WebUIHandler(BaseHTTPRequestHandler):
        def log_message(self, fmt: str, *args: Any) -> None:
            logger.debug(fmt, *args)

        def _send(self, code: int, body: bytes, ctype: str = "text/html") -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _json(self, data: Any, code: int = 200) -> None:
            self._send(code, json.dumps(data, indent=2).encode("utf-8"), "application/json")

        def _redirect(self, path: str) -> None:
            self.send_response(302)
            self.send_header("Location", path)
            self.end_headers()

        # --------------------------------------------------------------
        # GET
        # --------------------------------------------------------------

        def do_GET(self) -> None:
            parsed = urllib.parse.urlparse(self.path)
            path = parsed.path
            if path == "/":
                self._index()
            elif path == "/login":
                self._login_page()
            elif path == "/api/status":
                self._api_status()
            else:
                self._send(404, _render('<div class="card"><h2>404</h2><p>Page not found.</p><a href="/">← Back to dashboard</a></div>'))

        # --------------------------------------------------------------
        # POST
        # --------------------------------------------------------------

        def do_POST(self) -> None:
            parsed = urllib.parse.urlparse(self.path)
            path = parsed.path
            if path == "/login/initiate":
                self._login_initiate()
            elif path == "/login/complete":
                self._login_complete()
            elif path == "/login/cookie":
                self._login_cookie()
            elif path == "/login/extract":
                self._login_extract()
            elif path == "/logout":
                self._logout()
            else:
                self._send(404, b"Not found")

        # --------------------------------------------------------------
        # Pages
        # --------------------------------------------------------------

        def _index(self) -> None:
            meta = servicer.GetProviderMetadata(pb.GetProviderMetadataRequest(), _FakeContext())
            auth = meta.auth_status == pb.AUTH_STATUS_AUTHENTICATED
            # If the user is not authenticated, redirect straight to the login
            # page so that clicking the app menu entry immediately shows the
            # auth flow (Selenium browser popup).
            if not auth:
                self._redirect("/login")
                return
            status_html = (
                f'<span class="status {"ok" if auth else "no"}">'
                f'{"Authenticated" if auth else "Unauthenticated"}</span>'
            )
            btns = (
                '<div class="row">'
                '<a href="/login"><button>Authenticate</button></a>'
                '<form method="post" action="/logout" style="display:inline" '
                'onsubmit="return confirm(\'Remove stored credentials?\')">'
                '<button type="submit" style="background:#333">Logout</button></form></div>'
            )
            cap_list = "<br>".join(f"&middot; {_cap_name(c)}" for c in meta.capabilities)
            html = f'''
<div class="card">
  <h2>Status</h2>
  <p>{status_html}</p>
  <p style="color:var(--muted);font-size:.85rem">
    Provider: <strong>{meta.display_name}</strong> ({meta.provider_id})
  </p>
  {"<p>Credentials are stored.</p>" if auth else ""}
  {btns}
</div>
<div class="card">
  <h2>Capabilities</h2>
  <p style="font-size:.9rem">{cap_list}</p>
</div>
'''
            self._send(200, _render(html))

        def _login_page(self) -> None:
            html = '''
<div class="card">
  <h2>Auto Import from Browser</h2>
  <p style="font-size:.9rem;color:var(--muted)">
    Extract session cookies directly from your local browser's database.
    <strong>Please close the selected browser first</strong> to prevent database lock errors.
  </p>
  <label class="label">Select Browser</label>
  <div class="row">
    <select id="browserSelect" style="background:#111;border:1px solid var(--border);color:var(--text);padding:.5rem;border-radius:8px;outline:none;">
      <option value="chrome">Chrome</option>
      <option value="firefox">Firefox</option>
      <option value="edge">Edge</option>
      <option value="brave">Brave</option>
      <option value="opera">Opera</option>
      <option value="vivaldi">Vivaldi</option>
      <option value="safari">Safari (macOS)</option>
      <option value="whale">Whale</option>
    </select>
    <button onclick="importFromBrowser()">Import & Authenticate</button>
  </div>
  <div id="importResult" style="margin-top:.75rem"></div>
</div>
<div class="card">
  <h2>YouTube Music Login (Selenium)</h2>
  <p style="font-size:.9rem;color:var(--muted)">
    Click <strong>Start</strong> to open a real browser window.
    Log in to your Google account on YouTube Music — the server will
    automatically harvest the session cookies and complete authentication.
    No manual copy-paste is required.
  </p>
  <div class="row">
    <button onclick="startOAuth()">Start</button>
    <button id="completeBtn" onclick="completeOAuth()" disabled>Complete</button>
  </div>
  <div id="oauthResult" style="margin-top:1rem"></div>
</div>
<div class="card">
  <h2>Manual Cookie Login</h2>
  <p style="font-size:.9rem;color:var(--muted)">
    Advanced: paste a raw <code>headers_auth.json</code> object below.
  </p>
  <label class="label">Cookie JSON</label>
  <textarea id="cookieJson" placeholder='{"Authorization": "...", "Cookie": "..."}'></textarea>
  <div style="margin-top:.75rem">
    <button onclick="saveCookie()">Save Cookie</button>
  </div>
  <div id="cookieResult" style="margin-top:.75rem"></div>
</div>
<script>
let deviceCode = "";
async function importFromBrowser(){
  const browser = document.getElementById("browserSelect").value;
  const box = document.getElementById("importResult");
  box.innerHTML = 'Extracting and authenticating...';
  const res = await post("/login/extract",{browser:browser});
  if(res.error){ box.innerHTML = '<span style="color:var(--accent)">Error: '+res.error+'</span>'; return; }
  box.innerHTML = '<span style="color:var(--accent2)">Authenticated successfully!</span>';
  setTimeout(()=>location.href="/",1200);
}
async function startOAuth(){
  const res = await post("/login/initiate",{});
  const box = document.getElementById("oauthResult");
  const btn = document.getElementById("completeBtn");
  if(res.error){ box.innerHTML = '<span style="color:var(--accent)">Error: '+res.error+'</span>'; return; }
  if(res.already_authenticated){
    box.innerHTML = '<p><strong style="color:var(--accent2)">Auth Complete!</strong></p>';
    btn.disabled = false;
    btn.textContent = "Close";
    btn.onclick = () => location.href="/";
    return;
  }
  box.innerHTML = '<p><strong style="color:var(--accent2)">Browser window opened.</strong></p>'+
    '<p>A Chromium window was launched for YouTube Music.</p>'+
    '<p>Please log in inside <strong>that</strong> window. The server will detect it automatically.</p>';
  btn.disabled = false;
  pollStatus();
}
async function pollStatus(){
  const box = document.getElementById("oauthResult");
  const check = async () => {
    try{
      const res = await fetch("/api/status");
      const data = await res.json();
      if(data.auth_status === 1){
        box.innerHTML = '<span style="color:var(--accent2)">Authenticated successfully!</span>';
        setTimeout(()=>location.href="/", 1200);
        return;
      }
    }catch(e){}
    setTimeout(check, 3000);
  };
  check();
}
async function completeOAuth(){
  const btn = document.getElementById("completeBtn");
  btn.disabled = true; btn.textContent = "Completing…";
  const res = await post("/login/complete",{});
  const box = document.getElementById("oauthResult");
  if(res.error){ box.innerHTML = '<span style="color:var(--accent)">Error: '+res.error+'</span>'; btn.disabled=false; btn.textContent="Complete"; return; }
  box.innerHTML = '<span style="color:var(--accent2)">Authenticated successfully!</span>';
  setTimeout(()=>location.href="/",1200);
}
async function saveCookie(){
  const val = document.getElementById("cookieJson").value.trim();
  const box = document.getElementById("cookieResult");
  if(!val){ box.innerHTML = '<span style="color:var(--accent)">Paste JSON first.</span>'; return; }
  const res = await post("/login/cookie",{cookie_json:val});
  if(res.error){ box.innerHTML = '<span style="color:var(--accent)">Error: '+res.error+'</span>'; return; }
  box.innerHTML = '<span style="color:var(--accent2)">Cookie saved!</span>';
  setTimeout(()=>location.href="/",1200);
}
</script>
'''
            self._send(200, _render(html))

        def _api_status(self) -> None:
            meta = servicer.GetProviderMetadata(pb.GetProviderMetadataRequest(), _FakeContext())
            self._json({
                "provider_id": meta.provider_id,
                "display_name": meta.display_name,
                "auth_status": meta.auth_status,
                "capabilities": list(meta.capabilities),
            })

        # --------------------------------------------------------------
        # API endpoints
        # --------------------------------------------------------------

        def _read_json(self) -> Dict[str, Any]:
            length = int(self.headers.get("Content-Length", 0))
            if length == 0:
                return {}
            body = self.rfile.read(length)
            try:
                return json.loads(body.decode("utf-8"))
            except json.JSONDecodeError:
                return {}

        def _login_initiate(self) -> None:
            try:
                resp = servicer.InitiateAuth(pb.InitiateAuthRequest(), _FakeContext())
                already_done = False
                try:
                    with servicer._lock:
                        already_done = bool(servicer._selenium_result.get("success"))
                except Exception:
                    pass
                self._json({
                    "auth_url": resp.auth_url,
                    "device_code": resp.device_code,
                    "opened_browser": not already_done,
                    "already_authenticated": already_done,
                    "message": (
                        "A browser window has been opened. Please log in to YouTube Music in that window."
                        if not already_done
                        else "Authenticated using existing browser session."
                    ),
                })
            except Exception as exc:
                logger.error("InitiateAuth via web UI failed: %s", exc)
                self._json({"error": str(exc)}, 500)

        def _login_complete(self) -> None:
            try:
                req = pb.CompleteAuthRequest(params={})
                resp = servicer.CompleteAuth(req, _FakeContext())
                try:
                    from handlers.desktop_entry import register
                    register(webui_port=server.server_address[1], force=False)
                except Exception:
                    pass
                self._json({
                    "access_token": resp.access_token,
                    "expires_at": resp.expires_at,
                })
            except Exception as exc:
                logger.error("CompleteAuth via web UI failed: %s", exc)
                self._json({"error": str(exc)}, 500)

        def _login_cookie(self) -> None:
            data = self._read_json()
            cookie_json = data.get("cookie_json", "")
            try:
                req = pb.CompleteAuthRequest(params={"cookie_json": cookie_json})
                resp = servicer.CompleteAuth(req, _FakeContext())
                try:
                    from handlers.desktop_entry import register
                    register(webui_port=server.server_address[1], force=False)
                except Exception:
                    pass
                self._json({
                    "access_token": resp.access_token,
                    "expires_at": resp.expires_at,
                })
            except Exception as exc:
                logger.error("CompleteAuth(cookie) via web UI failed: %s", exc)
                self._json({"error": str(exc)}, 500)

        def _login_extract(self) -> None:
            data = self._read_json()
            browser = data.get("browser", "")
            user_agent = self.headers.get("User-Agent", "")
            try:
                req = pb.CompleteAuthRequest(params={"browser": browser, "user_agent": user_agent})
                resp = servicer.CompleteAuth(req, _FakeContext())
                try:
                    from handlers.desktop_entry import register
                    register(webui_port=server.server_address[1], force=False)
                except Exception:
                    pass
                self._json({
                    "access_token": resp.access_token,
                    "expires_at": resp.expires_at,
                })
            except Exception as exc:
                logger.error("CompleteAuth(extract) via web UI failed: %s", exc)
                self._json({"error": str(exc)}, 500)

        def _logout(self) -> None:
            try:
                auth_file = (
                    Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
                    / "gozik"
                    / "ytmusic_auth.json"
                )
                oauth_file = (
                    Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
                    / "gozik"
                    / "ytmusic_oauth.json"
                )
                removed = False
                for f in (auth_file, oauth_file):
                    if f.exists():
                        f.unlink()
                        removed = True
                        logger.info("Removed credential file: %s", f)
                if removed:
                    servicer._invalidate_client()
                self._redirect("/")
            except Exception as exc:
                logger.error("Logout failed: %s", exc)
                self._json({"error": str(exc)}, 500)

    return WebUIHandler


# ---------------------------------------------------------------------------
# Server runner
# ---------------------------------------------------------------------------

def start_webui(servicer: Any, port: int) -> HTTPServer:
    """Create and start the HTTP server in a background thread."""
    handler = make_handler(servicer)
    server = HTTPServer(("127.0.0.1", port), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    logger.info("Web UI listening on http://127.0.0.1:%d", port)
    return server
