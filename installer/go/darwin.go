package main

import (
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
)

func registerDarwinAutostart(ctx *installContext, log *guiLogger) error {
	launchDir := filepath.Join(os.Getenv("HOME"), "Library", "LaunchAgents")
	if err := os.MkdirAll(launchDir, 0o755); err != nil {
		return err
	}
	binary := filepath.Join(ctx.bundleDir, "gozik-yt-music-server")
	plist := `<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>com.gosuda.gozik.ytmusic</string>
  <key>ProgramArguments</key>
  <array>
    <string>` + binary + `</string>
  </array>
  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <true/>
  <key>StandardOutPath</key>
  <string>/tmp/gozik-ytmusic.log</string>
  <key>StandardErrorPath</key>
  <string>/tmp/gozik-ytmusic.log</string>
</dict>
</plist>
`
	plistPath := filepath.Join(launchDir, "com.gosuda.gozik.ytmusic.plist")
	if err := os.WriteFile(plistPath, []byte(plist), 0o644); err != nil {
		return err
	}
	if out, err := exec.Command("launchctl", "load", "-w", plistPath).CombinedOutput(); err != nil {
		return fmt.Errorf("launchctl load: %w\n%s", err, string(out))
	}
	return nil
}

func createDarwinShortcut(ctx *installContext, log *guiLogger) error {
	appDir := filepath.Join(os.Getenv("HOME"), "Applications", "gozik-yt-music-webui.app")
	if err := os.MkdirAll(filepath.Join(appDir, "Contents", "MacOS"), 0o755); err != nil {
		return err
	}
	binary := filepath.Join(ctx.bundleDir, "gozik-yt-music-server")
	launcher := filepath.Join(appDir, "Contents", "MacOS", "gozik-yt-music-webui")
	content := "#!/bin/bash\nexec \"" + binary + "\" --web-ui-port 50052\n"
	if err := os.WriteFile(launcher, []byte(content), 0o755); err != nil {
		return err
	}
	plist := `<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
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
`
	return os.WriteFile(filepath.Join(appDir, "Contents", "Info.plist"), []byte(plist), 0o644)
}
