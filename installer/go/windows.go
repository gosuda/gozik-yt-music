package main

import (
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
)

func registerWindowsAutostart(ctx *installContext, log *guiLogger) error {
	binary := filepath.Join(ctx.bundleDir, "gozik-yt-music-server.exe")
	quotedBinary := strings.ReplaceAll(binary, "\\", "\\\\")

	// Create a user-level scheduled task that runs at logon.
	ps := fmt.Sprintf(
		`$action = New-ScheduledTaskAction -Execute '%s'; `+
			`$trigger = New-ScheduledTaskTrigger -AtLogon; `+
			`$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable; `+
			`$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -RunLevel Highest; `+
			`$task = New-ScheduledTask -Action $action -Trigger $trigger -Settings $settings -Principal $principal; `+
			`Register-ScheduledTask -TaskName 'gozik-yt-music-server' -InputObject $task -Force`,
		quotedBinary,
	)
	cmd := exec.Command("powershell", "-NoProfile", "-Command", ps)
	if out, err := cmd.CombinedOutput(); err != nil {
		// Fallback to startup folder shortcut.
		log.Logf("Scheduled task registration failed, using Startup folder: %s", string(out))
		return createWindowsStartupShortcut(ctx)
	}
	return nil
}

func createWindowsStartupShortcut(ctx *installContext) error {
	startup := filepath.Join(os.Getenv("APPDATA"), "Microsoft", "Windows", "Start Menu", "Programs", "Startup")
	if err := os.MkdirAll(startup, 0o755); err != nil {
		return err
	}
	binary := filepath.Join(ctx.bundleDir, "gozik-yt-music-server.exe")
	shortcut := filepath.Join(startup, "gozik-yt-music-server.lnk")
	ps := fmt.Sprintf(
		`$WshShell = New-Object -comObject WScript.Shell; `+
			`$Shortcut = $WshShell.CreateShortcut('%s'); `+
			`$Shortcut.TargetPath = '%s'; `+
			`$Shortcut.WorkingDirectory = '%s'; `+
			`$Shortcut.Save()`,
		shortcut, binary, ctx.bundleDir,
	)
	cmd := exec.Command("powershell", "-NoProfile", "-Command", ps)
	if out, err := cmd.CombinedOutput(); err != nil {
		return fmt.Errorf("create startup shortcut: %w\n%s", err, string(out))
	}
	return nil
}

func createWindowsShortcut(ctx *installContext, log *guiLogger) error {
	programs := filepath.Join(os.Getenv("APPDATA"), "Microsoft", "Windows", "Start Menu", "Programs", "gozik")
	if err := os.MkdirAll(programs, 0o755); err != nil {
		return err
	}
	binary := filepath.Join(ctx.bundleDir, "gozik-yt-music-server.exe")
	shortcut := filepath.Join(programs, "gozik YouTube Music.lnk")
	ps := fmt.Sprintf(
		`$WshShell = New-Object -comObject WScript.Shell; `+
			`$Shortcut = $WshShell.CreateShortcut('%s'); `+
			`$Shortcut.TargetPath = '%s'; `+
			`$Shortcut.Arguments = '--web-ui-port 50052'; `+
			`$Shortcut.WorkingDirectory = '%s'; `+
			`$Shortcut.Description = 'YouTube Music plugin web console'; `+
			`$Shortcut.Save()`,
		shortcut, binary, ctx.bundleDir,
	)
	cmd := exec.Command("powershell", "-NoProfile", "-Command", ps)
	if out, err := cmd.CombinedOutput(); err != nil {
		return fmt.Errorf("create start menu shortcut: %w\n%s", err, string(out))
	}
	return nil
}
