package main

import (
	"fmt"

	"fyne.io/fyne/v2"
	"fyne.io/fyne/v2/widget"
)

// guiLogger appends messages to a Fyne multi-line entry.
type guiLogger struct {
	box *widget.Entry
}

func (l *guiLogger) Log(msg string) {
	l.Logf(msg)
}

func (l *guiLogger) Logf(format string, args ...interface{}) {
	text := fmt.Sprintf(format, args...)
	current := l.box.Text
	if current != "" {
		current += "\n"
	}
	current += text
	l.box.SetText(current)
	l.box.CursorRow = len(splitLines(current))
	fyne.CurrentApp().Driver().CanvasForObject(l.box).Refresh(l.box)
}

func splitLines(s string) []string {
	var out []string
	start := 0
	for i := 0; i < len(s); i++ {
		if s[i] == '\n' {
			out = append(out, s[start:i])
			start = i + 1
		}
	}
	if start < len(s) {
		out = append(out, s[start:])
	}
	return out
}
