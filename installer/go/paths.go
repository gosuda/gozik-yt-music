package main

import (
	"os"
	"path/filepath"
	"runtime"
)

// userDataDir returns a platform-appropriate user-local data directory.
func userDataDir(home string) string {
	switch runtime.GOOS {
	case "windows":
		if local := os.Getenv("LOCALAPPDATA"); local != "" {
			return local
		}
		return filepath.Join(home, "AppData", "Local")
	case "darwin":
		return filepath.Join(home, "Library", "Application Support")
	default:
		return filepath.Join(home, ".local", "share")
	}
}
