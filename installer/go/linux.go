package main

import (
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
)

func registerLinuxAutostart(ctx *installContext, log *guiLogger) error {
	unitDir := filepath.Join(os.Getenv("HOME"), ".config", "systemd", "user")
	if err := os.MkdirAll(unitDir, 0o755); err != nil {
		return err
	}
	binary := filepath.Join(ctx.bundleDir, "gozik-yt-music-server")
	unit := `[Unit]
Description=gozik YouTube Music gRPC plugin server
After=network.target

[Service]
Type=simple
ExecStart=` + binary + `
Restart=on-failure
RestartSec=5
StandardOutput=journal
StandardError=journal
PassEnvironment=DISPLAY XAUTHORITY WAYLAND_DISPLAY

[Install]
WantedBy=default.target
`
	unitPath := filepath.Join(unitDir, "gozik-yt-music-server.service")
	if err := os.WriteFile(unitPath, []byte(unit), 0o644); err != nil {
		return err
	}

	// Reload daemon and enable unit. If systemctl is missing, just write the file.
	if _, err := exec.LookPath("systemctl"); err != nil {
		log.Log("systemctl not found; unit file written but not enabled.")
		return nil
	}
	if out, err := exec.Command("systemctl", "--user", "daemon-reload").CombinedOutput(); err != nil {
		return fmt.Errorf("daemon-reload: %w\n%s", err, string(out))
	}
	if out, err := exec.Command("systemctl", "--user", "enable", "gozik-yt-music-server.service").CombinedOutput(); err != nil {
		return fmt.Errorf("enable unit: %w\n%s", err, string(out))
	}
	return nil
}

func createLinuxShortcut(ctx *installContext, log *guiLogger) error {
	appsDir := filepath.Join(os.Getenv("HOME"), ".local", "share", "applications")
	if err := os.MkdirAll(appsDir, 0o755); err != nil {
		return err
	}
	binary := filepath.Join(ctx.bundleDir, "gozik-yt-music-server")
	icon := filepath.Join(ctx.srcDir, "assets", "gozik-yt-music.svg")
	desktop := `[Desktop Entry]
Name=gozik YouTube Music
Comment=YouTube Music plugin web console
Exec=` + binary + ` --web-ui-port 50052
Type=Application
Terminal=false
Icon=` + icon + `
Categories=AudioVideo;Audio;Player;Network;
StartupNotify=true
StartupWMClass=gozik-yt-music
`
	path := filepath.Join(appsDir, "gozik-yt-music-webui.desktop")
	if err := os.WriteFile(path, []byte(desktop), 0o755); err != nil {
		return err
	}
	_ = exec.Command("update-desktop-database", appsDir).Run()
	return nil
}
