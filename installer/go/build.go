package main

import (
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"strings"
)

// downloadSource fetches and extracts the source tarball for ctx.tag.
func downloadSource(ctx *installContext, log *guiLogger) error {
	_ = os.RemoveAll(ctx.srcDir)
	tmpDir := filepath.Join(ctx.installDir, "src-tmp")
	_ = os.RemoveAll(tmpDir)

	url := sourceTarballURL(ctx.tag)
	archive := filepath.Join(ctx.installDir, "source.tar.gz")
	if err := download(url, archive, log); err != nil {
		return err
	}
	defer os.Remove(archive)

	if err := extractTarGz(archive, tmpDir, false); err != nil {
		return err
	}

	// Move the single extracted top-level directory to the final src path.
	entries, err := os.ReadDir(tmpDir)
	if err != nil {
		return err
	}
	if len(entries) != 1 || !entries[0].IsDir() {
		return fmt.Errorf("unexpected source tarball layout")
	}
	if err := os.Rename(filepath.Join(tmpDir, entries[0].Name()), ctx.srcDir); err != nil {
		return err
	}
	_ = os.RemoveAll(tmpDir)
	return nil
}

// installPythonDeps creates a venv in the source tree and installs
// requirements.txt + pyinstaller.
func installPythonDeps(ctx *installContext, log *guiLogger) error {
	venvDir := filepath.Join(ctx.srcDir, ".venv")
	_ = os.RemoveAll(venvDir)

	cmd := exec.Command(ctx.pythonExec, "-m", "venv", venvDir)
	if out, err := cmd.CombinedOutput(); err != nil {
		return fmt.Errorf("create venv: %w\n%s", err, string(out))
	}

	pip := filepath.Join(venvDir, "bin", "pip")
	if runtime.GOOS == "windows" {
		pip = filepath.Join(venvDir, "Scripts", "pip.exe")
	}

	req := filepath.Join(ctx.srcDir, "requirements.txt")
	cmd = exec.Command(pip, "install", "-r", req, "pyinstaller")
	cmd.Dir = ctx.srcDir
	if out, err := cmd.CombinedOutput(); err != nil {
		return fmt.Errorf("pip install: %w\n%s", err, string(out))
	}

	// Update yt-dlp to latest nightly for the same behavior as package.sh.
	cmd = exec.Command(pip, "install", "-U", "--pre", "yt-dlp")
	cmd.Dir = ctx.srcDir
	if out, err := cmd.CombinedOutput(); err != nil {
		log.Logf("Warning: yt-dlp nightly update failed, continuing: %s", string(out))
	}

	return nil
}

// buildBundle runs PyInstaller in the source tree.
func buildBundle(ctx *installContext, log *guiLogger) error {
	// Ensure a node binary exists in the source .tools/bin expected by package.sh.
	toolsBin := filepath.Join(ctx.srcDir, ".tools", "bin")
	if err := os.MkdirAll(toolsBin, 0o755); err != nil {
		return err
	}
	nodeSrc := filepath.Join(ctx.nodeDir, "bin", "node")
	nodeDst := filepath.Join(toolsBin, "node")
	if runtime.GOOS == "windows" {
		nodeSrc = filepath.Join(ctx.nodeDir, "node.exe")
		nodeDst = filepath.Join(toolsBin, "node.exe")
	}
	_ = os.Remove(nodeDst)
	if err := os.Symlink(nodeSrc, nodeDst); err != nil {
		// Fall back to copy if symlink fails (Windows non-admin).
		if err := copyFile(nodeSrc, nodeDst); err != nil {
			return fmt.Errorf("link node into source tree: %w", err)
		}
	}

	// Ensure certifi/cacert.pem exists.
	certDir := filepath.Join(ctx.srcDir, "certifi")
	if err := os.MkdirAll(certDir, 0o755); err != nil {
		return err
	}
	py := venvPython(ctx)
	cmd := exec.Command(py, "-c", "import certifi; print(certifi.where())")
	cmd.Dir = ctx.srcDir
	out, err := cmd.CombinedOutput()
	if err != nil {
		return fmt.Errorf("locate certifi bundle: %w\n%s", err, string(out))
	}
	certSrc := strings.TrimSpace(string(out))
	if err := copyFile(certSrc, filepath.Join(certDir, "cacert.pem")); err != nil {
		return fmt.Errorf("copy certifi bundle: %w", err)
	}

	pyinstaller := venvPyInstaller(ctx)
	args := []string{
		"server.py",
		"--name=gozik-yt-music-server",
		"--onedir",
		"--distpath=dist",
		"--workpath=build",
		"--hidden-import=ytmusicapi",
		"--hidden-import=grpc",
		"--hidden-import=grpc._cython",
		"--hidden-import=grpc._cython.cygrpc",
		"--hidden-import=google.protobuf",
		"--hidden-import=google.protobuf.internal",
		"--hidden-import=yt_dlp",
		"--hidden-import=yt_dlp.extractor",
		"--hidden-import=yt_dlp.extractor.youtubetab",
		"--hidden-import=yt_dlp.extractor.youtube",
		"--hidden-import=requests",
		"--hidden-import=certifi",
		"--hidden-import=charset_normalizer",
		"--hidden-import=idna",
		"--hidden-import=urllib3",
		"--hidden-import=generated",
		"--hidden-import=handlers",
		"--collect-all=ytmusicapi",
		"--collect-all=grpc",
		"--collect-all=google.protobuf",
		"--collect-all=yt_dlp",
		"--collect-all=certifi",
		fmt.Sprintf("--add-data=%s", pyinstallerDataArg("generated", "generated")),
		fmt.Sprintf("--add-data=%s", pyinstallerDataArg(filepath.Join("certifi", "cacert.pem"), "certifi")),
		fmt.Sprintf("--add-binary=%s", pyinstallerDataArg(nodeSrc, ".")),
		"--exclude-module=tkinter",
		"--exclude-module=unittest",
		"--exclude-module=test",
		"--exclude-module=grpc_tools",
		"--exclude-module=lib2to3",
		"--exclude-module=pydoc",
		"--exclude-module=doctest",
		"--noconfirm",
		"--clean",
	}

	cmd = exec.Command(pyinstaller, args...)
	cmd.Dir = ctx.srcDir
	if out, err := cmd.CombinedOutput(); err != nil {
		return fmt.Errorf("pyinstaller build: %w\n%s", err, string(out))
	}

	return nil
}

func venvPython(ctx *installContext) string {
	if runtime.GOOS == "windows" {
		return filepath.Join(ctx.srcDir, ".venv", "Scripts", "python.exe")
	}
	return filepath.Join(ctx.srcDir, ".venv", "bin", "python3")
}

func venvPyInstaller(ctx *installContext) string {
	if runtime.GOOS == "windows" {
		return filepath.Join(ctx.srcDir, ".venv", "Scripts", "pyinstaller.exe")
	}
	return filepath.Join(ctx.srcDir, ".venv", "bin", "pyinstaller")
}

func copyFile(src, dst string) error {
	in, err := os.Open(src)
	if err != nil {
		return err
	}
	defer in.Close()
	if err := os.MkdirAll(filepath.Dir(dst), 0o755); err != nil {
		return err
	}
	out, err := os.Create(dst)
	if err != nil {
		return err
	}
	defer out.Close()
	_, err = out.ReadFrom(in)
	if err != nil {
		return err
	}
	info, err := in.Stat()
	if err != nil {
		return err
	}
	return os.Chmod(dst, info.Mode())
}

// pyinstallerDataArg builds a PyInstaller --add-data/--add-binary argument using
// the correct path separator for the current platform (':' on Unix, ';' on Windows).
func pyinstallerDataArg(src, dst string) string {
	sep := ":"
	if runtime.GOOS == "windows" {
		sep = ";"
	}
	return src + sep + dst
}
