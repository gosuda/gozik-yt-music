# gozik-yt-music

YouTube Music plugin server for the [gozik](https://github.com/gg582/gozik) desktop music player.

Implemented as a standalone background gRPC daemon that the gozik Go frontend connects to over localhost. Built with [ytmusicapi](https://github.com/sigma67/ytmusicapi) and PyInstaller

---

## Architecture

```
┌─────────────────────────────────┐        gRPC / loopback
│  gozik (Go + GTK3 frontend)     │  ──────────────────────►  ┌──────────────────────────────┐
│  github.com/gosuda/gozik        │  127.0.0.1:50051          │  gozik-yt-music (this repo)  │
│                                 │  ◄──────────────────────  │  Python 3 + Node.js          │
└─────────────────────────────────┘                           │  MusicProviderService gRPC   │
                               │                              └──────────────────────────────┘
                               │                                           │
                               │                                    ytmusicapi + yt-dlp
                               │                                           │
                               │           ┌──────────────────────────────┐
                               └──────────►│  YouTube Music API           │
                               HTTP        └──────────────────────────────┘
                               127.0.0.1:50052
                          (built-in web UI)
```

The plugin runs as a persistent daemon and is registered as a boot-time service by the platform-specific installer (Windows service via NSSM, macOS LaunchAgent, Linux systemd user unit).

---

## Features

| Capability | RPC method | Notes |
|---|---|---|
| Provider metadata & auth status | `GetProviderMetadata` | Returns `AUTH_STATUS_AUTHENTICATED` if credentials exist |
| OAuth2 device-code login | `InitiateAuth` / `CompleteAuth` | Browser-less flow; device URL displayed in the gozik UI or built-in web UI |
| Browser cookie auth | `CompleteAuth` with `cookie_json` | Paste the JSON exported by ytmusicapi's browser auth helper |
| Token refresh | `RefreshAuth` | Transparent refresh of OAuth tokens |
| Built-in web UI | `http.server` on port 50052 | Dark-themed dashboard for auth, status, and logout without the gozik app |
| Desktop entry auto-registration | `handlers/desktop_entry.py` | Creates an app-menu shortcut on first startup or first successful auth |
| Search | `Search` | Songs, albums, artists, playlists |
| Search | `Search` | Songs, albums, artists, playlists |
| Autocomplete | `SearchSuggestions` (streaming) | Real-time suggestions as the user types |
| Track details | `GetTrackDetails` | Full metadata for a single video ID |
| Stream URL resolution | `ResolveStream` | Returns a pre-signed audio URL + required headers via yt-dlp |
| Binary audio streaming | `StreamAudio` (streaming) | Pipes raw audio bytes over gRPC (alternative to URL resolution) |
| Liked songs library | `GetUserLibrary` | Requires authentication |
| Playlist list | `GetUserPlaylists` | Requires authentication |
| Playlist contents | `GetPlaylistDetails` | Paginated track list |

---

## Repository layout

```
gozik-yt-music/
├── server.py              # gRPC server entrypoint (bind 127.0.0.1:50051)
├── handlers/
│   ├── provider.py        # MusicProviderServiceServicer — all 12 RPCs implemented
│   ├── webui.py           # Stand-alone HTTP dashboard (port 50052, stdlib only)
│   └── desktop_entry.py   # Cross-platform app-menu registration (Linux/Windows/macOS)
├── assets/                # Desktop icon (SVG)
├── generated/             # Protobuf Python stubs (produced by codegen.sh)
│   ├── __init__.py
│   ├── music_provider_pb2.py
│   ├── music_provider_pb2_grpc.py
│   ├── provider_link_pb2.py
│   └── provider_link_pb2_grpc.py
├── codegen.sh             # Compile .proto → generated/ stubs
├── package.sh             # PyInstaller onedir build → dist/gozik-yt-music-server
├── requirements.txt       # Pinned Python dependencies
├── build_manifest.json    # Build configuration and post-build metadata tracker
└── .gitignore
```

The proto source files live in the sibling `gozik` repository at `../gozik/api/music/v1/`.

---

## Prerequisites

### Running from source

```bash
make
# make dev for running development server
make install-user
```

---

## Installation

### Run from source

```bash
# 1. Clone both repositories as siblings
git clone https://github.com/gg582/gozik.git
git clone https://github.com/gg582/gozik-yt-music.git

# Directory layout must be:
#   workspace/
#   ├── gozik/
#   └── gozik-yt-music/

# 2. Create a virtual environment
cd gozik-yt-music
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate

# 3. Install dependencies
pip install -r requirements.txt

# 4. Generate protobuf stubs
bash codegen.sh

# 5. Start the server
python3 server.py
# Options:
#   --host HOST      Bind address (default: 127.0.0.1)
#   --port PORT      Bind port    (default: 50051)
#   --workers N      gRPC thread-pool size (default: 4)
```

---

### Option B — Install prebuilt release (user-only)

No root access required. The extracted bundle runs in-place from your home directory.

1. Download the release archive matching your platform from the [Releases](https://github.com/gg582/gozik-yt-music/releases) page.
2. Extract the archive:

```bash
tar xf <release-archive> -C ~/.local/share
```

3. Create a user systemd unit:

```bash
mkdir -p ~/.config/systemd/user
cat > ~/.config/systemd/user/gozik-yt-music-server.service <<'EOF'
[Unit]
Description=gozik YouTube Music gRPC plugin server
After=network.target

[Service]
Type=simple
ExecStart=%h/.local/share/gozik-yt-music-server/gozik-yt-music-server
Restart=on-failure
RestartSec=5
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=default.target
EOF
```

4. Enable and start the service:

```bash
systemctl --user daemon-reload
systemctl --user enable --now gozik-yt-music-server
```

**Status & logs**

```bash
systemctl --user status gozik-yt-music-server
journalctl --user -u gozik-yt-music-server -f
```

**Uninstall**

```bash
systemctl --user stop gozik-yt-music-server
systemctl --user disable gozik-yt-music-server
rm ~/.config/systemd/user/gozik-yt-music-server.service
systemctl --user daemon-reload
rm -rf ~/.local/share/gozik-yt-music-server
```

---

### Option C — Build a release binary locally

**Quick install (Makefile)** — build and install in one go:

```bash
cd gozik-yt-music
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
bash codegen.sh

# Build + install + start daemon (all at once)
make && make install-user && sudo systemctl enable --user --now gozik-yt-music-server
```

Or step by step:

```bash
make              # build the release binary
sudo make install # install binary + systemd unit
sudo systemctl enable --now gozik-yt-music-server
```

**Manual build** (same as `make`, but invoked directly):

```bash
cd gozik-yt-music
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
bash codegen.sh

# Release build (binary written to dist/gozik-yt-music-server)
bash package.sh

# Debug build — PyInstaller debug traces enabled
bash package.sh --debug
```

**Makefile targets**

| Target | Description |
|---|---|
| `make` or `make all` | Build the release binary (`dist/gozik-yt-music-server`) |
| `make install` | Install binary, systemd unit, **desktop entry**, and icon system-wide |
| `sudo make uninstall` | Remove the installed binary, systemd unit, desktop entry, and icon |
| `make clean` | Remove `dist/`, build scratch directories, and `__pycache__` |
| `make codegen` | Regenerate protobuf Python stubs |

**Install prefix**

Default is `/usr/local`. Override with:

```bash
make PREFIX=/opt
sudo make PREFIX=/opt install
```

**DESTDIR support**

Useful for distro packaging or staged installs:

```bash
sudo make DESTDIR=/tmp/stage install
# creates /tmp/stage/usr/local/bin/gozik-yt-music-server
# and /tmp/stage/etc/systemd/system/gozik-yt-music-server.service
# and /tmp/stage/usr/local/share/applications/gozik-yt-music-webui.desktop
```

**Safety check**

`make install` refuses to run if the binary has not been built yet (prevents accidental rebuild under `sudo`).

```bash
sudo make install
# Binary not found: dist/gozik-yt-music-server. Run 'make' first.
```

**Uninstall**

```bash
sudo make uninstall
```

The binary is fully self-contained. Copy `dist/gozik-yt-music-server` to any machine with the same OS/arch and run it directly.

---

## Authentication

The daemon supports two authentication methods. Both persist credentials to `~/.config/gozik/` (or `$XDG_CONFIG_HOME/gozik/` if set).

### Method 1 — OAuth2 device-code flow (recommended)

**Via gozik desktop UI:**

Trigger from the gozik UI via **Settings → Plugins → YouTube Music → Connect**. The UI calls `InitiateAuth`, receives a verification URL and device code, and displays them. Visit the URL on any device, approve access, then confirm in the gozik UI which calls `CompleteAuth`.

**Via built-in web UI (no gozik app required):**

1. Open [http://127.0.0.1:50052](http://127.0.0.1:50052) in your browser.
2. Click **Authenticate** → **Start OAuth**.
3. Open the verification URL in any browser, enter the device code, and approve access.
4. Return to the web UI and click **Complete**.

Credentials are saved to `~/.config/gozik/ytmusic_oauth.json`.

### Method 2 — Browser cookie authentication

Follow the [ytmusicapi browser auth guide](https://ytmusicapi.readthedocs.io/en/stable/setup/browser.html) to export your YouTube Music cookies.

**Via gozik desktop UI:** pass the resulting JSON to the gozik UI.

**Via built-in web UI:** open the web console, go to **Authenticate**, paste the JSON into the **Browser Cookie Login** textarea, and click **Save Cookie**.

Credentials are saved to `~/.config/gozik/ytmusic_auth.json`.

### Auth status

`GetProviderMetadata` reports the current status:

| Status | Meaning |
|---|---|
| `AUTH_STATUS_UNAUTHENTICATED` | No credential files found |
| `AUTH_STATUS_AUTHENTICATED` | Valid credential file present |
| `AUTH_STATUS_EXPIRED` | Credentials present but expired (trigger `RefreshAuth`) |

---

## Environment variables

| Variable | Default | Description |
|---|---|---|
| `GOZIK_YTM_HOST` | `127.0.0.1` | Bind host |
| `GOZIK_YTM_PORT` | `50051` | Bind port |
| `GOZIK_YTM_WORKERS` | `4` | gRPC thread-pool size |
| `GOZIK_YTM_WEBUI_PORT` | `50052` | Web UI HTTP port (set to `0` to disable) |
| `GOZIK_YTM_REGISTER_DESKTOP` | `auto` | Desktop entry behaviour (`auto`/`always`/`never`) |
| `XDG_CONFIG_HOME` | `~/.config` | Base directory for credential storage |

---

## Daemon management

### Web UI

When the server is running, open [http://127.0.0.1:50052](http://127.0.0.1:50052) in your browser.

| Page | Path | Description |
|---|---|---|
| Dashboard | `/` | Auth status, capabilities, and logout |
| Authenticate | `/login` | OAuth device-code flow and cookie login |
| Status JSON | `/api/status` | Machine-readable provider metadata |

The web UI is served only on loopback (`127.0.0.1`) and requires no extra dependencies.

#### Desktop entry (app-menu shortcut)

The server can automatically register itself in the desktop environment's app menu:

- **Linux**: creates `~/.local/share/applications/gozik-yt-music-webui.desktop`
- **Windows**: creates a Start Menu shortcut via PowerShell
- **macOS**: creates `~/Applications/gozik-yt-music-webui.app`

Registration happens on **first server startup** (if `--register-desktop-entry=auto`) and again on **first successful authentication** via the web UI, so standalone binaries (AppImage, `.exe`, Nuitka onefile) that are not installed via a package manager still get a menu entry without manual setup.

| Flag | Behaviour |
|---|---|
| `--register-desktop-entry auto` | Register once if missing (default) |
| `--register-desktop-entry always` | Always overwrite existing entry |
| `--register-desktop-entry never` | Skip registration entirely |

When using `make install`, the desktop entry and icon are installed system-wide under `$PREFIX/share/applications/` and `$PREFIX/share/icons/hicolor/scalable/apps/` instead.

### Linux (systemd)

When installed via `sudo make install` (system-wide service):

```bash
# Status
sudo systemctl status gozik-yt-music-server

# Restart
sudo systemctl restart gozik-yt-music-server

# Stop
sudo systemctl stop gozik-yt-music-server

# Disable autostart
sudo systemctl disable gozik-yt-music-server

# View logs
sudo journalctl -u gozik-yt-music-server -f
```

When using the AppImage / per-user installer (`--user` service):

```bash
# Status
systemctl --user status gozik-ytmusic.service

# Restart
systemctl --user restart gozik-ytmusic.service

# Stop
systemctl --user stop gozik-ytmusic.service

# Disable autostart
systemctl --user disable gozik-ytmusic.service

# View logs
journalctl --user -u gozik-ytmusic.service -f
```

### macOS (launchd)

```bash
# Stop
launchctl unload ~/Library/LaunchAgents/com.gosuda.gozik.ytmusic.plist

# Start
launchctl load -w ~/Library/LaunchAgents/com.gosuda.gozik.ytmusic.plist

# Uninstall
/Applications/gozik\ YouTube\ Music\ Plugin.app/Contents/MacOS/uninstall-daemon.sh

# View logs
tail -f /tmp/gozik-ytmusic.log
```

### Windows (Services)

```powershell
# Status
Get-Service GozikYTMusicPlugin

# Restart
Restart-Service GozikYTMusicPlugin

# Stop
Stop-Service GozikYTMusicPlugin

# View logs (Event Viewer)
Get-EventLog -LogName Application -Source GozikYTMusicPlugin -Newest 50
```

---

## Proto definitions

The gRPC interface is defined in [`../gozik/api/music/v1/music_provider.proto`](../gozik/api/music/v1/music_provider.proto).

Regenerate Python stubs after any proto change:

```bash
bash codegen.sh
```

The script reads protos from `../gozik/api/music/v1/`, writes stubs to `generated/`, and fixes the relative import paths in the generated `*_pb2_grpc.py` files.

---

## CI / Packaging workflow

The GitHub Actions workflow at `.github/workflows/build.yml` builds release artefacts for all platforms on every `plugin-v*` tag push.

| Platform | Runner | Build tool | Package format |
|---|---|---|---|
| Linux AMD64 | `ubuntu-latest` | PyInstaller | `.tar.gz` |
| Linux ARM64 | `ubuntu-latest` + QEMU (aarch64) | PyInstaller in container | `.tar.gz` |
| Linux RISCV64 | `ubuntu-latest` + QEMU (riscv64) | PyInstaller in container | `.tar.gz` |
| macOS ARM64 | `macos-latest` (M1) | PyInstaller | `.tar.gz` |
| Windows AMD64 | `windows-latest` | PyInstaller | `.zip` |
| Windows ARM64 | — (use AMD64 build) | — | `.zip` (x64 emulation) |

To publish a release:

```bash
git tag plugin-v1.0.0
git push origin plugin-v1.0.0
```

---

## Dependency versions

| Package | Pinned version | Purpose |
|---|---|---|
| `grpcio` | 1.81.0 | gRPC server runtime |
| `grpcio-tools` | 1.81.0 | Proto compilation (`codegen.sh`) |
| `ytmusicapi` | 1.10.2 | YouTube Music API client |
| `yt-dlp` | 2025.1.26 | Audio stream URL extraction |
| `pyinstaller` | 6.x | Python application bundler |

---

## License

Same as the parent [gozik](https://github.com/gg582/gozik) project.
