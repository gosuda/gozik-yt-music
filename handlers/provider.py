"""
handlers/provider.py — MusicProviderService gRPC servicer implementation.

Maps every RPC defined in music_provider.proto to concrete ytmusicapi calls.
Authentication credentials are stored in the host OS standard configuration
directory: ~/.config/gozik/ytmusic_auth.json

Authentication Architecture (three-tier fallback):
    1. OAuth 2.0 (primary)
       - Uses ytmusicapi's built-in OAuthCredentials / device-code flow.
       - Tokens persisted to _OAUTH_FILE as JSON (includes client_id/client_secret).
       - Client credentials are read from environment variables
         GOZIK_YTM_OAUTH_CLIENT_ID and GOZIK_YTM_OAUTH_CLIENT_SECRET.
    2. Browser cookie fallback (secondary)
       - Interactive Selenium login when OAuth is unavailable.
       - Cookies and User-Agent are persisted to _AUTH_FILE as JSON headers.
       - Streaming uses a temporary Netscape-format cookiefile; never
         cookiesfrombrowser.
    3. Unauthenticated public mode (last resort)
       - Search, GetTrackDetails, and ResolveStream work without login.
       - Personal-data RPCs (GetUserLibrary, GetUserPlaylists) return
         UNAUTHENTICATED with an actionable remediation message.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import platform
import shutil
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

import grpc
import ytmusicapi
from selenium import webdriver
from selenium.webdriver.chrome.service import Service as ChromeService
from selenium.webdriver.support.ui import WebDriverWait
from webdriver_manager.chrome import ChromeDriverManager
from webdriver_manager.core.os_manager import ChromeType

from generated import music_provider_pb2 as pb
from generated import music_provider_pb2_grpc as pb_grpc

logger = logging.getLogger("gozik.ytmusic.provider")

PROVIDER_ID = "ytmusic"
DISPLAY_NAME = "YouTube Music"

_AUTH_FILE: Path = (
    Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    / "gozik"
    / "ytmusic_auth.json"
)

_OAUTH_FILE: Path = (
    Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    / "gozik"
    / "ytmusic_oauth.json"
)

_FALLBACK_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

_SUPPORTED_YTDLP_BROWSERS = {
    "brave",
    "chrome",
    "chromium",
    "edge",
    "firefox",
    "opera",
    "safari",
    "vivaldi",
    "whale",
}

_QUALITY_FORMAT: Dict[int, str] = {
    pb.AUDIO_QUALITY_UNSPECIFIED: "bestaudio",
    pb.AUDIO_QUALITY_LOW: "worstaudio",
    pb.AUDIO_QUALITY_MEDIUM: "bestaudio[abr<=128]",
    pb.AUDIO_QUALITY_HIGH: "bestaudio",
}

_CAPABILITIES = [
    pb.PROVIDER_CAPABILITY_SEARCH,
    pb.PROVIDER_CAPABILITY_STREAM_TRACK,
    pb.PROVIDER_CAPABILITY_LIBRARY_MANAGEMENT,
]

# ---------------------------------------------------------------------------
# Environment variable names for OAuth client credentials
# ---------------------------------------------------------------------------
_ENV_OAUTH_CLIENT_ID = "GOZIK_YTM_OAUTH_CLIENT_ID"
_ENV_OAUTH_CLIENT_SECRET = "GOZIK_YTM_OAUTH_CLIENT_SECRET"


def _get_oauth_env_credentials() -> tuple[Optional[str], Optional[str]]:
    """Return OAuth client credentials from environment variables, if set."""
    client_id = os.environ.get(_ENV_OAUTH_CLIENT_ID, "").strip()
    client_secret = os.environ.get(_ENV_OAUTH_CLIENT_SECRET, "").strip()
    if client_id and client_secret:
        return client_id, client_secret
    return None, None


# ---------------------------------------------------------------------------
# Auth file helpers
# ---------------------------------------------------------------------------

def _load_json_file(path: Path) -> Optional[Dict[str, Any]]:
    """Load a JSON dict from *path* if it exists and is valid."""
    try:
        if not path.exists():
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or not data:
            return None
        return data
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("Failed to read JSON file %s: %s", path, exc)
        return None


def _save_json_file(path: Path, data: Dict[str, Any]) -> None:
    """Persist *data* as indented JSON to *path*, creating parents if needed."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    logger.info("Saved auth credentials to %s", path)


def _load_auth_file() -> Optional[Dict[str, Any]]:
    """Load the persisted browser-cookie auth JSON if it is structurally valid."""
    return _load_json_file(_AUTH_FILE)


def _save_auth_file(data: Dict[str, Any]) -> None:
    """Persist browser-cookie auth data to the config directory."""
    _save_json_file(_AUTH_FILE, data)


def _load_oauth_file() -> Optional[Dict[str, Any]]:
    """Load the persisted OAuth token JSON if it is structurally valid."""
    return _load_json_file(_OAUTH_FILE)


def _save_oauth_file(data: Dict[str, Any]) -> None:
    """Persist OAuth token data to the config directory."""
    _save_json_file(_OAUTH_FILE, data)


# ---------------------------------------------------------------------------
# Browser name normalisation (auth-setup only — never used in streaming)
# ---------------------------------------------------------------------------

def _normalise_browser_name(value: str) -> Optional[str]:
    """Return a yt-dlp browser name, or None when the value is unsupported."""
    browser = value.strip().lower()
    aliases = {
        "google": "chrome",
        "google-chrome": "chrome",
        "google_chrome": "chrome",
        "msedge": "edge",
        "microsoft-edge": "edge",
        "microsoft_edge": "edge",
        "chromium-browser": "chromium",
    }
    browser = aliases.get(browser, browser)
    if browser in _SUPPORTED_YTDLP_BROWSERS:
        return browser
    return None


def _browser_from_chrome_type(chrome_type: ChromeType) -> str:
    """Convert webdriver-manager ChromeType into a yt-dlp browser name."""
    if chrome_type == ChromeType.CHROMIUM:
        return "chromium"
    if chrome_type == ChromeType.MSEDGE:
        return "edge"
    return "chrome"


# ---------------------------------------------------------------------------
# Cookie / header builders
# ---------------------------------------------------------------------------

def _generate_sapisid_auth(cookie_str: str) -> tuple[str, str]:
    """Generate a SAPISIDHASH authorization header from browser cookies."""
    cookie_dict: Dict[str, str] = {}
    for part in cookie_str.split(";"):
        if "=" in part:
            key, value = part.strip().split("=", 1)
            cookie_dict[key] = value

    sapisid = cookie_dict.get("SAPISID") or cookie_dict.get("__Secure-3PAPISID")
    if not sapisid:
        raise RuntimeError(
            "SAPISID cookie not found — are you logged in? "
            "How to fix: complete the browser login flow and ensure you are signed in to YouTube Music. "
            "Fallback: public search is still available without login."
        )

    origin = "https://music.youtube.com"
    timestamp = int(time.time())
    message = f"{timestamp} {sapisid} {origin}"
    sha1 = hashlib.sha1(message.encode()).hexdigest()
    return f"SAPISIDHASH {timestamp}_{sha1}", "0"


def _build_cookie_auth_headers(
    cookie_str: str,
    user_agent: str,
    browser_name: Optional[str] = None,
    visitor_id: str = "",
) -> Dict[str, str]:
    """Build a ytmusicapi-compatible browser-cookie auth header object."""
    auth_header, auth_user = _generate_sapisid_auth(cookie_str)
    headers: Dict[str, str] = {
        "user-agent": user_agent,
        "accept": "*/*",
        "accept-encoding": "gzip, deflate",
        "content-type": "application/json",
        "content-encoding": "gzip",
        "origin": "https://music.youtube.com",
        "cookie": cookie_str,
        "x-goog-authuser": auth_user,
        "authorization": auth_header,
    }
    if visitor_id:
        headers["x-goog-visitor-id"] = visitor_id
    if browser_name:
        headers["browser"] = browser_name
    return headers


# ---------------------------------------------------------------------------
# YTMusic client builder (OAuth → browser cookie → unauthenticated)
# ---------------------------------------------------------------------------

def _build_ytmusic() -> ytmusicapi.YTMusic:
    """Construct a YTMusic instance from OAuth, browser-cookie auth, or public mode."""
    oauth_data = _load_oauth_file()
    if oauth_data:
        client_id = oauth_data.get("client_id")
        client_secret = oauth_data.get("client_secret")
        if client_id and client_secret:
            try:
                oauth_creds = ytmusicapi.OAuthCredentials(
                    client_id=client_id,
                    client_secret=client_secret,
                )
                logger.debug("Initializing YTMusic with OAuth credentials from %s", _OAUTH_FILE)
                return ytmusicapi.YTMusic(str(_OAUTH_FILE), oauth_credentials=oauth_creds)
            except Exception as exc:
                logger.warning(
                    "Failed to initialize YTMusic with OAuth (%s). "
                    "Falling back to browser cookie auth.",
                    exc,
                )
        else:
            logger.warning(
                "OAuth file %s is missing client_id/client_secret. "
                "Re-authenticate with OAuth or use browser cookie fallback.",
                _OAUTH_FILE,
            )

    auth_data = _load_auth_file()
    if auth_data:
        try:
            logger.debug("Initializing YTMusic with browser auth from %s", _AUTH_FILE)
            return ytmusicapi.YTMusic(str(_AUTH_FILE))
        except Exception as exc:
            logger.warning(
                "Failed to initialize YTMusic with browser auth (%s). "
                "Falling back to unauthenticated public mode.",
                exc,
            )

    logger.info("Initializing unauthenticated YTMusic client")
    return ytmusicapi.YTMusic()


# ---------------------------------------------------------------------------
# Chrome binary detection
# ---------------------------------------------------------------------------

def _find_chrome_binary() -> tuple[str, ChromeType]:
    """Locate a Chrome, Chromium, or Edge binary."""
    candidates: list[tuple[str, ChromeType]] = [
        ("chromium", ChromeType.CHROMIUM),
        ("chromium-browser", ChromeType.CHROMIUM),
        ("google-chrome-stable", ChromeType.GOOGLE),
        ("google-chrome", ChromeType.GOOGLE),
        ("chrome", ChromeType.GOOGLE),
        ("google-chrome-beta", ChromeType.GOOGLE),
        ("google-chrome-dev", ChromeType.GOOGLE),
        ("msedge", ChromeType.MSEDGE),
        ("microsoft-edge", ChromeType.MSEDGE),
    ]

    for name, chrome_type in candidates:
        path = shutil.which(name)
        if path:
            logger.debug("Found browser on PATH: %s (type=%s)", path, chrome_type)
            return path, chrome_type

    system = platform.system()
    fallback_paths: list[tuple[str, ChromeType]] = []

    if system == "Windows":
        program_files = os.environ.get("ProgramFiles", r"C:\Program Files")
        program_files_x86 = os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")
        local_appdata = os.environ.get("LocalAppData")
        if not local_appdata:
            local_appdata = os.path.join(str(Path.home()), "AppData", "Local")

        fallback_paths = [
            (os.path.join(program_files, "Google", "Chrome", "Application", "chrome.exe"), ChromeType.GOOGLE),
            (os.path.join(program_files_x86, "Google", "Chrome", "Application", "chrome.exe"), ChromeType.GOOGLE),
            (os.path.join(local_appdata, "Google", "Chrome", "Application", "chrome.exe"), ChromeType.GOOGLE),
            (os.path.join(program_files, "Microsoft", "Edge", "Application", "msedge.exe"), ChromeType.MSEDGE),
            (os.path.join(program_files_x86, "Microsoft", "Edge", "Application", "msedge.exe"), ChromeType.MSEDGE),
        ]
    elif system == "Darwin":
        home = str(Path.home())
        fallback_paths = [
            ("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome", ChromeType.GOOGLE),
            (os.path.join(home, "Applications", "Google Chrome.app", "Contents", "MacOS", "Google Chrome"), ChromeType.GOOGLE),
            ("/Applications/Chromium.app/Contents/MacOS/Chromium", ChromeType.CHROMIUM),
            (os.path.join(home, "Applications", "Chromium.app", "Contents", "MacOS", "Chromium"), ChromeType.CHROMIUM),
            ("/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge", ChromeType.MSEDGE),
        ]

    for path, chrome_type in fallback_paths:
        if os.path.isfile(path) and os.access(path, os.X_OK):
            logger.debug("Found browser at well-known path: %s (type=%s)", path, chrome_type)
            return path, chrome_type

    raise RuntimeError(
        "No Chrome, Chromium, or Edge browser found. "
        "Please install a supported browser and ensure it is in your PATH. "
        "Fallback: public search is still available without login."
    )


# ---------------------------------------------------------------------------
# Selenium interactive auth thread
# ---------------------------------------------------------------------------

def _selenium_auth_thread(result: Dict[str, Any]) -> None:
    """Open a browser, wait for login, and persist ytmusicapi auth headers."""
    options = webdriver.ChromeOptions()
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_experimental_option("excludeSwitches", ["enable-automation"])
    options.add_experimental_option("useAutomationExtension", False)
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")

    binary_path, chrome_type = _find_chrome_binary()
    browser_name = _browser_from_chrome_type(chrome_type)
    options.binary_location = binary_path

    driver: Optional[Any] = None
    try:
        if chrome_type == ChromeType.MSEDGE:
            from selenium.webdriver.edge.service import Service as EdgeService
            from webdriver_manager.microsoft import EdgeChromiumDriverManager

            service = EdgeService(EdgeChromiumDriverManager().install())
            driver = webdriver.Edge(service=service, options=options)
        else:
            service = ChromeService(ChromeDriverManager(chrome_type=chrome_type).install())
            driver = webdriver.Chrome(service=service, options=options)

        driver.get("https://music.youtube.com")

        def _has_sapisid(d: Any) -> bool:
            cookies = d.get_cookies() or []
            return any(c.get("name") in ("SAPISID", "__Secure-3PAPISID") for c in cookies)

        WebDriverWait(driver, 300).until(_has_sapisid)

        cookies_list = driver.get_cookies() or []
        cookie_dict = {c["name"]: c["value"] for c in cookies_list}
        cookie_str = "; ".join(f"{key}={value}" for key, value in cookie_dict.items())

        visitor_id = driver.execute_script(
            "try { return window.yt.config_.VISITOR_DATA; } catch(e) { return ''; }"
        )
        user_agent = driver.execute_script("return navigator.userAgent;")

        headers = _build_cookie_auth_headers(
            cookie_str=cookie_str,
            user_agent=user_agent or _FALLBACK_USER_AGENT,
            browser_name=browser_name,
            visitor_id=visitor_id or "",
        )

        _save_auth_file(headers)

        result["success"] = True
        logger.info("Selenium auth succeeded — credentials saved to %s", _AUTH_FILE)
    except Exception as exc:
        logger.error("Selenium auth failed: %s", exc, exc_info=True)
        result["error"] = str(exc)
        result.pop("pending", None)
    else:
        result.pop("pending", None)
    finally:
        if driver is not None:
            try:
                driver.quit()
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Auth status
# ---------------------------------------------------------------------------

def _auth_status() -> int:
    """Return the current provider authentication state."""
    if _OAUTH_FILE.exists() or _load_auth_file() is not None:
        return pb.AUTH_STATUS_AUTHENTICATED
    return pb.AUTH_STATUS_UNAUTHENTICATED


# ---------------------------------------------------------------------------
# JS runtime helper for yt-dlp
# ---------------------------------------------------------------------------

def _get_node_path() -> Optional[str]:
    """Return the path to a Node.js binary for yt-dlp JS challenges.

    Search order:
        1. Bundled PyInstaller onedir (same dir as the executable).
        2. Project-local .tools/bin/node.
        3. Any node in the system PATH.
    """
    # 1. PyInstaller bundle
    if getattr(sys, "frozen", False):
        bundle_dir = Path(sys.executable).parent
        bundled = bundle_dir / "node"
        if bundled.exists() and os.access(bundled, os.X_OK):
            return str(bundled)

    # 2. Local download (dev / build prep)
    local_node = Path(__file__).resolve().parent.parent / ".tools" / "bin" / "node"
    if local_node.exists() and os.access(local_node, os.X_OK):
        return str(local_node)

    # 3. System PATH
    system_node = shutil.which("node")
    if system_node:
        return system_node

    return None


# ---------------------------------------------------------------------------
# Stream resolution (NO cookiesfrombrowser — uses stored cookie string only)
# ---------------------------------------------------------------------------

def _write_netscape_cookie_file(cookie_str: str) -> str:
    """Write *cookie_str* to a temporary Netscape-format cookie file.

    Returns the path to the temporary file. The caller is responsible for
    deleting the file when done.
    """
    fd, path = tempfile.mkstemp(suffix=".txt")
    with os.fdopen(fd, "w") as f:
        f.write("# Netscape HTTP Cookie File\n")
        f.write("# This file was generated by gozik-yt-music\n")
        for part in cookie_str.split(";"):
            part = part.strip()
            if "=" not in part:
                continue
            key, value = part.split("=", 1)
            # Netscape format: domain  flag  path  secure  expiry  name  value
            f.write(
                f".youtube.com\tTRUE\t/\tTRUE\t0\t{key.strip()}\t{value.strip()}\n"
            )
    return path


def _resolve_stream_url(video_id: str, quality: int) -> tuple[str, dict[str, str], int]:
    """Extract a direct audio stream URL for the given YouTube Music video ID.

    Authentication fallback for yt-dlp:
        1. Browser cookie string from _AUTH_FILE (written to a temporary
           Netscape cookiefile and passed via yt-dlp's cookiefile option).
        2. Stored User-Agent from _AUTH_FILE (passed via http_headers).
        3. Unauthenticated if no cookies are stored.

    Raises:
        RuntimeError: with actionable remediation advice on every failure path.
    """
    try:
        import yt_dlp
    except ImportError as exc:
        raise RuntimeError(
            "What failed: yt-dlp is not installed. "
            "How to fix: install dependencies via requirements.txt. "
            "Fallback: public search is still available without streaming."
        ) from exc

    format_selector = _QUALITY_FORMAT.get(quality, "bestaudio")
    ydl_opts: Dict[str, Any] = {
        "format": format_selector,
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "skip_download": True,
        "extract_flat": False,
    }

    node_path = _get_node_path()
    if node_path:
        ydl_opts["js_runtimes"] = {"node": {"path": node_path}}
        ydl_opts["remote_components"] = {"ejs:github"}

    cookie_path: Optional[str] = None
    auth_data = _load_auth_file()

    try:
        if auth_data:
            cookie_str = auth_data.get("cookie", "")
            if cookie_str:
                try:
                    cookie_path = _write_netscape_cookie_file(cookie_str)
                    ydl_opts["cookiefile"] = cookie_path
                    logger.debug("Using stored browser cookie file for streaming")
                except OSError as exc:
                    logger.warning("Failed to write temporary cookie file: %s", exc)
            else:
                logger.debug("No cookie string found in auth file")

            user_agent = auth_data.get("user-agent", "")
            if user_agent:
                ydl_opts["http_headers"] = {"User-Agent": user_agent}
                logger.debug("Using stored User-Agent for streaming")

        url = f"https://music.youtube.com/watch?v={video_id}"
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=False)
        except yt_dlp.utils.DownloadError as exc:
            error_msg = str(exc).lower()
            if "sign in to confirm" in error_msg or "bot" in error_msg:
                raise RuntimeError(
                    "What failed: YouTube bot detection triggered. "
                    "How to fix: re-authenticate in Settings using browser cookie "
                    "or OAuth authentication. "
                    "Fallback: public search is still available without login."
                ) from exc
            elif "unavailable" in error_msg or "private" in error_msg:
                raise RuntimeError(
                    f"What failed: this track is unavailable or private ({video_id}). "
                    "How to fix: try a different track. "
                    "Fallback: public search is still available."
                ) from exc
            else:
                raise RuntimeError(
                    "What failed: stream extraction failed. "
                    "How to fix: update yt-dlp: `pip install -U yt-dlp` or "
                    "`yt-dlp -U --update-to nightly`. "
                    "Fallback: public search is still available without streaming."
                ) from exc

        if info is None:
            raise RuntimeError(
                "What failed: yt-dlp returned no stream info. "
                "How to fix: update yt-dlp: `pip install -U yt-dlp` or "
                "`yt-dlp -U --update-to nightly`. "
                "Fallback: public search is still available without streaming."
            )

        stream_url: str = info.get("url", "")
        if not stream_url:
            formats = info.get("formats", [])
            if formats:
                stream_url = formats[-1].get("url", "")

        if not stream_url:
            raise RuntimeError(
                "What failed: no stream URL found. "
                "How to fix: this track may be region-restricted or require authentication. "
                "Try re-authenticating in Settings or using a different track. "
                "Fallback: public search is still available without streaming."
            )

        http_headers: dict[str, str] = info.get("http_headers", {})
        headers = {key: str(value) for key, value in http_headers.items()}
        expiry_ms = int((time.time() + 6 * 3600) * 1000)

        return stream_url, headers, expiry_ms
    finally:
        if cookie_path and os.path.exists(cookie_path):
            try:
                os.unlink(cookie_path)
            except OSError:
                pass


# ---------------------------------------------------------------------------
# Protobuf helpers
# ---------------------------------------------------------------------------

def _to_pb_image(thumb: Dict[str, Any]) -> pb.Image:
    """Convert a ytmusicapi thumbnail dict to a protobuf Image message."""
    return pb.Image(
        url=thumb.get("url", ""),
        width=thumb.get("width", 0),
        height=thumb.get("height", 0),
    )


def _to_pb_artist(artist: Dict[str, Any]) -> pb.Artist:
    """Convert a ytmusicapi artist dict to a protobuf Artist message."""
    return pb.Artist(
        id=artist.get("id") or "",
        name=artist.get("name") or "",
    )


def _to_pb_album(album: Optional[Dict[str, Any]]) -> pb.Album:
    """Convert a ytmusicapi album dict to a protobuf Album message."""
    if album is None:
        return pb.Album()
    return pb.Album(
        id=album.get("id") or "",
        title=album.get("name") or album.get("title") or "",
        release_year=_safe_int(album.get("year")),
    )


def _thumbnails_to_images(thumbnails: Optional[List[Dict[str, Any]]]) -> List[pb.Image]:
    """Convert thumbnail dictionaries to protobuf Image messages."""
    if not thumbnails:
        return []
    return [_to_pb_image(t) for t in thumbnails if isinstance(t, dict)]


def _safe_int(value: Any, default: int = 0) -> int:
    """Cast a value to int, returning default on failure."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _duration_to_ms(duration_str: Optional[str]) -> int:
    """Convert a YouTube Music duration string to milliseconds."""
    if not duration_str:
        return 0

    parts = duration_str.strip().split(":")
    try:
        if len(parts) == 2:
            total_s = int(parts[0]) * 60 + int(parts[1])
        elif len(parts) == 3:
            total_s = int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])
        else:
            return 0
        return total_s * 1000
    except (ValueError, IndexError):
        return 0


def _ytm_track_to_pb(result: Dict[str, Any]) -> pb.Track:
    """Convert a ytmusicapi song, video, or episode result to a protobuf Track."""
    video_id = result.get("videoId") or ""
    title = result.get("title") or ""
    artists = [_to_pb_artist(a) for a in (result.get("artists") or [])]
    # Episode results use "podcast" instead of "artists"
    if not artists and isinstance(result.get("podcast"), dict):
        artists = [_to_pb_artist(result["podcast"])]
    album_raw = result.get("album")
    album = _to_pb_album(album_raw) if isinstance(album_raw, dict) else pb.Album()
    duration_ms = _duration_to_ms(result.get("duration"))
    thumbnails = _thumbnails_to_images(result.get("thumbnails"))
    explicit = bool(result.get("isExplicit"))

    return pb.Track(
        id=video_id,
        title=title,
        artists=artists,
        album=album,
        duration_ms=duration_ms,
        images=thumbnails,
        explicit=explicit,
    )


def _ytm_album_to_pb(result: Dict[str, Any]) -> pb.Album:
    """Convert a ytmusicapi album result to a protobuf Album."""
    browse_id = result.get("browseId") or ""
    title = result.get("title") or ""
    artists = [_to_pb_artist(a) for a in (result.get("artists") or [])]
    thumbnails = _thumbnails_to_images(result.get("thumbnails"))
    return pb.Album(
        id=browse_id,
        title=title,
        artists=artists,
        images=thumbnails,
        release_year=_safe_int(result.get("year")),
        track_count=_safe_int(result.get("trackCount")),
    )


def _ytm_artist_to_pb(result: Dict[str, Any]) -> pb.Artist:
    """Convert a ytmusicapi artist result to a protobuf Artist message."""
    browse_id = result.get("browseId") or ""
    name = result.get("artist") or result.get("name") or ""
    return pb.Artist(id=browse_id, name=name)


def _ytm_playlist_to_pb(result: Dict[str, Any]) -> pb.Playlist:
    """Convert a ytmusicapi playlist result to a protobuf Playlist."""
    browse_id = result.get("browseId") or ""
    title = result.get("title") or ""
    thumbnails = _thumbnails_to_images(result.get("thumbnails"))
    author = result.get("author") or {}
    owner_name = author.get("name") or "" if isinstance(author, dict) else str(author)
    return pb.Playlist(
        id=browse_id,
        title=title,
        owner_name=owner_name,
        images=thumbnails,
        track_count=_safe_int(result.get("itemCount") or 0),
    )


# ---------------------------------------------------------------------------
# Servicer
# ---------------------------------------------------------------------------

class MusicProviderServicer(pb_grpc.MusicProviderServiceServicer):
    """Implementation of the MusicProviderService gRPC service."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._ytm: Optional[ytmusicapi.YTMusic] = None
        # Unauthenticated client used exclusively for search (tracks, albums,
        # artists, playlists). Playback and library calls use the authenticated
        # client built by _get_client().
        self._search_ytm = ytmusicapi.YTMusic()
        self._selenium_result: Dict[str, Any] = {}
        self._pending_oauth: Optional[Dict[str, Any]] = None
        logger.info("MusicProviderServicer initialised — auth file: %s", _AUTH_FILE)

    def _get_client(self) -> ytmusicapi.YTMusic:
        """Return the shared YTMusic client, creating it if needed."""
        with self._lock:
            if self._ytm is None:
                self._ytm = _build_ytmusic()
            return self._ytm

    def _invalidate_client(self) -> None:
        """Force the YTMusic client to be rebuilt on the next call."""
        with self._lock:
            self._ytm = None

    def _require_auth(self, context: grpc.ServicerContext) -> ytmusicapi.YTMusic:
        """Return a YTMusic client only when the user is authenticated.

        If no credentials are present, aborts with UNAUTHENTICATED and an
        actionable error message.
        """
        if _auth_status() != pb.AUTH_STATUS_AUTHENTICATED:
            context.abort(
                grpc.StatusCode.UNAUTHENTICATED,
                "What failed: no valid credentials found. "
                "How to fix: log in via Settings using OAuth or browser cookie authentication. "
                "Fallback: public search and track details are still available without login.",
            )
        return self._get_client()

    # -----------------------------------------------------------------------
    # GetProviderMetadata
    # -----------------------------------------------------------------------

    def GetProviderMetadata(
        self,
        request: pb.GetProviderMetadataRequest,
        context: grpc.ServicerContext,
    ) -> pb.GetProviderMetadataResponse:
        """Return static provider metadata and current auth status."""
        status = _auth_status()
        logger.debug("GetProviderMetadata — auth_status=%d", status)

        auth_url = ""
        if self._pending_oauth:
            auth_url = self._pending_oauth.get("verification_url", "")
        elif status == pb.AUTH_STATUS_UNAUTHENTICATED:
            auth_url = "https://music.youtube.com"

        return pb.GetProviderMetadataResponse(
            provider_id=PROVIDER_ID,
            display_name=DISPLAY_NAME,
            capabilities=_CAPABILITIES,
            auth_status=status,
            auth_url=auth_url,
        )

    # -----------------------------------------------------------------------
    # InitiateAuth
    # -----------------------------------------------------------------------

    def InitiateAuth(
        self,
        request: pb.InitiateAuthRequest,
        context: grpc.ServicerContext,
    ) -> pb.InitiateAuthResponse:
        """Start authentication.

        Priority:
            1. OAuth 2.0 device-code flow (if GOZIK_YTM_OAUTH_CLIENT_ID/SECRET are set).
            2. Fast-path browser cookie extraction via yt_dlp.
            3. Interactive Selenium browser login.
        """
        logger.info("InitiateAuth: starting authentication flow")

        # Reset any stale state.
        with self._lock:
            self._pending_oauth = None
            self._selenium_result = {}

        # -------------------------------------------------------------------
        # Tier 1 — OAuth 2.0
        # -------------------------------------------------------------------
        client_id, client_secret = _get_oauth_env_credentials()
        if client_id and client_secret:
            try:
                oauth_creds = ytmusicapi.OAuthCredentials(
                    client_id=client_id,
                    client_secret=client_secret,
                )
                code_info = oauth_creds.get_code()
                device_code = code_info["device_code"]
                verification_url = code_info["verification_url"]
                user_code = code_info["user_code"]

                with self._lock:
                    self._pending_oauth = {
                        "device_code": device_code,
                        "client_id": client_id,
                        "client_secret": client_secret,
                        "verification_url": verification_url,
                        "user_code": user_code,
                    }

                auth_url = f"{verification_url}?user_code={user_code}"
                logger.info(
                    "InitiateAuth: OAuth device-code issued — user_code=%s", user_code
                )
                return pb.InitiateAuthResponse(
                    auth_url=auth_url,
                    device_code=device_code,
                )
            except Exception as exc:
                logger.error(
                    "InitiateAuth: OAuth initiation failed (%s). "
                    "Falling back to browser cookie auth.",
                    exc,
                )
                # Fall through to browser cookie fallback.

        # -------------------------------------------------------------------
        # Tier 2 — Fast-path browser cookie extraction
        # -------------------------------------------------------------------
        browser_priority = [
            "chrome",
            "chromium",
            "edge",
            "brave",
            "opera",
            "vivaldi",
            "firefox",
            "safari",
            "whale",
        ]

        for browser_name in browser_priority:
            try:
                import yt_dlp.cookies

                cookie_jar = yt_dlp.cookies.extract_cookies_from_browser(browser_name)
                cookie_dict: Dict[str, str] = {}

                for cookie in cookie_jar:
                    if "youtube.com" in cookie.domain or "google.com" in cookie.domain:
                        cookie_dict[cookie.name] = cookie.value

                sapisid = cookie_dict.get("SAPISID") or cookie_dict.get("__Secure-3PAPISID")
                if not sapisid:
                    continue

                cookie_str = "; ".join(f"{key}={value}" for key, value in cookie_dict.items())
                headers = _build_cookie_auth_headers(
                    cookie_str=cookie_str,
                    user_agent=_FALLBACK_USER_AGENT,
                    browser_name=browser_name,
                )

                _save_auth_file(headers)
                self._invalidate_client()

                with self._lock:
                    self._selenium_result = {"success": True}

                logger.info(
                    "InitiateAuth: fast-path succeeded using existing '%s' session",
                    browser_name,
                )
                return pb.InitiateAuthResponse(
                    auth_url="https://music.youtube.com",
                    device_code="",
                )
            except Exception as exc:
                logger.debug(
                    "InitiateAuth: fast-path probe '%s' failed (%s)",
                    browser_name,
                    exc,
                )

        # -------------------------------------------------------------------
        # Tier 3 — Interactive Selenium login
        # -------------------------------------------------------------------
        with self._lock:
            result: Dict[str, Any] = {"pending": True}
            self._selenium_result = result

        thread = threading.Thread(
            target=_selenium_auth_thread,
            args=(result,),
            daemon=True,
        )
        thread.start()

        logger.info("InitiateAuth: launched interactive Selenium login")
        return pb.InitiateAuthResponse(
            auth_url="https://music.youtube.com",
            device_code="",
        )

    # -----------------------------------------------------------------------
    # CompleteAuth
    # -----------------------------------------------------------------------

    def CompleteAuth(
        self,
        request: pb.CompleteAuthRequest,
        context: grpc.ServicerContext,
    ) -> pb.CompleteAuthResponse:
        """Complete OAuth, browser-cookie, or direct-cookie authentication."""
        params: Dict[str, str] = dict(request.params)
        logger.info("CompleteAuth invoked with param keys: %s", list(params.keys()))

        # -------------------------------------------------------------------
        # Tier 1 — Complete pending OAuth flow
        # -------------------------------------------------------------------
        with self._lock:
            pending_oauth = self._pending_oauth

        if pending_oauth is not None:
            try:
                oauth_creds = ytmusicapi.OAuthCredentials(
                    client_id=pending_oauth["client_id"],
                    client_secret=pending_oauth["client_secret"],
                )
                token_response = oauth_creds.token_from_code(
                    pending_oauth["device_code"]
                )

                # Preserve client credentials inside the token file so
                # RefreshAuth and _build_ytmusic can reuse them later.
                token_response["client_id"] = pending_oauth["client_id"]
                token_response["client_secret"] = pending_oauth["client_secret"]

                _save_oauth_file(token_response)

                with self._lock:
                    self._pending_oauth = None

                self._invalidate_client()

                access_token = token_response.get("access_token", "")
                refresh_token = token_response.get("refresh_token", "")
                expires_in = int(token_response.get("expires_in", 3600))
                expires_at = int(time.time() * 1000) + expires_in * 1000

                logger.info("CompleteAuth: OAuth token acquired, expires_at=%d", expires_at)
                return pb.CompleteAuthResponse(
                    access_token=access_token,
                    refresh_token=refresh_token,
                    expires_at=expires_at,
                )
            except Exception as exc:
                logger.error("CompleteAuth OAuth poll failed: %s", exc, exc_info=True)
                context.abort(
                    grpc.StatusCode.UNAUTHENTICATED,
                    "What failed: OAuth token exchange failed. "
                    "How to fix: ensure you completed the Google consent screen, "
                    "then retry. If OAuth is not configured, set "
                    f"{_ENV_OAUTH_CLIENT_ID} and {_ENV_OAUTH_CLIENT_SECRET} "
                    "environment variables and restart the server. "
                    "Fallback: try browser cookie authentication instead.",
                )
                return pb.CompleteAuthResponse()

        # -------------------------------------------------------------------
        # Check Selenium result
        # -------------------------------------------------------------------
        with self._lock:
            result = self._selenium_result

        if result.get("pending"):
            context.abort(
                grpc.StatusCode.UNAUTHENTICATED,
                "What failed: browser authentication is still in progress. "
                "How to fix: finish logging in inside the opened browser window and try again. "
                "Fallback: public search is still available without login.",
            )
            return pb.CompleteAuthResponse()

        if result.get("success"):
            self._invalidate_client()
            logger.info("CompleteAuth: browser auth succeeded")
            return pb.CompleteAuthResponse(
                access_token="browser_cookie",
                refresh_token="",
                expires_at=0,
            )

        if result.get("error"):
            context.abort(
                grpc.StatusCode.UNAUTHENTICATED,
                f"What failed: browser auth failed — {result['error']}. "
                "How to fix: ensure a supported browser is installed, "
                "or try logging in with a different browser. "
                "Fallback: public search is still available without login.",
            )
            return pb.CompleteAuthResponse()

        # -------------------------------------------------------------------
        # Tier 2 — Auto-extract cookies from a named browser
        # -------------------------------------------------------------------
        browser = _normalise_browser_name(params.get("browser", ""))
        if browser:
            try:
                import yt_dlp.cookies

                logger.info("Attempting to auto-extract cookies from browser: %s", browser)
                cookie_jar = yt_dlp.cookies.extract_cookies_from_browser(browser)

                cookie_dict = {
                    cookie.name: cookie.value
                    for cookie in cookie_jar
                    if "youtube.com" in cookie.domain or "google.com" in cookie.domain
                }

                if not cookie_dict:
                    raise ValueError(
                        f"No cookies found for YouTube/Google in '{browser}'. "
                        "Please verify you are logged in to YouTube Music on this browser."
                    )

                sapisid = cookie_dict.get("SAPISID") or cookie_dict.get("__Secure-3PAPISID")
                if not sapisid:
                    raise ValueError(
                        f"SAPISID cookie not found in '{browser}' session. "
                        "Are you logged in to YouTube/YouTube Music?"
                    )

                cookie_str = "; ".join(f"{key}={value}" for key, value in cookie_dict.items())
                user_agent = params.get("user_agent", _FALLBACK_USER_AGENT)
                headers = _build_cookie_auth_headers(
                    cookie_str=cookie_str,
                    user_agent=user_agent,
                    browser_name=browser,
                )

                _save_auth_file(headers)
                self._invalidate_client()

                logger.info("CompleteAuth: auto-extracted cookies saved successfully")
                return pb.CompleteAuthResponse(
                    access_token="browser_cookie",
                    refresh_token="",
                    expires_at=0,
                )
            except Exception as exc:
                logger.error(
                    "Auto-extraction from browser %s failed: %s",
                    browser,
                    exc,
                    exc_info=True,
                )
                context.abort(
                    grpc.StatusCode.INVALID_ARGUMENT,
                    f"What failed: failed to extract cookies from browser '{browser}': {exc}. "
                    "How to fix: ensure you are logged in to YouTube Music in that browser, "
                    "or try a different browser. "
                    "Fallback: public search is still available without login.",
                )
                return pb.CompleteAuthResponse()

        # -------------------------------------------------------------------
        # Manual cookie_json payload
        # -------------------------------------------------------------------
        cookie_json = params.get("cookie_json", "")
        if cookie_json:
            try:
                cookie_data = json.loads(cookie_json)
                if not isinstance(cookie_data, dict):
                    raise ValueError("cookie_json must be a JSON object")

                if "browser" in params:
                    browser_from_params = _normalise_browser_name(params["browser"])
                    if browser_from_params:
                        cookie_data["browser"] = browser_from_params

                _save_auth_file(cookie_data)
                self._invalidate_client()

                logger.info("CompleteAuth: browser cookie credentials saved")
                return pb.CompleteAuthResponse(
                    access_token="browser_cookie",
                    refresh_token="",
                    expires_at=0,
                )
            except (json.JSONDecodeError, ValueError) as exc:
                context.abort(
                    grpc.StatusCode.INVALID_ARGUMENT,
                    f"What failed: invalid cookie_json payload: {exc}. "
                    "How to fix: provide a valid JSON object containing browser auth headers. "
                    "Fallback: public search is still available without login.",
                )
                return pb.CompleteAuthResponse()

        # -------------------------------------------------------------------
        # Nothing matched
        # -------------------------------------------------------------------
        context.abort(
            grpc.StatusCode.INVALID_ARGUMENT,
            "What failed: no valid auth payload provided in 'params'. "
            "How to fix: supply 'browser', 'cookie_json', or complete an OAuth consent screen. "
            "Fallback: public search is still available without login.",
        )
        return pb.CompleteAuthResponse()

    # -----------------------------------------------------------------------
    # RefreshAuth
    # -----------------------------------------------------------------------

    def RefreshAuth(
        self,
        request: pb.RefreshAuthRequest,
        context: grpc.ServicerContext,
    ) -> pb.RefreshAuthResponse:
        """Refresh OAuth tokens or report browser-cookie auth status."""
        logger.info("RefreshAuth requested")

        # -------------------------------------------------------------------
        # Tier 1 — Refresh OAuth access token
        # -------------------------------------------------------------------
        oauth_data = _load_oauth_file()
        if oauth_data and oauth_data.get("refresh_token"):
            client_id = oauth_data.get("client_id")
            client_secret = oauth_data.get("client_secret")
            if client_id and client_secret:
                try:
                    oauth_creds = ytmusicapi.OAuthCredentials(
                        client_id=client_id,
                        client_secret=client_secret,
                    )
                    refreshed = oauth_creds.refresh_token(oauth_data["refresh_token"])

                    # Preserve fields that the refresh response does not include.
                    refreshed["refresh_token"] = oauth_data["refresh_token"]
                    refreshed["client_id"] = client_id
                    refreshed["client_secret"] = client_secret
                    expires_in = int(refreshed.get("expires_in", 3600))
                    expires_at = int(time.time()) + expires_in
                    refreshed["expires_at"] = expires_at

                    _save_oauth_file(refreshed)
                    self._invalidate_client()

                    logger.info(
                        "RefreshAuth: OAuth token refreshed, expires_at=%d", expires_at
                    )
                    return pb.RefreshAuthResponse(
                        access_token=refreshed["access_token"],
                        expires_at=expires_at * 1000,
                    )
                except Exception as exc:
                    logger.error("RefreshAuth: OAuth refresh failed: %s", exc, exc_info=True)
                    context.abort(
                        grpc.StatusCode.UNAUTHENTICATED,
                        "What failed: OAuth token refresh failed. "
                        "How to fix: re-authenticate in Settings. "
                        "If the issue persists, verify your OAuth client credentials. "
                        "Fallback: browser cookie authentication or public mode is available.",
                    )
                    return pb.RefreshAuthResponse()

        # -------------------------------------------------------------------
        # Tier 2 — Browser cookie auth (no refresh mechanism)
        # -------------------------------------------------------------------
        if _load_auth_file() is not None:
            logger.info("RefreshAuth: browser-cookie auth does not expose refresh")
            return pb.RefreshAuthResponse(
                access_token="browser_cookie",
                expires_at=0,
            )

        # -------------------------------------------------------------------
        # Tier 3 — Nothing available
        # -------------------------------------------------------------------
        context.abort(
            grpc.StatusCode.UNAUTHENTICATED,
            "What failed: no credentials found. "
            "How to fix: authenticate first via Settings using OAuth or browser cookies. "
            "Fallback: public search is still available without login.",
        )
        return pb.RefreshAuthResponse()

    # -----------------------------------------------------------------------
    # yt-dlp track search (unauthenticated)
    # -----------------------------------------------------------------------

    def _search_tracks_ytdlp(self, query: str, limit: int) -> List[pb.Track]:
        """Return a list of protobuf Track messages using yt-dlp search.

        yt-dlp performs an unauthenticated YouTube search and returns actual
        music videos with valid videoIds.  These identifiers are later fed
        into the authenticated playback backend.
        """
        try:
            import yt_dlp
        except ImportError:
            logger.warning("yt-dlp is not installed; track search unavailable")
            return []

        ydl_opts: Dict[str, Any] = {
            "quiet": True,
            "extract_flat": True,
            "playlistend": limit,
        }

        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(f"ytsearch{limit}:{query}", download=False)
        except Exception as exc:
            logger.warning("yt-dlp track search failed: %s", exc)
            return []

        entries: List[Dict[str, Any]] = info.get("entries") or []
        tracks: List[pb.Track] = []
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            track = self._ytdlp_result_to_pb(entry)
            if track and track.id:
                tracks.append(track)
        return tracks

    def _ytdlp_result_to_pb(self, result: Dict[str, Any]) -> pb.Track:
        """Convert a yt-dlp search entry to a protobuf Track message."""
        video_id = result.get("id") or ""
        title = result.get("title") or ""
        if not video_id or not title:
            return pb.Track()

        uploader = result.get("uploader") or ""
        artists = [pb.Artist(id="", name=uploader)] if uploader else []

        duration_s = result.get("duration")
        duration_ms = int(duration_s * 1000) if isinstance(duration_s, (int, float)) else 0

        images: List[pb.Image] = []
        for thumb in result.get("thumbnails") or []:
            if isinstance(thumb, dict):
                images.append(
                    pb.Image(
                        url=thumb.get("url", ""),
                        width=thumb.get("width", 0),
                        height=thumb.get("height", 0),
                    )
                )

        return pb.Track(
            id=video_id,
            title=title,
            artists=artists,
            album=pb.Album(),
            duration_ms=duration_ms,
            images=images,
            explicit=False,
        )

    # -----------------------------------------------------------------------
    # Search
    # -----------------------------------------------------------------------

    def Search(
        self,
        request: pb.SearchRequest,
        context: grpc.ServicerContext,
    ) -> pb.SearchResponse:
        """Search YouTube Music for tracks, albums, artists, and playlists."""
        query = request.query.strip()
        if not query:
            context.abort(
                grpc.StatusCode.INVALID_ARGUMENT,
                "What failed: query must not be empty. "
                "How to fix: provide a non-empty search query.",
            )
            return pb.SearchResponse()

        limit = request.limit if request.limit > 0 else 20
        requested_types = set(request.types)

        type_filter: Dict[int, str] = {
            pb.MEDIA_TYPE_TRACK: "songs",
            pb.MEDIA_TYPE_ALBUM: "albums",
            pb.MEDIA_TYPE_ARTIST: "artists",
            pb.MEDIA_TYPE_PLAYLIST: "playlists",
        }

        if not requested_types or pb.MEDIA_TYPE_UNSPECIFIED in requested_types:
            categories_to_fetch = list(type_filter.items())
        else:
            categories_to_fetch = [
                (media_type, ytm_filter)
                for media_type, ytm_filter in type_filter.items()
                if media_type in requested_types
            ]

        tracks: List[pb.Track] = []
        albums: List[pb.Album] = []
        artists: List[pb.Artist] = []
        playlists: List[pb.Playlist] = []

        client = self._get_client()

        # Use the unauthenticated search client for all discovery.
        search_client = self._search_ytm

        for media_type, ytm_filter in categories_to_fetch:
            try:
                if media_type == pb.MEDIA_TYPE_TRACK:
                    # yt-dlp unauthenticated YouTube search returns actual
                    # music videos with reliable metadata and playable videoIds.
                    # The videoIds are later resolved by the authenticated
                    # playback backend, keeping search and playback decoupled.
                    tracks.extend(
                        self._search_tracks_ytdlp(query=query, limit=limit)
                    )
                else:
                    results = search_client.search(
                        query=query, filter=ytm_filter, limit=limit
                    )
                    if not isinstance(results, list):
                        continue

                    for item in results[:limit]:
                        if not isinstance(item, dict):
                            continue

                        if media_type == pb.MEDIA_TYPE_ALBUM:
                            albums.append(_ytm_album_to_pb(item))
                        elif media_type == pb.MEDIA_TYPE_ARTIST:
                            artists.append(_ytm_artist_to_pb(item))
                        elif media_type == pb.MEDIA_TYPE_PLAYLIST:
                            playlists.append(_ytm_playlist_to_pb(item))
            except Exception as exc:
                logger.warning(
                    "Search error for filter=%s: %s",
                    ytm_filter if media_type != pb.MEDIA_TYPE_TRACK else "tracks",
                    exc,
                )

        logger.info(
            "Search('%s') → tracks=%d albums=%d artists=%d playlists=%d",
            query,
            len(tracks),
            len(albums),
            len(artists),
            len(playlists),
        )

        return pb.SearchResponse(
            tracks=tracks,
            albums=albums,
            artists=artists,
            playlists=playlists,
            next_page_token="",
        )

    # -----------------------------------------------------------------------
    # SearchSuggestions
    # -----------------------------------------------------------------------

    def SearchSuggestions(
        self,
        request: pb.SearchSuggestionsRequest,
        context: grpc.ServicerContext,
    ) -> Iterator[pb.SearchSuggestionsResponse]:
        """Stream autocomplete suggestions for a partial search query."""
        query = request.query.strip()
        if not query:
            return

        limit = request.limit if request.limit > 0 else 10
        client = self._get_client()

        try:
            raw_suggestions = client.get_search_suggestions(query)
        except Exception as exc:
            logger.error("SearchSuggestions error: %s", exc, exc_info=True)
            context.abort(
                grpc.StatusCode.INTERNAL,
                f"What failed: failed to fetch suggestions: {exc}. "
                "How to fix: check your network connection or try again later. "
                "Fallback: public search is still available without login.",
            )
            return

        if not isinstance(raw_suggestions, list):
            return

        count = 0
        for suggestion in raw_suggestions:
            if count >= limit:
                break
            if not context.is_active():
                logger.debug("SearchSuggestions: client cancelled stream")
                return

            text = ""
            if isinstance(suggestion, str):
                text = suggestion
            elif isinstance(suggestion, dict):
                text = suggestion.get("suggestion", "") or suggestion.get("query", "")

            if not text:
                continue

            yield pb.SearchSuggestionsResponse(
                suggestion=text,
                type=pb.MEDIA_TYPE_UNSPECIFIED,
                related_id="",
            )
            count += 1

    # -----------------------------------------------------------------------
    # GetTrackDetails
    # -----------------------------------------------------------------------

    def GetTrackDetails(
        self,
        request: pb.GetTrackDetailsRequest,
        context: grpc.ServicerContext,
    ) -> pb.GetTrackDetailsResponse:
        """Fetch detailed metadata for a single track by video ID."""
        video_id = request.track_id.strip()
        if not video_id:
            context.abort(
                grpc.StatusCode.INVALID_ARGUMENT,
                "What failed: track_id must not be empty. "
                "How to fix: provide a valid track ID.",
            )
            return pb.GetTrackDetailsResponse()

        client = self._get_client()

        try:
            song_info = client.get_song(video_id)
        except Exception as exc:
            logger.error("GetTrackDetails error for %s: %s", video_id, exc, exc_info=True)
            context.abort(
                grpc.StatusCode.NOT_FOUND,
                f"What failed: track not found or API error: {exc}. "
                "How to fix: verify the track ID or try again later. "
                "Fallback: public search is still available without login.",
            )
            return pb.GetTrackDetailsResponse()

        video_details = song_info.get("videoDetails", {}) if isinstance(song_info, dict) else {}

        title = video_details.get("title", "")
        channel_name = video_details.get("author", "")
        duration_s = _safe_int(video_details.get("lengthSeconds"))
        thumbnails_raw = (
            video_details.get("thumbnail", {}).get("thumbnails", [])
            if isinstance(video_details, dict)
            else []
        )

        track = pb.Track(
            id=video_id,
            title=title,
            artists=[pb.Artist(id="", name=channel_name)],
            duration_ms=duration_s * 1000,
            images=_thumbnails_to_images(thumbnails_raw),
        )

        logger.debug("GetTrackDetails: %s → '%s'", video_id, title)
        return pb.GetTrackDetailsResponse(track=track)

    # -----------------------------------------------------------------------
    # ResolveStream
    # -----------------------------------------------------------------------

    def ResolveStream(
        self,
        request: pb.ResolveStreamRequest,
        context: grpc.ServicerContext,
    ) -> pb.ResolveStreamResponse:
        """Resolve a direct pre-signed audio stream URL for a track."""
        video_id = request.track_id.strip()
        if not video_id:
            context.abort(
                grpc.StatusCode.INVALID_ARGUMENT,
                "What failed: track_id must not be empty. "
                "How to fix: provide a valid track ID.",
            )
            return pb.ResolveStreamResponse()

        quality = request.preferred_quality
        logger.info("ResolveStream: video_id=%s quality=%d", video_id, quality)

        try:
            stream_url, headers, expiry_ms = _resolve_stream_url(video_id, quality)
        except RuntimeError as exc:
            logger.error("ResolveStream failed for %s: %s", video_id, exc)
            context.abort(grpc.StatusCode.INTERNAL, str(exc))
            return pb.ResolveStreamResponse()
        except Exception as exc:
            logger.error("ResolveStream failed for %s: %s", video_id, exc, exc_info=True)
            context.abort(
                grpc.StatusCode.INTERNAL,
                f"What failed: stream resolution failed: {exc}. "
                "How to fix: update yt-dlp: `pip install -U yt-dlp` or "
                "`yt-dlp -U --update-to nightly`. "
                "Fallback: public search is still available without streaming.",
            )
            return pb.ResolveStreamResponse()

        logger.info("ResolveStream: resolved URL (len=%d) for %s", len(stream_url), video_id)
        return pb.ResolveStreamResponse(
            stream_url=stream_url,
            headers=headers,
            expiry_ms=expiry_ms,
        )

    # -----------------------------------------------------------------------
    # StreamAudio
    # -----------------------------------------------------------------------

    def StreamAudio(
        self,
        request: pb.StreamAudioRequest,
        context: grpc.ServicerContext,
    ) -> Iterator[pb.AudioChunk]:
        """Stream raw audio bytes for a track directly over gRPC."""
        video_id = request.track_id.strip()
        if not video_id:
            context.abort(
                grpc.StatusCode.INVALID_ARGUMENT,
                "What failed: track_id must not be empty. "
                "How to fix: provide a valid track ID.",
            )
            return

        quality = request.quality
        start_ms = request.start_position_ms

        logger.info(
            "StreamAudio: video_id=%s quality=%d start_ms=%d",
            video_id,
            quality,
            start_ms,
        )

        chunk_size = 64 * 1024

        try:
            import urllib.request

            stream_url, headers, _ = _resolve_stream_url(video_id, quality)
            req = urllib.request.Request(stream_url, headers=headers)

            with urllib.request.urlopen(req, timeout=30) as resp:
                timestamp_ms = start_ms

                while True:
                    if not context.is_active():
                        logger.debug("StreamAudio: client closed stream for %s", video_id)
                        return

                    chunk = resp.read(chunk_size)
                    if not chunk:
                        yield pb.AudioChunk(data=b"", timestamp_ms=timestamp_ms, eof=True)
                        logger.info("StreamAudio: finished streaming %s", video_id)
                        return

                    yield pb.AudioChunk(data=chunk, timestamp_ms=timestamp_ms, eof=False)
                    timestamp_ms += int(len(chunk) / 16)
        except RuntimeError as exc:
            logger.error("StreamAudio error for %s: %s", video_id, exc)
            context.abort(grpc.StatusCode.INTERNAL, str(exc))
        except Exception as exc:
            logger.error("StreamAudio error for %s: %s", video_id, exc, exc_info=True)
            context.abort(
                grpc.StatusCode.INTERNAL,
                f"What failed: audio streaming failed: {exc}. "
                "How to fix: check your network connection or update yt-dlp. "
                "Fallback: public search is still available without streaming.",
            )

    # -----------------------------------------------------------------------
    # GetUserLibrary
    # -----------------------------------------------------------------------

    def GetUserLibrary(
        self,
        request: pb.GetUserLibraryRequest,
        context: grpc.ServicerContext,
    ) -> pb.GetUserLibraryResponse:
        """Return the authenticated user's liked songs library."""
        client = self._require_auth(context)

        limit = request.limit if request.limit > 0 else 25

        try:
            liked_songs = client.get_liked_songs(limit=limit)
        except Exception as exc:
            logger.error("GetUserLibrary error: %s", exc, exc_info=True)
            error_str = str(exc).lower()
            if "unauthorized" in error_str or "authentication" in error_str or "sign in" in error_str:
                context.abort(
                    grpc.StatusCode.UNAUTHENTICATED,
                    "What failed: your session expired or you are not logged in. "
                    "How to fix: re-authenticate in Settings using OAuth or browser cookies. "
                    "Fallback: public search is still available without login.",
                )
            else:
                context.abort(
                    grpc.StatusCode.INTERNAL,
                    f"What failed: failed to fetch library: {exc}. "
                    "How to fix: check your network connection or try again later. "
                    "Fallback: public search is still available without login.",
                )
            return pb.GetUserLibraryResponse()

        tracks: List[pb.Track] = []
        raw_tracks = liked_songs.get("tracks", []) if isinstance(liked_songs, dict) else []

        for item in raw_tracks:
            if isinstance(item, dict):
                tracks.append(_ytm_track_to_pb(item))

        logger.info("GetUserLibrary: returning %d liked tracks", len(tracks))
        return pb.GetUserLibraryResponse(tracks=tracks, next_page_token="")

    # -----------------------------------------------------------------------
    # GetUserPlaylists
    # -----------------------------------------------------------------------

    def GetUserPlaylists(
        self,
        request: pb.GetUserPlaylistsRequest,
        context: grpc.ServicerContext,
    ) -> pb.GetUserPlaylistsResponse:
        """Return the authenticated user's playlists."""
        client = self._require_auth(context)

        limit = request.limit if request.limit > 0 else 50

        try:
            raw_playlists = client.get_library_playlists(limit=limit)
        except Exception as exc:
            logger.error("GetUserPlaylists error: %s", exc, exc_info=True)
            error_str = str(exc).lower()
            if "unauthorized" in error_str or "authentication" in error_str or "sign in" in error_str:
                context.abort(
                    grpc.StatusCode.UNAUTHENTICATED,
                    "What failed: your session expired or you are not logged in. "
                    "How to fix: re-authenticate in Settings using OAuth or browser cookies. "
                    "Fallback: public search is still available without login.",
                )
            else:
                context.abort(
                    grpc.StatusCode.INTERNAL,
                    f"What failed: failed to fetch playlists: {exc}. "
                    "How to fix: check your network connection or try again later. "
                    "Fallback: public search is still available without login.",
                )
            return pb.GetUserPlaylistsResponse()

        playlists: List[pb.Playlist] = []

        if isinstance(raw_playlists, list):
            for item in raw_playlists[:limit]:
                if not isinstance(item, dict):
                    continue

                browse_id = item.get("playlistId") or item.get("browseId") or ""
                title = item.get("title") or ""
                count = _safe_int(item.get("count") or item.get("trackCount"))
                thumbnails = _thumbnails_to_images(item.get("thumbnails"))

                playlists.append(
                    pb.Playlist(
                        id=browse_id,
                        title=title,
                        track_count=count,
                        images=thumbnails,
                    )
                )

        logger.info("GetUserPlaylists: returning %d playlists", len(playlists))
        return pb.GetUserPlaylistsResponse(playlists=playlists, next_page_token="")

    # -----------------------------------------------------------------------
    # GetPlaylistDetails
    # -----------------------------------------------------------------------

    def GetPlaylistDetails(
        self,
        request: pb.GetPlaylistDetailsRequest,
        context: grpc.ServicerContext,
    ) -> pb.GetPlaylistDetailsResponse:
        """Return metadata and track listing for a specific playlist."""
        playlist_id = request.playlist_id.strip()
        if not playlist_id:
            context.abort(
                grpc.StatusCode.INVALID_ARGUMENT,
                "What failed: playlist_id must not be empty. "
                "How to fix: provide a valid playlist ID.",
            )
            return pb.GetPlaylistDetailsResponse()

        limit = request.limit if request.limit > 0 else 100
        client = self._get_client()

        try:
            raw = client.get_playlist(playlistId=playlist_id, limit=limit)
        except Exception as exc:
            logger.error("GetPlaylistDetails error for %s: %s", playlist_id, exc, exc_info=True)
            context.abort(
                grpc.StatusCode.NOT_FOUND,
                f"What failed: playlist not found or API error: {exc}. "
                "How to fix: verify the playlist ID or try again later. "
                "Fallback: public search is still available without login.",
            )
            return pb.GetPlaylistDetailsResponse()

        if not isinstance(raw, dict):
            context.abort(
                grpc.StatusCode.INTERNAL,
                "What failed: unexpected API response format. "
                "How to fix: try again later. "
                "Fallback: public search is still available without login.",
            )
            return pb.GetPlaylistDetailsResponse()

        title = raw.get("title") or ""
        description = raw.get("description") or ""
        author = raw.get("author") or {}
        owner_name = author.get("name") or "" if isinstance(author, dict) else str(author)
        thumbnails = _thumbnails_to_images(raw.get("thumbnails"))
        track_count = _safe_int(raw.get("trackCount"))

        playlist_pb = pb.Playlist(
            id=playlist_id,
            title=title,
            description=description,
            owner_name=owner_name,
            images=thumbnails,
            track_count=track_count,
        )

        tracks: List[pb.Track] = []
        raw_tracks = raw.get("tracks") or []

        for item in raw_tracks[:limit]:
            if not isinstance(item, dict):
                continue

            video_id = item.get("videoId") or ""
            if not video_id:
                continue

            tracks.append(_ytm_track_to_pb(item))

        logger.info(
            "GetPlaylistDetails: playlist_id=%s title='%s' tracks=%d",
            playlist_id,
            title,
            len(tracks),
        )

        return pb.GetPlaylistDetailsResponse(
            playlist=playlist_pb,
            tracks=tracks,
            next_page_token="",
        )
