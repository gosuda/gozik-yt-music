package main

import (
	"archive/tar"
	"archive/zip"
	"compress/gzip"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"strings"
)

// extractTarGz extracts src to dst, optionally stripping the first path
// component. If stripTop is true, the top-level directory inside the tarball
// is removed so its contents land directly in dst.
func extractTarGz(src, dst string, stripTop bool) error {
	if err := os.MkdirAll(dst, 0o755); err != nil {
		return err
	}

	f, err := os.Open(src)
	if err != nil {
		return err
	}
	defer f.Close()

	gz, err := gzip.NewReader(f)
	if err != nil {
		return err
	}
	defer gz.Close()

	tr := tar.NewReader(gz)
	for {
		hdr, err := tr.Next()
		if err == io.EOF {
			break
		}
		if err != nil {
			return err
		}
		if hdr.Typeflag == tar.TypeXGlobalHeader || hdr.Typeflag == tar.TypeXHeader {
			continue
		}

		name := hdr.Name
		if stripTop {
			parts := strings.SplitN(name, string(os.PathSeparator), 2)
			if len(parts) < 2 {
				continue
			}
			name = parts[1]
		}
		if name == "" {
			continue
		}
		target := filepath.Join(dst, filepath.FromSlash(name))

		switch hdr.Typeflag {
		case tar.TypeDir:
			if err := os.MkdirAll(target, os.FileMode(hdr.Mode)); err != nil {
				return err
			}
		case tar.TypeReg, tar.TypeRegA:
			if err := os.MkdirAll(filepath.Dir(target), 0o755); err != nil {
				return err
			}
			out, err := os.OpenFile(target, os.O_CREATE|os.O_WRONLY|os.O_TRUNC, os.FileMode(hdr.Mode))
			if err != nil {
				return err
			}
			if _, err := io.Copy(out, tr); err != nil {
				out.Close()
				return err
			}
			out.Close()
		case tar.TypeSymlink:
			if err := os.MkdirAll(filepath.Dir(target), 0o755); err != nil {
				return err
			}
			if err := os.Symlink(hdr.Linkname, target); err != nil {
				return err
			}
		case tar.TypeLink:
			// Hard links inside a portable Python tarball are rare; skip.
			continue
		default:
			// Ignore other types.
		}
	}
	return nil
}

// extractZip extracts src to dst, optionally stripping the top-level directory.
func extractZip(src, dst string, stripTop bool) error {
	zr, err := zip.OpenReader(src)
	if err != nil {
		return err
	}
	defer zr.Close()

	if err := os.MkdirAll(dst, 0o755); err != nil {
		return err
	}

	for _, zf := range zr.File {
		name := zf.Name
		if stripTop {
			parts := strings.SplitN(name, string(os.PathSeparator), 2)
			if len(parts) < 2 {
				continue
			}
			name = parts[1]
		}
		if name == "" {
			continue
		}
		target := filepath.Join(dst, filepath.FromSlash(name))

		if zf.FileInfo().IsDir() {
			if err := os.MkdirAll(target, zf.Mode()); err != nil {
				return err
			}
			continue
		}

		if err := os.MkdirAll(filepath.Dir(target), 0o755); err != nil {
			return err
		}

		rc, err := zf.Open()
		if err != nil {
			return err
		}
		out, err := os.OpenFile(target, os.O_CREATE|os.O_WRONLY|os.O_TRUNC, zf.Mode())
		if err != nil {
			rc.Close()
			return err
		}
		_, err = io.Copy(out, rc)
		out.Close()
		rc.Close()
		if err != nil {
			return err
		}
	}
	return nil
}

// extractArchive dispatches to the correct extractor based on file extension.
func extractArchive(src, dst string, stripTop bool) error {
	switch {
	case strings.HasSuffix(strings.ToLower(src), ".tar.gz"), strings.HasSuffix(strings.ToLower(src), ".tgz"):
		return extractTarGz(src, dst, stripTop)
	case strings.HasSuffix(strings.ToLower(src), ".zip"):
		return extractZip(src, dst, stripTop)
	default:
		return fmt.Errorf("unsupported archive: %s", src)
	}
}
