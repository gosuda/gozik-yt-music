package main

import (
	"fmt"
	"os"
	"path/filepath"
	"runtime"
)

const targetNodeVersion = "22.14.0"

// setupNode downloads the Node.js standalone binary into ctx.nodeDir.
func setupNode(ctx *installContext, log *guiLogger) error {
	nodeExe := filepath.Join(ctx.nodeDir, "bin", "node")
	if runtime.GOOS == "windows" {
		nodeExe = filepath.Join(ctx.nodeDir, "node.exe")
	}
	if fileExists(nodeExe) {
		log.Log("Node.js already present.")
		return nil
	}

	osName := runtime.GOOS
	arch := runtime.GOARCH
	switch osName {
	case "darwin":
		osName = "darwin"
	case "linux":
		osName = "linux"
	case "windows":
		osName = "win"
	default:
		return fmt.Errorf("unsupported OS for Node.js: %s", osName)
	}
	switch arch {
	case "amd64":
		arch = "x64"
	case "arm64":
		arch = "arm64"
	default:
		return fmt.Errorf("unsupported architecture for Node.js: %s", arch)
	}

	url := fmt.Sprintf("https://nodejs.org/dist/v%s/node-v%s-%s-%s.tar.xz", targetNodeVersion, targetNodeVersion, osName, arch)
	archive := filepath.Join(ctx.installDir, "node.tar.xz")
	if runtime.GOOS == "windows" {
		url = fmt.Sprintf("https://nodejs.org/dist/v%s/node-v%s-%s-%s.zip", targetNodeVersion, targetNodeVersion, osName, arch)
		archive = filepath.Join(ctx.installDir, "node.zip")
	}

	if err := download(url, archive, log); err != nil {
		return fmt.Errorf("download node: %w", err)
	}
	log.Log("Verifying Node.js archive checksum...")
	if err := verifyNodeArchive(archive); err != nil {
		return fmt.Errorf("verify node archive: %w", err)
	}
	if err := extractArchive(archive, ctx.nodeDir, true); err != nil {
		return fmt.Errorf("extract node: %w", err)
	}
	_ = os.Remove(archive)

	if !fileExists(nodeExe) {
		return fmt.Errorf("node binary not found after extraction at %s", nodeExe)
	}
	return nil
}
