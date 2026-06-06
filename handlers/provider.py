"""
handlers/provider.py — MusicProviderService gRPC servicer implementation.

Maps every RPC defined in music_provider.proto to concrete ytmusicapi calls.
Authentication credentials are stored in the host OS standard configuration
directory: ~/.config/gozik/ytmusic_auth.json
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

import grpc
import ytmusicapi
from selenium import webdriver
from selenium.webdriver.chrome.service import Service as ChromeService
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait
from webdriver_manager.chrome import ChromeDriverManager

from generated import music_provider_pb2 as pb
from generated import music_provider_pb2_grpc as pb_grpc

# ---------------------------------------------------------------------------
# Module-level logger
# ---------------------------------------------------------------------------
logger = logging.getLogger("gozik.ytmusic.provider")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
PROVIDER_ID = "ytmusic"
DISPLAY_NAME = "YouTube Music"

# Path to the persisted OAuth / browser cookie credential file.
_AUTH_FILE: Path = (
    Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    / "gozik"
    / "ytmusic_auth.json"
)

# ytmusicapi's OAuth credentials file (separate JSON produced by setup_oauth).
_OAUTH_FILE: Path = (
    Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    / "gozik"
    / "ytmusic_oauth.json"
)

# Audio quality → yt-dlp format selector mapping.
_QUALITY_FORMAT: Dict[int, str] = {
    pb.AUDIO_QUALITY_UNSPECIFIED: "bestaudio",
    pb.AUDIO_QUALITY_LOW: "worstaudio",
    pb.AUDIO_QUALITY_MEDIUM: "bestaudio[abr<=128]",
    pb.AUDIO_QUALITY_HIGH: "bestaudio",
}

# Capabilities advertised by this plugin.
_CAPABILITIES = [
    pb.PROVIDER_CAPABILITY_SEARCH,
    pb.PROVIDER_CAPABILITY_STREAM_TRACK,
    pb.PROVIDER_CAPABILITY_LIBRARY_MANAGEMENT,
]


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _load_auth_file() -> Optional[Dict[str, Any]]:
    """Load and validate the persisted auth JSON.

    Returns the parsed dict on success, or None if the file is absent or
    structurally invalid.
    """
    try:
        if not _AUTH_FILE.exists():
            return None
        raw = _AUTH_FILE.read_text(encoding="utf-8")
        data = json.loads(raw)
        # Minimal structural check: the file must be a non-empty dict.
        if not isinstance(data, dict) or not data:
            return None
        return data
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("Failed to read auth file %s: %s", _AUTH_FILE, exc)
        return None


def _save_auth_file(data: Dict[str, Any]) -> None:
    """Persist auth data to the config directory, creating parents as needed."""
    _AUTH_FILE.parent.mkdir(parents=True, exist_ok=True)
    _AUTH_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")
    logger.info("Auth credentials saved to %s", _AUTH_FILE)


def _build_ytmusic(auth_data: Optional[Dict[str, Any]] = None) -> ytmusicapi.YTMusic:
    """Construct a YTMusic instance.

    If *auth_data* is provided and the OAuth file exists, the OAuth path is
    used. If the browser auth file exists on disk, it is used directly.
    Falls back to an unauthenticated client as a last resort (limited to
    public search; library calls will fail with HTTP 401).
    """
    if _OAUTH_FILE.exists():
        logger.debug("Initialising YTMusic with OAuth credentials from %s", _OAUTH_FILE)
        return ytmusicapi.YTMusic(str(_OAUTH_FILE))

    if _AUTH_FILE.exists():
        logger.debug("Initialising YTMusic with browser auth from %s", _AUTH_FILE)
        try:
            return ytmusicapi.YTMusic(str(_AUTH_FILE))
        except Exception as exc:  # noqa: BLE001
            logger.warning("Failed to load auth from %s: %s — falling back to unauthenticated", _AUTH_FILE, exc)

    logger.info("Initialising unauthenticated YTMusic client")
    return ytmusicapi.YTMusic()


# ---------------------------------------------------------------------------
# Browser cookie auth via Selenium
# ---------------------------------------------------------------------------

def _generate_sapisid_auth(cookie_str: str) -> tuple[str, str]:
    """Generate SAPISIDHASH authorization header from browser cookies."""
    cookie_dict: Dict[str, str] = {}
    for part in cookie_str.split(";"):
        if "=" in part:
            k, v = part.strip().split("=", 1)
            cookie_dict[k] = v

    sapisid = cookie_dict.get("SAPISID") or cookie_dict.get("__Secure-3PAPISID")
    if not sapisid:
        raise RuntimeError("SAPISID cookie not found — are you logged in?")

    origin = "https://music.youtube.com"
    ts = int(time.time())
    msg = f"{ts} {sapisid} {origin}"
    sha1 = hashlib.sha1(msg.encode()).hexdigest()
    return f"SAPISIDHASH {ts}_{sha1}", "0"


def _selenium_auth_thread(result: Dict[str, Any]) -> None:
    """Open a real browser, wait for the user to log in, then harvest cookies
    and build a ytmusicapi-compatible headers_auth.json file."""
    options = webdriver.ChromeOptions()
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_experimental_option("excludeSwitches", ["enable-automation"])
    options.add_experimental_option("useAutomationExtension", False)
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.binary_location = "/usr/bin/chromium"

    driver: Optional[webdriver.Chrome] = None
    try:
        service = ChromeService(ChromeDriverManager().install())
        driver = webdriver.Chrome(service=service, options=options)
        driver.get("https://music.youtube.com")

        # Wait up to 5 minutes for the logged-in UI to appear.
        WebDriverWait(driver, 300).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, "ytmusic-pivot-bar-item-renderer"))
        )

        # Harvest cookies via Selenium (includes HttpOnly cookies).
        cookies_list = driver.get_cookies()
        cookie_dict = {c["name"]: c["value"] for c in cookies_list}
        cookie_str = "; ".join(f"{k}={v}" for k, v in cookie_dict.items())

        auth_header, auth_user = _generate_sapisid_auth(cookie_str)

        visitor_id = driver.execute_script(
            "try { return window.yt.config_.VISITOR_DATA; } catch(e) { return ''; }"
        )
        user_agent = driver.execute_script("return navigator.userAgent;")

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

        _AUTH_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(_AUTH_FILE, "w", encoding="utf-8") as f:
            json.dump(headers, f, indent=4, ensure_ascii=True)

        result["success"] = True
        logger.info("Selenium auth succeeded — credentials saved to %s", _AUTH_FILE)
    except Exception as exc:
        logger.error("Selenium auth failed: %s", exc, exc_info=True)
        result["error"] = str(exc)
    finally:
        if driver is not None:
            driver.quit()


def _auth_status() -> int:
    """Determine the current authentication status enum value."""
    if _OAUTH_FILE.exists() or _load_auth_file() is not None:
        return pb.AUTH_STATUS_AUTHENTICATED
    return pb.AUTH_STATUS_UNAUTHENTICATED


# ---------------------------------------------------------------------------
# Protobuf conversion helpers
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
    """Convert a list of thumbnail dicts to protobuf Image messages."""
    if not thumbnails:
        return []
    return [_to_pb_image(t) for t in thumbnails if isinstance(t, dict)]


def _safe_int(value: Any, default: int = 0) -> int:
    """Safely cast a value to int, returning *default* on failure."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _duration_to_ms(duration_str: Optional[str]) -> int:
    """Convert a 'M:SS' or 'H:MM:SS' duration string to milliseconds."""
    if not duration_str:
        return 0
    parts = duration_str.strip().split(":")
    try:
        if len(parts) == 2:  # M:SS
            total_s = int(parts[0]) * 60 + int(parts[1])
        elif len(parts) == 3:  # H:MM:SS
            total_s = int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])
        else:
            return 0
        return total_s * 1000
    except (ValueError, IndexError):
        return 0


def _ytm_track_to_pb(result: Dict[str, Any]) -> pb.Track:
    """Convert a ytmusicapi search result (type 'song' or 'video') to a Track pb."""
    video_id = result.get("videoId") or ""
    title = result.get("title") or ""
    artists = [_to_pb_artist(a) for a in (result.get("artists") or [])]
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
    """Convert a ytmusicapi search result (type 'album') to an Album pb."""
    browse_id = result.get("browseId") or ""
    title = result.get("title") or ""
    artists = [_to_pb_artist(a) for a in (result.get("artists") or [])]
    thumbnails = _thumbnails_to_images(result.get("thumbnails"))
    year_str = result.get("year")
    return pb.Album(
        id=browse_id,
        title=title,
        artists=artists,
        images=thumbnails,
        release_year=_safe_int(year_str),
        track_count=_safe_int(result.get("trackCount")),
    )


def _ytm_artist_to_pb(result: Dict[str, Any]) -> pb.Artist:
    """Convert a ytmusicapi search result (type 'artist') to an Artist pb."""
    browse_id = result.get("browseId") or ""
    name = result.get("artist") or result.get("name") or ""
    return pb.Artist(id=browse_id, name=name)


def _ytm_playlist_to_pb(result: Dict[str, Any]) -> pb.Playlist:
    """Convert a ytmusicapi search result (type 'playlist') to a Playlist pb."""
    browse_id = result.get("browseId") or ""
    title = result.get("title") or ""
    thumbnails = _thumbnails_to_images(result.get("thumbnails"))
    author = result.get("author") or {}
    if isinstance(author, dict):
        owner_name = author.get("name") or ""
    else:
        owner_name = str(author)
    item_count = result.get("itemCount") or 0
    return pb.Playlist(
        id=browse_id,
        title=title,
        owner_name=owner_name,
        images=thumbnails,
        track_count=_safe_int(item_count),
    )


# ---------------------------------------------------------------------------
# Stream URL resolution
# ---------------------------------------------------------------------------

def _resolve_stream_url(video_id: str, quality: int) -> tuple[str, dict[str, str], int]:
    """Extract a direct audio stream URL for the given video_id.

    Uses yt-dlp to obtain a pre-signed URL. Returns a tuple of
    (url, headers_dict, expiry_ms) where expiry_ms is a UTC epoch
    millisecond timestamp after which the URL may be invalid.

    Raises grpc.RpcError (via context.abort) on failure — callers are
    responsible for catching exceptions and translating them.
    """
    try:
        import yt_dlp  # noqa: PLC0415 — lazy import to avoid mandatory dep at module load
    except ImportError as exc:
        raise RuntimeError("yt-dlp is not installed; install it via requirements.txt") from exc

    format_selector = _QUALITY_FORMAT.get(quality, "bestaudio")
    ydl_opts = {
        "format": format_selector,
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        # Prevent yt-dlp from writing to disk; we only need the URL.
        "skip_download": True,
        "extract_flat": False,
    }

    url = f"https://music.youtube.com/watch?v={video_id}"
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=False)

    if info is None:
        raise RuntimeError(f"yt-dlp returned no info for video_id={video_id}")

    stream_url: str = info.get("url", "")
    if not stream_url:
        # Some formats store the URL inside a nested 'formats' list.
        formats = info.get("formats", [])
        if formats:
            stream_url = formats[-1].get("url", "")
    if not stream_url:
        raise RuntimeError(f"No stream URL found for video_id={video_id}")

    # Extract HTTP headers required to play the stream (e.g. Referer, Origin).
    http_headers: dict[str, str] = info.get("http_headers", {})
    # Convert all header values to strings for proto compatibility.
    headers = {k: str(v) for k, v in http_headers.items()}

    # Approximate expiry: YouTube signed URLs are typically valid for ~6 hours.
    expiry_ms = int((time.time() + 6 * 3600) * 1000)

    return stream_url, headers, expiry_ms


# ---------------------------------------------------------------------------
# Servicer implementation
# ---------------------------------------------------------------------------

class MusicProviderServicer(pb_grpc.MusicProviderServiceServicer):
    """Full implementation of the MusicProviderService gRPC service.

    Thread-safety notes:
    - The YTMusic client instance is rebuilt lazily and guarded by a lock.
    - All public RPC methods may be called from multiple worker threads
      simultaneously; shared mutable state is confined to _ytm and _lock.
    """

    def __init__(self) -> None:
        """Initialise the servicer, loading existing credentials if available."""
        self._lock = threading.Lock()
        self._ytm: Optional[ytmusicapi.YTMusic] = None
        self._selenium_result: Dict[str, Any] = {}
        logger.info("MusicProviderServicer initialised — auth file: %s", _AUTH_FILE)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _get_client(self) -> ytmusicapi.YTMusic:
        """Return the shared YTMusic client, creating it if necessary."""
        with self._lock:
            if self._ytm is None:
                self._ytm = _build_ytmusic()
            return self._ytm

    def _invalidate_client(self) -> None:
        """Force the client to be rebuilt on the next call."""
        with self._lock:
            self._ytm = None

    # ------------------------------------------------------------------
    # RPC: GetProviderMetadata
    # ------------------------------------------------------------------

    def GetProviderMetadata(
        self,
        request: pb.GetProviderMetadataRequest,
        context: grpc.ServicerContext,
    ) -> pb.GetProviderMetadataResponse:
        """Return static plugin metadata and the current authentication state.

        Checks whether the credential files exist on disk; does not perform
        a live API call to avoid blocking or raising on startup.
        """
        status = _auth_status()
        logger.debug("GetProviderMetadata — auth_status=%d", status)
        return pb.GetProviderMetadataResponse(
            provider_id=PROVIDER_ID,
            display_name=DISPLAY_NAME,
            capabilities=_CAPABILITIES,
            auth_status=status,
        )

    # ------------------------------------------------------------------
    # RPC: InitiateAuth
    # ------------------------------------------------------------------

    def InitiateAuth(
        self,
        request: pb.InitiateAuthRequest,
        context: grpc.ServicerContext,
    ) -> pb.InitiateAuthResponse:
        """Launch a Selenium-controlled browser so the user can log in to
        YouTube Music interactively.  Cookies are harvested automatically once
        the logged-in UI is detected, and a ytmusicapi-compatible
        headers_auth.json is written to disk."""
        logger.info("InitiateAuth: launching Selenium browser auth")
        with self._lock:
            result: Dict[str, Any] = {}
            self._selenium_result = result
        thread = threading.Thread(
            target=_selenium_auth_thread, args=(result,), daemon=True
        )
        thread.start()
        return pb.InitiateAuthResponse(
            auth_url="https://music.youtube.com",
            device_code="",
        )

    # ------------------------------------------------------------------
    # RPC: CompleteAuth
    # ------------------------------------------------------------------

    def CompleteAuth(
        self,
        request: pb.CompleteAuthRequest,
        context: grpc.ServicerContext,
    ) -> pb.CompleteAuthResponse:
        """Check whether the Selenium browser auth already finished and, if so,
        mark the client as authenticated.  Falls back to the legacy
        cookie_json / OAuth paths if Selenium did not run."""
        params: Dict[str, str] = dict(request.params)
        logger.info("CompleteAuth invoked with param keys: %s", list(params.keys()))

        # ------------------------------------------------------------------
        # Path 0: Selenium browser auth already succeeded in background.
        # ------------------------------------------------------------------
        with self._lock:
            result = self._selenium_result
        if result.get("success"):
            self._invalidate_client()
            logger.info("CompleteAuth: Selenium browser auth succeeded")
            return pb.CompleteAuthResponse(
                access_token="browser_cookie",
                refresh_token="",
                expires_at=0,
            )
        if result.get("error"):
            context.abort(
                grpc.StatusCode.UNAUTHENTICATED,
                f"Browser auth failed: {result['error']}",
            )
            return pb.CompleteAuthResponse()

        # ------------------------------------------------------------------
        # Path 1: Browser cookie JSON provided directly.
        # ------------------------------------------------------------------
        cookie_json = params.get("cookie_json", "")
        if cookie_json:
            try:
                cookie_data = json.loads(cookie_json)
                if not isinstance(cookie_data, dict):
                    raise ValueError("cookie_json must be a JSON object")
                _save_auth_file(cookie_data)
                self._invalidate_client()
                logger.info("CompleteAuth: browser cookie credentials saved")
                return pb.CompleteAuthResponse(
                    access_token="browser_cookie",
                    refresh_token="",
                    expires_at=0,
                )
            except (json.JSONDecodeError, ValueError) as exc:
                context.abort(grpc.StatusCode.INVALID_ARGUMENT, f"Invalid cookie_json: {exc}")
                return pb.CompleteAuthResponse()

        # ------------------------------------------------------------------
        # Path 2: Poll the pending OAuth device-code grant.
        # ------------------------------------------------------------------
        pending_oauth = getattr(self, "_pending_oauth", None)
        if pending_oauth is not None:
            try:
                oauth_helper = ytmusicapi.OAuthCredentials(
                    client_id=ytmusicapi.YTMusic.OAUTH_CLIENT_ID,
                    client_secret=ytmusicapi.YTMusic.OAUTH_CLIENT_SECRET,
                    proxies=None,
                    session=None,
                )
                token_response = oauth_helper.token_from_code(pending_oauth)
                _OAUTH_FILE.parent.mkdir(parents=True, exist_ok=True)
                _OAUTH_FILE.write_text(json.dumps(token_response, indent=2), encoding="utf-8")
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
            except Exception as exc:  # noqa: BLE001
                logger.error("CompleteAuth OAuth poll failed: %s", exc, exc_info=True)
                context.abort(grpc.StatusCode.UNAUTHENTICATED, f"OAuth token exchange failed: {exc}")
                return pb.CompleteAuthResponse()

        # ------------------------------------------------------------------
        # Path 3: Manual auth_code provided (not a standard flow for YTM).
        # ------------------------------------------------------------------
        auth_code = params.get("auth_code", "")
        if auth_code:
            logger.warning(
                "CompleteAuth: manual auth_code path is not fully supported "
                "by ytmusicapi. Use InitiateAuth first or provide cookie_json."
            )
            context.abort(
                grpc.StatusCode.UNIMPLEMENTED,
                "Manual auth_code exchange requires a running InitiateAuth session. "
                "Restart the flow via InitiateAuth or supply 'cookie_json' instead.",
            )
            return pb.CompleteAuthResponse()

        context.abort(grpc.StatusCode.INVALID_ARGUMENT, "No valid auth payload provided in 'params'")
        return pb.CompleteAuthResponse()

    # ------------------------------------------------------------------
    # RPC: RefreshAuth
    # ------------------------------------------------------------------

    def RefreshAuth(
        self,
        request: pb.RefreshAuthRequest,
        context: grpc.ServicerContext,
    ) -> pb.RefreshAuthResponse:
        """Refresh an OAuth access token using the stored refresh_token.

        If ytmusicapi manages the token lifecycle internally (OAuth file),
        we simply rebuild the client which triggers its own refresh logic.
        """
        logger.info("RefreshAuth requested")
        try:
            if _OAUTH_FILE.exists():
                # ytmusicapi will automatically refresh the token when the
                # client is reconstructed and the stored token has expired.
                self._invalidate_client()
                client = self._get_client()
                # Read back the (potentially refreshed) token file.
                token_data = json.loads(_OAUTH_FILE.read_text(encoding="utf-8"))
                access_token = token_data.get("access_token", "")
                expires_in = int(token_data.get("expires_in", 3600))
                expires_at = int(time.time() * 1000) + expires_in * 1000
                logger.info("RefreshAuth: token refreshed, expires_at=%d", expires_at)
                return pb.RefreshAuthResponse(
                    access_token=access_token,
                    expires_at=expires_at,
                )

            # For browser-cookie auth there is no expiry or refresh mechanism.
            if _AUTH_FILE.exists():
                logger.info("RefreshAuth: browser-cookie auth does not expire")
                return pb.RefreshAuthResponse(
                    access_token="browser_cookie",
                    expires_at=0,
                )

            context.abort(grpc.StatusCode.UNAUTHENTICATED, "No credentials found; authenticate first")
            return pb.RefreshAuthResponse()
        except Exception as exc:  # noqa: BLE001
            logger.error("RefreshAuth error: %s", exc, exc_info=True)
            context.abort(grpc.StatusCode.INTERNAL, f"RefreshAuth failed: {exc}")
            return pb.RefreshAuthResponse()

    # ------------------------------------------------------------------
    # RPC: Search
    # ------------------------------------------------------------------

    def Search(
        self,
        request: pb.SearchRequest,
        context: grpc.ServicerContext,
    ) -> pb.SearchResponse:
        """Search YouTube Music for tracks, albums, artists, and/or playlists.

        The *types* field controls which result categories are requested.
        When empty, all categories are searched. Results are capped by
        the *limit* field (applied per category).
        """
        query = request.query.strip()
        if not query:
            context.abort(grpc.StatusCode.INVALID_ARGUMENT, "Query must not be empty")
            return pb.SearchResponse()

        limit = request.limit if request.limit > 0 else 20
        requested_types = set(request.types)

        # Determine which ytmusicapi filter values to query.
        # Mapping: proto MediaType enum → ytmusicapi filter string.
        _TYPE_FILTER: Dict[int, str] = {
            pb.MEDIA_TYPE_TRACK: "songs",
            pb.MEDIA_TYPE_ALBUM: "albums",
            pb.MEDIA_TYPE_ARTIST: "artists",
            pb.MEDIA_TYPE_PLAYLIST: "playlists",
        }

        # If no specific types requested, search all categories.
        if not requested_types or pb.MEDIA_TYPE_UNSPECIFIED in requested_types:
            categories_to_fetch = list(_TYPE_FILTER.items())
        else:
            categories_to_fetch = [
                (t, f) for t, f in _TYPE_FILTER.items() if t in requested_types
            ]

        tracks: List[pb.Track] = []
        albums: List[pb.Album] = []
        artists: List[pb.Artist] = []
        playlists: List[pb.Playlist] = []

        client = self._get_client()
        for media_type, ytm_filter in categories_to_fetch:
            try:
                results = client.search(query=query, filter=ytm_filter, limit=limit)
                if not isinstance(results, list):
                    continue
                for item in results[:limit]:
                    if not isinstance(item, dict):
                        continue
                    result_type = item.get("resultType", "")
                    if media_type == pb.MEDIA_TYPE_TRACK:
                        tracks.append(_ytm_track_to_pb(item))
                    elif media_type == pb.MEDIA_TYPE_ALBUM:
                        albums.append(_ytm_album_to_pb(item))
                    elif media_type == pb.MEDIA_TYPE_ARTIST:
                        artists.append(_ytm_artist_to_pb(item))
                    elif media_type == pb.MEDIA_TYPE_PLAYLIST:
                        playlists.append(_ytm_playlist_to_pb(item))
            except Exception as exc:  # noqa: BLE001
                # Log but continue; partial results are better than a hard failure.
                logger.warning("Search error for filter=%s: %s", ytm_filter, exc)

        logger.info(
            "Search('%s') → tracks=%d albums=%d artists=%d playlists=%d",
            query, len(tracks), len(albums), len(artists), len(playlists),
        )
        return pb.SearchResponse(
            tracks=tracks,
            albums=albums,
            artists=artists,
            playlists=playlists,
            next_page_token="",  # ytmusicapi does not expose continuation tokens
        )

    # ------------------------------------------------------------------
    # RPC: SearchSuggestions (server-streaming)
    # ------------------------------------------------------------------

    def SearchSuggestions(
        self,
        request: pb.SearchSuggestionsRequest,
        context: grpc.ServicerContext,
    ) -> Iterator[pb.SearchSuggestionsResponse]:
        """Stream autocomplete suggestions for a partial search query.

        Each suggestion is emitted as a separate SearchSuggestionsResponse
        message so the Go client can render results incrementally.
        """
        query = request.query.strip()
        if not query:
            return

        limit = request.limit if request.limit > 0 else 10
        client = self._get_client()
        try:
            raw_suggestions = client.get_search_suggestions(query)
        except Exception as exc:  # noqa: BLE001
            logger.error("SearchSuggestions error: %s", exc, exc_info=True)
            context.abort(grpc.StatusCode.INTERNAL, f"Failed to fetch suggestions: {exc}")
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

    # ------------------------------------------------------------------
    # RPC: GetTrackDetails
    # ------------------------------------------------------------------

    def GetTrackDetails(
        self,
        request: pb.GetTrackDetailsRequest,
        context: grpc.ServicerContext,
    ) -> pb.GetTrackDetailsResponse:
        """Fetch detailed metadata for a single track by its video ID."""
        video_id = request.track_id.strip()
        if not video_id:
            context.abort(grpc.StatusCode.INVALID_ARGUMENT, "track_id must not be empty")
            return pb.GetTrackDetailsResponse()

        client = self._get_client()
        try:
            # get_song() returns a dict with full track metadata including
            # streaming formats, but we only use the metadata fields here.
            song_info = client.get_song(video_id)
        except Exception as exc:  # noqa: BLE001
            logger.error("GetTrackDetails error for %s: %s", video_id, exc, exc_info=True)
            context.abort(grpc.StatusCode.NOT_FOUND, f"Track not found or API error: {exc}")
            return pb.GetTrackDetailsResponse()

        video_details = song_info.get("videoDetails", {}) if isinstance(song_info, dict) else {}
        microformat = song_info.get("microformat", {}) if isinstance(song_info, dict) else {}
        music_details = microformat.get("microformatDataRenderer", {})

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

    # ------------------------------------------------------------------
    # RPC: ResolveStream
    # ------------------------------------------------------------------

    def ResolveStream(
        self,
        request: pb.ResolveStreamRequest,
        context: grpc.ServicerContext,
    ) -> pb.ResolveStreamResponse:
        """Resolve a direct, pre-signed audio stream URL for a track.

        Delegates to yt-dlp for URL extraction. The returned URL is
        ephemeral (typically valid for ~6 hours) and must not be cached
        beyond the expiry timestamp.
        """
        video_id = request.track_id.strip()
        if not video_id:
            context.abort(grpc.StatusCode.INVALID_ARGUMENT, "track_id must not be empty")
            return pb.ResolveStreamResponse()

        quality = request.preferred_quality
        logger.info("ResolveStream: video_id=%s quality=%d", video_id, quality)
        try:
            stream_url, headers, expiry_ms = _resolve_stream_url(video_id, quality)
        except Exception as exc:  # noqa: BLE001
            logger.error("ResolveStream failed for %s: %s", video_id, exc, exc_info=True)
            context.abort(grpc.StatusCode.INTERNAL, f"Stream resolution failed: {exc}")
            return pb.ResolveStreamResponse()

        logger.info("ResolveStream: resolved URL (len=%d) for %s", len(stream_url), video_id)
        return pb.ResolveStreamResponse(
            stream_url=stream_url,
            headers=headers,
            expiry_ms=expiry_ms,
        )

    # ------------------------------------------------------------------
    # RPC: StreamAudio (server-streaming)
    # ------------------------------------------------------------------

    def StreamAudio(
        self,
        request: pb.StreamAudioRequest,
        context: grpc.ServicerContext,
    ) -> Iterator[pb.AudioChunk]:
        """Stream raw audio bytes for a track directly over gRPC.

        This is an alternative to ResolveStream: instead of returning a URL,
        the server fetches the audio via yt-dlp and pipes the data in chunks.
        Useful when the Go client cannot access YouTube URLs directly.
        """
        video_id = request.track_id.strip()
        if not video_id:
            context.abort(grpc.StatusCode.INVALID_ARGUMENT, "track_id must not be empty")
            return

        quality = request.quality
        start_ms = request.start_position_ms

        logger.info(
            "StreamAudio: video_id=%s quality=%d start_ms=%d",
            video_id, quality, start_ms,
        )

        try:
            import yt_dlp  # noqa: PLC0415
        except ImportError:
            context.abort(
                grpc.StatusCode.UNAVAILABLE,
                "yt-dlp is not installed; cannot stream audio directly",
            )
            return

        format_selector = _QUALITY_FORMAT.get(quality, "bestaudio")
        url = f"https://music.youtube.com/watch?v={video_id}"
        chunk_size = 64 * 1024  # 64 KiB chunks

        try:
            import io
            import urllib.request

            # Step 1: Resolve the stream URL.
            stream_url, headers, _ = _resolve_stream_url(video_id, quality)

            # Step 2: Open the HTTP stream and pipe bytes into AudioChunk messages.
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
                    # Approximate timestamp: assume 128 kbps average bitrate.
                    timestamp_ms += int(len(chunk) / 16)  # bytes / (128000/8/1000)
        except Exception as exc:  # noqa: BLE001
            logger.error("StreamAudio error for %s: %s", video_id, exc, exc_info=True)
            context.abort(grpc.StatusCode.INTERNAL, f"Audio streaming failed: {exc}")

    # ------------------------------------------------------------------
    # RPC: GetUserLibrary
    # ------------------------------------------------------------------

    def GetUserLibrary(
        self,
        request: pb.GetUserLibraryRequest,
        context: grpc.ServicerContext,
    ) -> pb.GetUserLibraryResponse:
        """Return the authenticated user's liked songs library.

        Requires an authenticated client. Returns UNAUTHENTICATED if no
        credentials are available.
        """
        if _auth_status() != pb.AUTH_STATUS_AUTHENTICATED:
            context.abort(
                grpc.StatusCode.UNAUTHENTICATED,
                "Authentication required to access user library",
            )
            return pb.GetUserLibraryResponse()

        limit = request.limit if request.limit > 0 else 25
        client = self._get_client()
        try:
            liked_songs = client.get_liked_songs(limit=limit)
        except Exception as exc:  # noqa: BLE001
            logger.error("GetUserLibrary error: %s", exc, exc_info=True)
            context.abort(grpc.StatusCode.INTERNAL, f"Failed to fetch library: {exc}")
            return pb.GetUserLibraryResponse()

        tracks: List[pb.Track] = []
        raw_tracks = liked_songs.get("tracks", []) if isinstance(liked_songs, dict) else []
        for item in raw_tracks:
            if isinstance(item, dict):
                tracks.append(_ytm_track_to_pb(item))

        logger.info("GetUserLibrary: returning %d liked tracks", len(tracks))
        return pb.GetUserLibraryResponse(tracks=tracks, next_page_token="")

    # ------------------------------------------------------------------
    # RPC: GetUserPlaylists
    # ------------------------------------------------------------------

    def GetUserPlaylists(
        self,
        request: pb.GetUserPlaylistsRequest,
        context: grpc.ServicerContext,
    ) -> pb.GetUserPlaylistsResponse:
        """Return the authenticated user's playlists.

        The *limit* field is advisory; ytmusicapi does not support server-side
        pagination for the playlist list endpoint, so all playlists are
        fetched and truncated client-side.
        """
        if _auth_status() != pb.AUTH_STATUS_AUTHENTICATED:
            context.abort(
                grpc.StatusCode.UNAUTHENTICATED,
                "Authentication required to fetch user playlists",
            )
            return pb.GetUserPlaylistsResponse()

        limit = request.limit if request.limit > 0 else 50
        client = self._get_client()
        try:
            raw_playlists = client.get_library_playlists(limit=limit)
        except Exception as exc:  # noqa: BLE001
            logger.error("GetUserPlaylists error: %s", exc, exc_info=True)
            context.abort(grpc.StatusCode.INTERNAL, f"Failed to fetch playlists: {exc}")
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
                playlists.append(pb.Playlist(
                    id=browse_id,
                    title=title,
                    track_count=count,
                    images=thumbnails,
                ))

        logger.info("GetUserPlaylists: returning %d playlists", len(playlists))
        return pb.GetUserPlaylistsResponse(playlists=playlists, next_page_token="")

    # ------------------------------------------------------------------
    # RPC: GetPlaylistDetails
    # ------------------------------------------------------------------

    def GetPlaylistDetails(
        self,
        request: pb.GetPlaylistDetailsRequest,
        context: grpc.ServicerContext,
    ) -> pb.GetPlaylistDetailsResponse:
        """Return metadata and track listing for a specific playlist.

        The *playlist_id* must be a YouTube Music playlist browse ID or
        a VL-prefixed playlist ID (e.g. 'PLxxxx' or 'VLPLxxxx').
        """
        playlist_id = request.playlist_id.strip()
        if not playlist_id:
            context.abort(grpc.StatusCode.INVALID_ARGUMENT, "playlist_id must not be empty")
            return pb.GetPlaylistDetailsResponse()

        limit = request.limit if request.limit > 0 else 100
        client = self._get_client()
        try:
            raw = client.get_playlist(playlistId=playlist_id, limit=limit)
        except Exception as exc:  # noqa: BLE001
            logger.error("GetPlaylistDetails error for %s: %s", playlist_id, exc, exc_info=True)
            context.abort(grpc.StatusCode.NOT_FOUND, f"Playlist not found or API error: {exc}")
            return pb.GetPlaylistDetailsResponse()

        if not isinstance(raw, dict):
            context.abort(grpc.StatusCode.INTERNAL, "Unexpected API response format")
            return pb.GetPlaylistDetailsResponse()

        # Build Playlist metadata.
        title = raw.get("title") or ""
        description = raw.get("description") or ""
        author = raw.get("author") or {}
        if isinstance(author, dict):
            owner_name = author.get("name") or ""
        else:
            owner_name = str(author)
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

        # Build track list.
        tracks: List[pb.Track] = []
        raw_tracks = raw.get("tracks") or []
        for item in raw_tracks[:limit]:
            if not isinstance(item, dict):
                continue
            # The playlist track dict uses "videoId" for the ID.
            video_id = item.get("videoId") or ""
            if not video_id:
                continue
            tracks.append(_ytm_track_to_pb(item))

        logger.info(
            "GetPlaylistDetails: playlist_id=%s title='%s' tracks=%d",
            playlist_id, title, len(tracks),
        )
        return pb.GetPlaylistDetailsResponse(
            playlist=playlist_pb,
            tracks=tracks,
            next_page_token="",
        )
