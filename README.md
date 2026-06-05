# gozik-yt-music

YouTube Music plugin server for the [gozik](https://github.com/gg582/gozik) desktop music player.

Implemented as a standalone background gRPC daemon that the gozik Go frontend connects to over localhost. Built with [ytmusicapi](https://github.com/sigma67/ytmusicapi) and compiled to a self-contained single-file binary via [Nuitka](https://nuitka.net/) — no Python interpreter required on end-user machines.

---

## Architecture

```
┌─────────────────────────────────┐        gRPC / loopback
│  gozik (Go + GTK3 frontend)     │  ──────────────────────►  ┌──────────────────────────────┐
│  github.com/gg582/gozik         │  127.0.0.1:50051           │  gozik-yt-music (this repo)   │
│                                 │  ◄──────────────────────   │  Python 3 / Nuitka binary     │
└─────────────────────────────────┘                            │  MusicProviderService gRPC    │
                                                               └──────────────────────────────┘
                                                                           │
                                                                    ytmusicapi + yt-dlp
                                                                           │
                                                               ┌──────────────────────────────┐
                                                               │  YouTube Music API            │
                                                               └──────────────────────────────┘
```

The plugin runs as a persistent daemon and is registered as a boot-time service by the platform-specific installer (Windows service via NSSM, macOS LaunchAgent, Linux systemd user unit).

---

## Features

| Capability | RPC method | Notes |
|---|---|---|
| Provider metadata & auth status | `GetProviderMetadata` | Returns `AUTH_STATUS_AUTHENTICATED` if credentials exist |
| OAuth2 device-code login | `InitiateAuth` / `CompleteAuth` | Browser-less flow; device URL displayed in the gozik UI |
| Browser cookie auth | `CompleteAuth` with `cookie_json` | Paste the JSON exported by ytmusicapi's browser auth helper |
| Token refresh | `RefreshAuth` | Transparent refresh of OAuth tokens |
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
│   └── provider.py        # MusicProviderServiceServicer — all 12 RPCs implemented
├── generated/             # Protobuf Python stubs (produced by codegen.sh)
│   ├── __init__.py
│   ├── music_provider_pb2.py
│   ├── music_provider_pb2_grpc.py
│   ├── provider_link_pb2.py
│   └── provider_link_pb2_grpc.py
├── codegen.sh             # Compile .proto → generated/ stubs
├── package.sh             # Nuitka AOT compilation → dist/gozik-yt-music-server
├── requirements.txt       # Pinned Python dependencies
├── build_manifest.json    # Build configuration and post-build metadata tracker
└── .gitignore
```

The proto source files live in the sibling `gozik` repository at `../gozik/api/music/v1/`.

---

## Prerequisites

### Running from source

| Requirement | Version | Notes |
|---|---|---|
| Python | 3.11+ | 3.13 recommended |
| pip | any | Used to install dependencies |
| gozik repository | sibling directory | Required for `codegen.sh` to find `.proto` files |

### Building a release binary

| Requirement | Version | Notes |
|---|---|---|
| Python | 3.13 (`--enable-shared`) | `libpython3.13.so` must exist |
| GCC | 11.0+ | Tested with GCC 14.2.0 |
| patchelf | 0.14+ | RPATH rewriting inside onefile payload (Linux) |
| ccache | any | Optional; speeds up re-builds ~6× |
| Nuitka | 4.1.2 | Installed automatically by `package.sh` if absent |

---

## Installation

### Option A — Pre-built binaries (recommended)

Download the latest release from the [Releases](https://github.com/gg582/gozik/releases) page.

**Linux (AppImage)**

```bash
# Download
curl -fsSL -O https://github.com/gg582/gozik/releases/latest/download/gozik-ytmusic-plugin-linux-amd64-<version>.AppImage
chmod +x gozik-ytmusic-plugin-linux-amd64-<version>.AppImage

# First launch installs the binary and registers the systemd user unit
./gozik-ytmusic-plugin-linux-amd64-<version>.AppImage

# Verify the daemon is running
systemctl --user status gozik-ytmusic.service
```

For ARM64 and RISCV64, replace `amd64` with `arm64` or `riscv64`.

**macOS (DMG)**

1. Open `gozik-ytmusic-plugin-macos-arm64-<version>.dmg`.
2. Drag `gozik YouTube Music Plugin.app` to `/Applications`.
3. Run the registration helper once:

```bash
/Applications/gozik\ YouTube\ Music\ Plugin.app/Contents/MacOS/install-daemon.sh
```

The LaunchAgent (`com.gosuda.gozik.ytmusic.plist`) is copied to `~/Library/LaunchAgents/` and loaded immediately. The daemon will start automatically on every login.

**Windows (Installer)**

Run `gozik-ytmusic-plugin-windows-amd64.exe` as Administrator.

The installer:
- Copies the binary to `%ProgramFiles%\gozik\plugins\ytmusic\`
- Registers `GozikYTMusicPlugin` as a Windows service via NSSM
- Configures `SERVICE_AUTO_START` so the daemon starts on every system boot without requiring a user login
- Starts the service immediately

To uninstall: use **Add or Remove Programs** → the uninstaller stops and removes the service automatically.

---

### Option B — Run from source

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

### Option C — Build a release binary locally

```bash
cd gozik-yt-music
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
bash codegen.sh

# Release build (binary written to dist/gozik-yt-music-server)
bash package.sh

# Debug build — retains intermediate C sources in the .build/ directory
bash package.sh --debug

# Disable ccache
bash package.sh --no-ccache
```

The binary is fully self-contained. Copy `dist/gozik-yt-music-server` to any machine with the same OS/arch and run it directly.

---

## Authentication

The daemon supports two authentication methods. Both persist credentials to `~/.config/gozik/` (or `$XDG_CONFIG_HOME/gozik/` if set).

### Method 1 — OAuth2 device-code flow (recommended)

Trigger from the gozik UI via **Settings → Plugins → YouTube Music → Connect**. The UI calls `InitiateAuth`, receives a verification URL and device code, and displays them. Visit the URL on any device, approve access, then confirm in the gozik UI which calls `CompleteAuth`.

Credentials are saved to `~/.config/gozik/ytmusic_oauth.json`.

### Method 2 — Browser cookie authentication

Follow the [ytmusicapi browser auth guide](https://ytmusicapi.readthedocs.io/en/stable/setup/browser.html) to export your YouTube Music cookies, then pass the resulting JSON to the gozik UI.

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
| `XDG_CONFIG_HOME` | `~/.config` | Base directory for credential storage |

---

## Daemon management

### Linux (systemd)

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

The GitHub Actions workflow at [`.github/workflows/package-plugins.yml`](../.github/workflows/package-plugins.yml) (inside the `gozik` repository) builds release artefacts for all platforms on every `plugin-v*` tag push.

| Platform | Runner | Compilation | Packaging |
|---|---|---|---|
| Linux AMD64 | `ubuntu-latest` | Nuitka native | appimagetool |
| Linux ARM64 | `ubuntu-latest` + QEMU (aarch64) | Nuitka native in container | appimagetool |
| Linux RISCV64 | `ubuntu-latest` + QEMU (riscv64) | Nuitka native in container | appimagetool |
| macOS ARM64 | `macos-latest` (M1) | Nuitka native | create-dmg |
| Windows AMD64 | `windows-latest` | Nuitka native | Inno Setup 6 + NSSM |
| Windows ARM64 | `windows-11-arm` | Nuitka native | Inno Setup 6 + NSSM |

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
| `nuitka` | 4.1.2 | AOT Python → C compilation |

---

## License

Same as the parent [gozik](https://github.com/gg582/gozik) project.
