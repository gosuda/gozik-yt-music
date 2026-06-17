package main

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"os"
	"path/filepath"
	"strings"
	"time"
)

const maxDownloadRetries = 3

// download downloads url to dst with retries, reporting progress through log.
func download(url, dst string, log *guiLogger) error {
	var lastErr error
	for attempt := 1; attempt <= maxDownloadRetries; attempt++ {
		if attempt > 1 && log != nil {
			log.Logf("Retrying download (attempt %d/%d)...", attempt, maxDownloadRetries)
		}
		lastErr = downloadOnce(url, dst, log)
		if lastErr == nil {
			return nil
		}
		if attempt < maxDownloadRetries {
			time.Sleep(time.Duration(attempt) * time.Second)
		}
	}
	return fmt.Errorf("download failed after %d attempts: %w", maxDownloadRetries, lastErr)
}

// downloadOnce performs a single HTTP GET and writes the response to dst.
func downloadOnce(url, dst string, log *guiLogger) error {
	if err := os.MkdirAll(filepath.Dir(dst), 0o755); err != nil {
		return err
	}

	out, err := os.Create(dst)
	if err != nil {
		return err
	}
	defer out.Close()

	client := &http.Client{Timeout: 0}
	req, err := http.NewRequest("GET", url, nil)
	if err != nil {
		return err
	}
	req.Header.Set("User-Agent", "gozik-yt-music-installer/1.0")

	resp, err := client.Do(req)
	if err != nil {
		return err
	}
	defer resp.Body.Close()

	if resp.StatusCode != http.StatusOK {
		return fmt.Errorf("HTTP %d: %s", resp.StatusCode, url)
	}

	var r io.Reader = resp.Body
	if log != nil && resp.ContentLength > 0 {
		r = &loggingReader{r: resp.Body, total: resp.ContentLength, log: log}
	}

	_, err = io.Copy(out, r)
	return err
}

type loggingReader struct {
	r       io.Reader
	total   int64
	read    int64
	lastPct int
	log     *guiLogger
}

func (lr *loggingReader) Read(p []byte) (int, error) {
	n, err := lr.r.Read(p)
	lr.read += int64(n)
	if lr.total > 0 {
		pct := int(float64(lr.read) / float64(lr.total) * 100)
		if pct != lr.lastPct && pct%10 == 0 {
			lr.log.Logf("Download %d%%", pct)
			lr.lastPct = pct
		}
	}
	return n, err
}

// resolveLatestTag asks the GitHub API for the latest release tag name.
func resolveLatestTag() (string, error) {
	url := fmt.Sprintf("https://api.github.com/repos/%s/%s/releases/latest", githubOwner, githubRepo)
	client := &http.Client{Timeout: 15 * time.Second}
	req, err := http.NewRequest("GET", url, nil)
	if err != nil {
		return "", err
	}
	req.Header.Set("User-Agent", "gozik-yt-music-installer/1.0")
	resp, err := client.Do(req)
	if err != nil {
		return "", err
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return "", fmt.Errorf("github api returned %d", resp.StatusCode)
	}
	var payload struct {
		TagName string `json:"tag_name"`
	}
	if err := json.NewDecoder(resp.Body).Decode(&payload); err != nil {
		return "", err
	}
	if payload.TagName == "" {
		return "", fmt.Errorf("no tag_name in github response")
	}
	return payload.TagName, nil
}

// sha256OfFile returns the hex-encoded SHA-256 digest of path.
func sha256OfFile(path string) (string, error) {
	f, err := os.Open(path)
	if err != nil {
		return "", err
	}
	defer f.Close()
	h := sha256.New()
	if _, err := io.Copy(h, f); err != nil {
		return "", err
	}
	return hex.EncodeToString(h.Sum(nil)), nil
}

// verifySHA256 checks that path matches the expected SHA-256 digest.
func verifySHA256(path, expected string) error {
	got, err := sha256OfFile(path)
	if err != nil {
		return err
	}
	if !strings.EqualFold(got, expected) {
		return fmt.Errorf("checksum mismatch for %s: got %s, expected %s", path, got, expected)
	}
	return nil
}

// nodeChecksumURL returns the Node.js SHASUMS256.txt URL for the configured version.
func nodeChecksumURL() string {
	return fmt.Sprintf("https://nodejs.org/dist/v%s/SHASUMS256.txt", targetNodeVersion)
}

// verifyNodeArchive downloads the official SHASUMS256.txt and verifies the archive.
func verifyNodeArchive(archive string) error {
	name := filepath.Base(archive)
	sumsPath := filepath.Join(filepath.Dir(archive), "SHASUMS256.txt")
	if err := downloadOnce(nodeChecksumURL(), sumsPath, nil); err != nil {
		return fmt.Errorf("download SHASUMS256: %w", err)
	}
	defer os.Remove(sumsPath)

	data, err := os.ReadFile(sumsPath)
	if err != nil {
		return err
	}
	for _, line := range strings.Split(string(data), "\n") {
		parts := strings.Fields(line)
		if len(parts) != 2 {
			continue
		}
		if parts[1] == name {
			return verifySHA256(archive, parts[0])
		}
	}
	return fmt.Errorf("no checksum found for %s in SHASUMS256.txt", name)
}

// sourceTarballURL returns the GitHub auto-generated source tarball URL.
func sourceTarballURL(tag string) string {
	return fmt.Sprintf("https://github.com/%s/%s/archive/refs/tags/%s.tar.gz", githubOwner, githubRepo, tag)
}

// repoExtractedDir returns the top-level directory name inside the GitHub
// source tarball, which is "<repo>-<tag>" (including the leading v).
func repoExtractedDir(tag string) string {
	return fmt.Sprintf("%s-%s", githubRepo, tag)
}
