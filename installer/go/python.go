package main

import (
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"strings"
)

const targetPythonVersion = "3.12.8"

// setupPython returns the absolute path to a working Python interpreter. On
// Linux it prefers the system Python; on Windows and macOS it downloads the
// official portable/installable package into the install directory.
func setupPython(ctx *installContext) (string, string, error) {
	switch runtime.GOOS {
	case "windows":
		return setupWindowsPython(ctx)
	case "darwin":
		return setupDarwinPython(ctx)
	case "linux":
		return setupLinuxPython(ctx)
	default:
		return "", "", fmt.Errorf("unsupported OS: %s", runtime.GOOS)
	}
}

// setupWindowsPython downloads the python.org embeddable zip and creates a
// small sitecustomize.py so that pip works.
func setupWindowsPython(ctx *installContext) (string, string, error) {
	pyDir := filepath.Join(ctx.installDir, ".python")
	pyExe := filepath.Join(pyDir, "python.exe")
	if fileExists(pyExe) {
		ver, err := pythonVersion(pyExe)
		return pyExe, ver, err
	}

	arch := pythonArch()
	url := fmt.Sprintf("https://www.python.org/ftp/python/%s/python-%s-embed-%s.zip", targetPythonVersion, targetPythonVersion, arch)
	zipPath := filepath.Join(ctx.installDir, "python-embed.zip")
	if err := download(url, zipPath, nil); err != nil {
		return "", "", fmt.Errorf("download embeddable python: %w", err)
	}
	if err := extractZip(zipPath, pyDir, false); err != nil {
		return "", "", fmt.Errorf("extract embeddable python: %w", err)
	}
	_ = os.Remove(zipPath)

	// The embeddable distribution disables import of site by default. Enable it
	// so that pip and venv work.
	pthPath := filepath.Join(pyDir, "python312._pth")
	if fileExists(pthPath) {
		data, err := os.ReadFile(pthPath)
		if err == nil {
			text := strings.ReplaceAll(string(data), "#import site", "import site")
			_ = os.WriteFile(pthPath, []byte(text), 0o644)
		}
	}

	// Download get-pip.py.
	pipScript := filepath.Join(pyDir, "get-pip.py")
	if err := download("https://bootstrap.pypa.io/get-pip.py", pipScript, nil); err != nil {
		return "", "", fmt.Errorf("download get-pip.py: %w", err)
	}
	cmd := exec.Command(pyExe, pipScript, "--no-warn-script-location")
	cmd.Dir = pyDir
	if out, err := cmd.CombinedOutput(); err != nil {
		return "", "", fmt.Errorf("install pip: %w\n%s", err, string(out))
	}
	_ = os.Remove(pipScript)

	ver, err := pythonVersion(pyExe)
	return pyExe, ver, err
}

// setupDarwinPython downloads the python.org macOS installer package and
// extracts it to the install directory using pkgutil. This avoids requiring
// administrator privileges because we extract to a user-writable path only.
func setupDarwinPython(ctx *installContext) (string, string, error) {
	pyDir := filepath.Join(ctx.installDir, ".python")
	pyExe := filepath.Join(pyDir, "bin", "python3")
	if fileExists(pyExe) {
		ver, err := pythonVersion(pyExe)
		return pyExe, ver, err
	}

	// Prefer the universal2 installer.
	pkgName := fmt.Sprintf("python-%s-macos11.pkg", targetPythonVersion)
	url := fmt.Sprintf("https://www.python.org/ftp/python/%s/%s", targetPythonVersion, pkgName)
	pkgPath := filepath.Join(ctx.installDir, pkgName)
	if err := download(url, pkgPath, nil); err != nil {
		return "", "", fmt.Errorf("download python pkg: %w", err)
	}

	expandDir := filepath.Join(ctx.installDir, "python-pkg-expand")
	_ = os.RemoveAll(expandDir)
	cmd := exec.Command("pkgutil", "--expand", pkgPath, expandDir)
	if out, err := cmd.CombinedOutput(); err != nil {
		return "", "", fmt.Errorf("expand python pkg: %w\n%s", err, string(out))
	}
	_ = os.Remove(pkgPath)

	// The payload is a Payload archive under Python_Framework.pkg or similar.
	var payload string
	_ = filepath.Walk(expandDir, func(path string, info os.FileInfo, err error) error {
		if err == nil && info.Name() == "Payload" && !info.IsDir() {
			payload = path
			return filepath.SkipAll
		}
		return nil
	})
	if payload == "" {
		return "", "", fmt.Errorf("no Payload found in python pkg")
	}

	payloadDir := filepath.Join(ctx.installDir, "python-payload")
	_ = os.RemoveAll(payloadDir)
	cmd = exec.Command("tar", "-xf", payload, "-C", payloadDir)
	if out, err := cmd.CombinedOutput(); err != nil {
		return "", "", fmt.Errorf("extract python payload: %w\n%s", err, string(out))
	}
	_ = os.RemoveAll(expandDir)

	// Locate Python.framework/Versions/Current/bin/python3 inside payloadDir.
	var found string
	_ = filepath.Walk(payloadDir, func(path string, info os.FileInfo, err error) error {
		if err != nil || info.IsDir() {
			return nil
		}
		if info.Name() == "python3" {
			found = path
			return filepath.SkipAll
		}
		return nil
	})
	if found == "" {
		return "", "", fmt.Errorf("python3 binary not found in extracted pkg")
	}

	// Move the framework contents into a conventional layout.
	frameworkRoot := filepath.Dir(filepath.Dir(found)) // .../Versions/Current
	if err := os.Rename(frameworkRoot, pyDir); err != nil {
		return "", "", fmt.Errorf("relocate python: %w", err)
	}
	_ = os.RemoveAll(payloadDir)

	// Ensure python3 and python exist.
	if !fileExists(filepath.Join(pyDir, "bin", "python")) {
		_ = os.Symlink("python3", filepath.Join(pyDir, "bin", "python"))
	}

	ver, err := pythonVersion(pyExe)
	return pyExe, ver, err
}

// setupLinuxPython checks for a system Python 3.11+ first; if missing it
// returns an error. Official portable Linux Python binaries are not provided by
// python.org, so we rely on the distribution's package manager being present.
func setupLinuxPython(ctx *installContext) (string, string, error) {
	pyDir := filepath.Join(ctx.installDir, ".python")
	pyExe := filepath.Join(pyDir, "bin", "python3")
	if fileExists(pyExe) {
		ver, err := pythonVersion(pyExe)
		return pyExe, ver, err
	}

	// Look for a suitable system python3.
	candidates := []string{"python3.12", "python3.11", "python3.13", "python3"}
	for _, name := range candidates {
		if path, err := exec.LookPath(name); err == nil {
			ver, err := pythonVersion(path)
			if err == nil && versionMajorMinor(ver) >= 3_11 {
				return path, ver, nil
			}
		}
	}

	return "", "", fmt.Errorf(
		"no suitable system Python 3.11+ found. " +
			"Please install python3 (e.g. apt install python3 python3-venv python3-pip) and run the installer again.",
	)
}

// pythonVersion returns the version string reported by the interpreter.
func pythonVersion(py string) (string, error) {
	out, err := exec.Command(py, "--version").CombinedOutput()
	if err != nil {
		return "", err
	}
	return strings.TrimSpace(strings.TrimPrefix(string(out), "Python ")), nil
}

// versionMajorMinor converts "3.12.8" to 312 (3*100 + 12).
func versionMajorMinor(ver string) int {
	parts := strings.Split(ver, ".")
	if len(parts) < 2 {
		return 0
	}
	var major, minor int
	fmt.Sscanf(parts[0], "%d", &major)
	fmt.Sscanf(parts[1], "%d", &minor)
	return major*100 + minor
}

func pythonArch() string {
	switch runtime.GOARCH {
	case "amd64":
		return "amd64"
	case "arm64":
		return "arm64"
	default:
		return runtime.GOARCH
	}
}

func fileExists(path string) bool {
	_, err := os.Stat(path)
	return err == nil
}
