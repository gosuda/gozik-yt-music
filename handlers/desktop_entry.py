"""
handlers/desktop_entry.py — Cross-platform desktop entry registration.

Registers the built-in web UI as a first-class application in the user's
desktop environment (Linux .desktop, Windows Start Menu .lnk, macOS
~/Applications .app bundle). Works for both installed binaries
(`make install`) and standalone one-file executables (AppImage, Nuitka
onefile, .exe).

Registration is idempotent: if an entry already exists it is skipped unless
*force=True*.
"""

from __future__ import annotations

import logging
import os
import platform
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Optional

logger = logging.getLogger("gozik.ytmusic.desktop_entry")

# ---------------------------------------------------------------------------
# Icon (inline SVG — a simple musical-note glyph)
# ---------------------------------------------------------------------------
_ICON_SVG = """<?xml version="1.0" encoding="UTF-8"?>
<svg xmlns="http://www.w3.org/2000/svg" width="128" height="128" viewBox="0 0 24 24" fill="none" stroke="#ff0033" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
  <circle cx="9" cy="18" r="3"/>
  <circle cx="15" cy="12" r="3"/>
  <path d="M9 18V5l6-2v13"/>
</svg>
"""


def _icon_path() -> Path:
    """Ensure the app icon exists on disk and return its path."""
    icon_dir = (
        Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
        / "gozik"
        / "icons"
    )
    icon_dir.mkdir(parents=True, exist_ok=True)
    p = icon_dir / "gozik-yt-music.svg"
    if not p.exists():
        p.write_text(_ICON_SVG, encoding="utf-8")
    return p


def _executable_path() -> str:
    """Return the absolute path to the running executable.

    For Nuitka onefile builds sys.executable points to the outer stub,
    which is what the desktop entry must invoke.

    When running from source the entry points at the main script so the
    desktop entry is at least functional on a machine with Python installed.
    """
    # Nuitka sets __compiled__ on the *main* module, not on this module.
    main_module = sys.modules.get("__main__")
    if main_module is not None and getattr(main_module, "__compiled__", None) is not None:
        return os.path.abspath(sys.executable)

    # Heuristic: if sys.executable does not look like a Python interpreter,
    # we are almost certainly running as a compiled binary (Nuitka onefile
    # or PyInstaller, etc.).
    exe_name = Path(sys.executable).name.lower()
    if "python" not in exe_name and "pythonw" not in exe_name:
        return os.path.abspath(sys.executable)

    # Running from source: point at the entry-point script so the menu item
    # at least works on machines that have Python installed.
    return os.path.abspath(sys.argv[0])


def _working_dir() -> str:
    """Return the directory containing the executable."""
    return str(Path(_executable_path()).parent)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def register(webui_port: int = 50052, force: bool = False) -> bool:
    """Register a desktop entry for the current platform.

    Returns *True* if a new entry was written, *False* if it already
    existed and *force* was not set.
    """
    system = platform.system()
    logger.info("Registering desktop entry (platform=%s, port=%d, force=%s)", system, webui_port, force)
    if system == "Linux":
        return _register_linux(webui_port, force)
    if system == "Windows":
        return _register_windows(webui_port, force)
    if system == "Darwin":
        return _register_macos(webui_port, force)
    logger.warning("Desktop entry registration not supported on %s", system)
    return False


def unregister() -> bool:
    """Remove the desktop entry for the current platform.

    Returns *True* if an entry was actually removed.
    """
    system = platform.system()
    logger.info("Unregistering desktop entry (platform=%s)", system)
    if system == "Linux":
        return _unregister_linux()
    if system == "Windows":
        return _unregister_windows()
    if system == "Darwin":
        return _unregister_macos()
    return False


# ---------------------------------------------------------------------------
# Linux — freedesktop .desktop file
# ---------------------------------------------------------------------------

def _register_linux(webui_port: int, force: bool) -> bool:
    user_apps = Path.home() / ".local" / "share" / "applications"
    user_apps.mkdir(parents=True, exist_ok=True)
    desktop_file = user_apps / "gozik-yt-music-webui.desktop"

    if desktop_file.exists() and not force:
        logger.debug("Desktop entry already exists: %s", desktop_file)
        return False

    icon = _icon_path()
    exec_path = _executable_path()

    content = f"""[Desktop Entry]
Name=gozik YouTube Music
Comment=YouTube Music plugin web console
Exec={shlex.quote(exec_path)} --web-ui-port {webui_port}
Type=Application
Terminal=false
Icon={icon}
Categories=AudioVideo;Audio;Player;Network;
StartupNotify=true
StartupWMClass=gozik-yt-music
"""
    desktop_file.write_text(content, encoding="utf-8")
    os.chmod(desktop_file, 0o755)
    # Refresh the desktop database so the entry appears immediately.
    subprocess.run(
        ["update-desktop-database", str(user_apps)],
        capture_output=True,
    )
    logger.info("Desktop entry created: %s", desktop_file)
    return True


def _unregister_linux() -> bool:
    desktop_file = Path.home() / ".local" / "share" / "applications" / "gozik-yt-music-webui.desktop"
    removed = False
    if desktop_file.exists():
        desktop_file.unlink()
        removed = True
        logger.info("Removed desktop entry: %s", desktop_file)
    user_apps = Path.home() / ".local" / "share" / "applications"
    subprocess.run(
        ["update-desktop-database", str(user_apps)],
        capture_output=True,
    )
    return removed


def install_system_desktop_entry(prefix: str = "/usr/local", webui_port: int = 50052) -> None:
    """Install a system-wide .desktop entry (called by `make install`)."""
    apps_dir = Path(prefix) / "share" / "applications"
    apps_dir.mkdir(parents=True, exist_ok=True)
    desktop_file = apps_dir / "gozik-yt-music-webui.desktop"

    bindir = Path(prefix) / "bin"
    icon = _icon_path()
    # For system installs we assume the binary lives in $PREFIX/bin.
    exec_path = bindir / "gozik-yt-music-server"

    content = f"""[Desktop Entry]
Name=gozik YouTube Music
Comment=YouTube Music plugin web console
Exec={shlex.quote(str(exec_path))} --web-ui-port {webui_port}
Type=Application
Terminal=false
Icon={icon}
Categories=AudioVideo;Audio;Player;Network;
StartupNotify=true
StartupWMClass=gozik-yt-music
"""
    desktop_file.write_text(content, encoding="utf-8")
    os.chmod(desktop_file, 0o755)
    logger.info("System desktop entry installed: %s", desktop_file)


def uninstall_system_desktop_entry(prefix: str = "/usr/local") -> None:
    desktop_file = Path(prefix) / "share" / "applications" / "gozik-yt-music-webui.desktop"
    if desktop_file.exists():
        desktop_file.unlink()
        logger.info("System desktop entry removed: %s", desktop_file)


# ---------------------------------------------------------------------------
# Windows — Start Menu .lnk via PowerShell
# ---------------------------------------------------------------------------

def _register_windows(webui_port: int, force: bool) -> bool:
    start_menu = (
        Path.home()
        / "AppData"
        / "Roaming"
        / "Microsoft"
        / "Windows"
        / "Start Menu"
        / "Programs"
    )
    start_menu.mkdir(parents=True, exist_ok=True)
    shortcut = start_menu / "gozik YouTube Music.lnk"

    if shortcut.exists() and not force:
        logger.debug("Start Menu shortcut already exists: %s", shortcut)
        return False

    exec_path = _executable_path()
    ps_cmd = (
        f'$WshShell = New-Object -comObject WScript.Shell; '
        f'$Shortcut = $WshShell.CreateShortcut(\"{shortcut}\"); '
        f'$Shortcut.TargetPath = \"{exec_path}\"; '
        f'$Shortcut.Arguments = \"--web-ui-port {webui_port}\"; '
        f'$Shortcut.WorkingDirectory = \"{_working_dir()}\"; '
        f'$Shortcut.Description = \"YouTube Music plugin web console\"; '
        f'$Shortcut.Save()'
    )
    result = subprocess.run(
        ["powershell", "-NoProfile", "-Command", ps_cmd],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        logger.error("Failed to create Windows shortcut: %s", result.stderr.strip())
        return False
    logger.info("Start Menu shortcut created: %s", shortcut)
    return True


def _unregister_windows() -> bool:
    shortcut = (
        Path.home()
        / "AppData"
        / "Roaming"
        / "Microsoft"
        / "Windows"
        / "Start Menu"
        / "Programs"
        / "gozik YouTube Music.lnk"
    )
    if shortcut.exists():
        shortcut.unlink()
        logger.info("Removed Start Menu shortcut: %s", shortcut)
        return True
    return False


# ---------------------------------------------------------------------------
# macOS — ~/Applications helper .app bundle
# ---------------------------------------------------------------------------

def _register_macos(webui_port: int, force: bool) -> bool:
    app_dir = Path.home() / "Applications" / "gozik-yt-music-webui.app"
    if app_dir.exists() and not force:
        logger.debug("macOS .app bundle already exists: %s", app_dir)
        return False

    contents = app_dir / "Contents"
    macos_dir = contents / "MacOS"
    macos_dir.mkdir(parents=True, exist_ok=True)

    exec_path = _executable_path()
    launcher = macos_dir / "gozik-yt-music-webui"
    launcher.write_text(
        f'#!/bin/bash\nexec "{exec_path}" --web-ui-port {webui_port}\n',
        encoding="utf-8",
    )
    os.chmod(launcher, 0o755)

    plist = contents / "Info.plist"
    plist.write_text(
        """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleExecutable</key>
  <string>gozik-yt-music-webui</string>
  <key>CFBundleIdentifier</key>
  <string>com.gosuda.gozik.ytmusic.webui</string>
  <key>CFBundleName</key>
  <string>gozik YouTube Music</string>
  <key>CFBundleVersion</key>
  <string>1.0</string>
</dict>
</plist>
""",
        encoding="utf-8",
    )
    logger.info("macOS .app bundle created: %s", app_dir)
    return True


def _unregister_macos() -> bool:
    app_dir = Path.home() / "Applications" / "gozik-yt-music-webui.app"
    if app_dir.exists():
        import shutil

        shutil.rmtree(app_dir)
        logger.info("Removed macOS .app bundle: %s", app_dir)
        return True
    return False
