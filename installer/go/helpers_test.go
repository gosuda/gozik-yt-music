package main

import (
	"runtime"
	"testing"
)

func TestNormalizeTag(t *testing.T) {
	cases := []struct {
		in, want string
	}{
		{"", "latest"},
		{"latest", "latest"},
		{"1.2.3", "v1.2.3"},
		{"v1.2.3", "v1.2.3"},
		{"  v1.0  ", "v1.0"},
	}
	for _, c := range cases {
		got := normalizeTag(c.in)
		if got != c.want {
			t.Errorf("normalizeTag(%q) = %q, want %q", c.in, got, c.want)
		}
	}
}

func TestVersionMajorMinor(t *testing.T) {
	cases := []struct {
		in   string
		want int
	}{
		{"3.12.8", 312},
		{"3.11.0", 311},
		{"3.9.1", 309},
		{"3.12", 312},
		{"bad", 0},
	}
	for _, c := range cases {
		got := versionMajorMinor(c.in)
		if got != c.want {
			t.Errorf("versionMajorMinor(%q) = %d, want %d", c.in, got, c.want)
		}
	}
}

func TestPyinstallerDataArg(t *testing.T) {
	got := pyinstallerDataArg("src", "dst")
	var want string
	if runtime.GOOS == "windows" {
		want = "src;dst"
	} else {
		want = "src:dst"
	}
	if got != want {
		t.Errorf("pyinstallerDataArg(src,dst) = %q, want %q", got, want)
	}
}
