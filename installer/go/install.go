package main

import (
	"fmt"
	"os"
	"path/filepath"
	"runtime"
)

// installBundle copies the PyInstaller output into the installation directory
// and creates a user-facing wrapper/shortcut.
func installBundle(ctx *installContext, log *guiLogger) error {
	srcBundle := filepath.Join(ctx.srcDir, "dist", "gozik-yt-music-server")
	if !fileExists(srcBundle) {
		return fmt.Errorf("built bundle not found at %s", srcBundle)
	}

	// Remove any previous bundle.
	_ = os.RemoveAll(ctx.bundleDir)
	if err := copyDir(srcBundle, ctx.bundleDir); err != nil {
		return fmt.Errorf("copy bundle: %w", err)
	}

	binary := filepath.Join(ctx.bundleDir, "gozik-yt-music-server")
	if runtime.GOOS == "windows" {
		binary = filepath.Join(ctx.bundleDir, "gozik-yt-music-server.exe")
	}
	if !fileExists(binary) {
		return fmt.Errorf("bundle binary not found at %s", binary)
	}

	// Ensure executable bit on Unix.
	if runtime.GOOS != "windows" {
		_ = os.Chmod(binary, 0o755)
	}

	// Create wrapper.
	if err := createWrapper(ctx, binary); err != nil {
		return fmt.Errorf("create wrapper: %w", err)
	}

	return nil
}

// copyDir recursively copies src to dst.
func copyDir(src, dst string) error {
	info, err := os.Stat(src)
	if err != nil {
		return err
	}
	if err := os.MkdirAll(dst, info.Mode()); err != nil {
		return err
	}

	entries, err := os.ReadDir(src)
	if err != nil {
		return err
	}
	for _, entry := range entries {
		srcPath := filepath.Join(src, entry.Name())
		dstPath := filepath.Join(dst, entry.Name())
		if entry.IsDir() {
			if err := copyDir(srcPath, dstPath); err != nil {
				return err
			}
		} else {
			if err := copyFile(srcPath, dstPath); err != nil {
				return err
			}
		}
	}
	return nil
}

// createWrapper creates a user-local launcher.
func createWrapper(ctx *installContext, binary string) error {
	switch runtime.GOOS {
	case "windows":
		return createWindowsWrapper(ctx, binary)
	case "darwin":
		return createDarwinWrapper(ctx, binary)
	default: // Linux and other Unix
		return createLinuxWrapper(ctx, binary)
	}
}

// createLinuxWrapper writes a shell wrapper into ~/.local/bin.
func createLinuxWrapper(ctx *installContext, binary string) error {
	binDir := filepath.Join(userHome(), ".local", "bin")
	if err := os.MkdirAll(binDir, 0o755); err != nil {
		return err
	}
	wrapper := filepath.Join(binDir, "gozik-yt-music-server")
	content := fmt.Sprintf("#!/bin/sh\nexec %q \"$@\"\n", binary)
	if err := os.WriteFile(wrapper, []byte(content), 0o755); err != nil {
		return err
	}
	return nil
}

// createDarwinWrapper is the same as Linux on macOS.
func createDarwinWrapper(ctx *installContext, binary string) error {
	return createLinuxWrapper(ctx, binary)
}

// createWindowsWrapper writes a small .cmd launcher.
func createWindowsWrapper(ctx *installContext, binary string) error {
	binDir := filepath.Join(os.Getenv("USERPROFILE"), "AppData", "Roaming", "Microsoft", "Windows", "Start Menu", "Programs", "gozik")
	if err := os.MkdirAll(binDir, 0o755); err != nil {
		return err
	}
	wrapper := filepath.Join(binDir, "gozik-yt-music-server.cmd")
	content := fmt.Sprintf("@echo off\n\"%s\" %%*\n", binary)
	return os.WriteFile(wrapper, []byte(content), 0o644)
}

func userHome() string {
	home, _ := os.UserHomeDir()
	return home
}
