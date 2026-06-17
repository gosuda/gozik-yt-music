# gozik YouTube Music Installer

Cross-platform GUI installer for `gozik-yt-music`. Built with Go + Fyne.

## What the installer does

1. Shows a wizard: welcome → license → options → progress → finish.
2. Downloads a portable Python 3.12 runtime (Windows/macOS) or uses the system Python 3.11+ (Linux).
3. Downloads the source tarball for the selected release tag from GitHub.
4. Creates a Python venv and installs dependencies + PyInstaller.
5. Downloads a Node.js binary for the current platform.
6. Builds the `gozik-yt-music-server` bundle locally with PyInstaller.
7. Installs the bundle under the user's profile.
8. Registers autostart:
   - Linux: systemd user unit
   - macOS: LaunchAgent
   - Windows: Scheduled task (with Startup folder fallback)
9. Creates an application menu shortcut and optionally starts the service.

## Building the installer

```bash
cd installer/go

# Download dependencies
go mod tidy

# Build for current platform
go build -o gozik-yt-music-installer .

# Cross-compile examples:
GOOS=windows GOARCH=amd64 go build -o gozik-yt-music-installer-windows-amd64.exe .
GOOS=darwin  GOARCH=arm64 go build -o gozik-yt-music-installer-darwin-arm64 .
GOOS=linux   GOARCH=amd64 go build -o gozik-yt-music-installer-linux-amd64 .
GOOS=linux   GOARCH=arm64 go build -o gozik-yt-music-installer-linux-arm64 .
```

> Fyne requires CGO on some platforms. For macOS and Linux builds, set
> `CGO_ENABLED=1` and ensure a C toolchain is available. Windows builds can be
> cross-compiled from Linux using a MinGW toolchain.

## Running the installer

```bash
# Install latest release
gozik-yt-music-installer

# Install specific tag
gozik-yt-music-installer --tag v1.2.3
```

## Implementation notes

- The installer does **not** require administrator privileges; everything is installed under the current user profile.
- The installation directory can be changed on the options screen.
- Downloads are retried automatically, and Node.js archives are verified against the official `SHASUMS256.txt`.
- Shortcut creation and starting the service are best-effort; if they fail, the installation still succeeds and the failure is shown as a warning.
- On Linux, the installer relies on the distribution's Python 3.11+ because python.org does not provide portable Linux binaries.
- The embedded Windows Python distribution enables `import site` and bootstraps `pip` automatically.
- macOS Python is extracted from the official `.pkg` using `pkgutil` and `tar`, also without admin rights.
