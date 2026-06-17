package main

import (
	"fmt"
	"os/exec"
	"path/filepath"
	"runtime"
)

// registerAutostart registers a user-level autostart entry for the current
// platform and returns an error if registration fails.
func registerAutostart(ctx *installContext, log *guiLogger) error {
	switch runtime.GOOS {
	case "linux":
		return registerLinuxAutostart(ctx, log)
	case "darwin":
		return registerDarwinAutostart(ctx, log)
	case "windows":
		return registerWindowsAutostart(ctx, log)
	default:
		return fmt.Errorf("autostart not supported on %s", runtime.GOOS)
	}
}

// createShortcut creates a platform-appropriate application menu shortcut.
func createShortcut(ctx *installContext, log *guiLogger) error {
	switch runtime.GOOS {
	case "linux":
		return createLinuxShortcut(ctx, log)
	case "darwin":
		return createDarwinShortcut(ctx, log)
	case "windows":
		return createWindowsShortcut(ctx, log)
	default:
		log.Logf("shortcut not supported on %s", runtime.GOOS)
		return nil
	}
}

// startService attempts to start the installed service immediately.
func startService(ctx *installContext, log *guiLogger) error {
	binary := filepath.Join(ctx.bundleDir, "gozik-yt-music-server")
	if runtime.GOOS == "windows" {
		binary += ".exe"
	}
	switch runtime.GOOS {
	case "linux":
		cmd := exec.Command("systemctl", "--user", "start", "gozik-yt-music-server.service")
		if out, err := cmd.CombinedOutput(); err != nil {
			// Fallback: launch binary directly.
			_ = exec.Command(binary).Start()
			log.Logf("systemctl start failed, launched directly: %s", string(out))
		}
		return nil
	case "darwin":
		cmd := exec.Command("launchctl", "start", "com.gosuda.gozik.ytmusic")
		_ = cmd.Run()
		return nil
	case "windows":
		cmd := exec.Command("schtasks", "/Run", "/Tn", "gozik-yt-music-server")
		_ = cmd.Run()
		return nil
	default:
		return nil
	}
}
