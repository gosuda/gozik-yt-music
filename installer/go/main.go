// gozik-yt-music installer — cross-platform GUI installer written in Go + Fyne.
//
// The installer downloads a portable Python runtime, the source tarball for the
// requested tag, and a Node.js binary, then builds a PyInstaller bundle locally
// for the current user. Finally it installs the bundle and registers autostart.
//
// Usage:
//   gozik-yt-music-installer [--tag v1.2.3]
//
// Platform support: Linux, macOS, Windows.

package main

import (
	"flag"
	"fmt"
	"log"
	"os"
	"path/filepath"
	"runtime"
	"strings"

	"fyne.io/fyne/v2"
	"fyne.io/fyne/v2/app"
	"fyne.io/fyne/v2/container"
	"fyne.io/fyne/v2/dialog"
	"fyne.io/fyne/v2/layout"
	"fyne.io/fyne/v2/widget"
)

const (
	appID       = "com.gosuda.gozik.ytmusic.installer"
	appName     = "gozik YouTube Music Installer"
	githubOwner = "gosuda"
	githubRepo  = "gozik-yt-music"
	defaultTag  = "latest"
)

// installContext carries every piece of state needed by the wizard and the
// background installation worker.
type installContext struct {
	tag            string
	installDir     string
	dataDir        string
	pythonExec     string
	nodeDir        string
	srcDir         string
	bundleDir      string
	agreedToTerms  bool
	createShortcut bool
	startService   bool

	// Filled in during install.
	pythonVersion string
	downloadedPy  string
	err           error
	warnings      []string
}

func main() {
	var tagFlag string
	flag.StringVar(&tagFlag, "tag", defaultTag, "Tag or version to install (e.g. v1.2.3)")
	flag.Parse()

	ctx := &installContext{
		tag:            tagFlag,
		createShortcut: true,
		startService:   true,
	}

	if ctx.tag == "" || ctx.tag == defaultTag {
		ctx.tag = defaultTag
	}

	// Compute default installation paths before the GUI opens.
	if err := ctx.resolvePaths(); err != nil {
		log.Fatalf("failed to resolve installation paths: %v", err)
	}

	a := app.NewWithID(appID)
	w := a.NewWindow(appName)
	w.Resize(fyne.NewSize(640, 480))
	w.CenterOnScreen()

	buildWizard(a, w, ctx)

	w.ShowAndRun()
}

// resolvePaths computes platform-appropriate default directories.
func (ctx *installContext) resolvePaths() error {
	home, err := os.UserHomeDir()
	if err != nil {
		return err
	}

	ctx.dataDir = userDataDir(home)
	ctx.installDir = filepath.Join(ctx.dataDir, "gozik-yt-music-server")
	ctx.srcDir = filepath.Join(ctx.installDir, "src")
	ctx.nodeDir = filepath.Join(ctx.installDir, ".tools")
	ctx.bundleDir = filepath.Join(ctx.installDir, "dist", "gozik-yt-music-server")

	if err := os.MkdirAll(ctx.installDir, 0o755); err != nil {
		return err
	}
	return nil
}

// setInstallDir updates the install directory and all derived paths after
// the user selects a location. It creates the directory if it does not exist.
func (ctx *installContext) setInstallDir(dir string) error {
	dir = filepath.Clean(dir)
	if dir == "" {
		return fmt.Errorf("install directory cannot be empty")
	}

	info, err := os.Stat(dir)
	if err == nil && !info.IsDir() {
		return fmt.Errorf("%s is not a directory", dir)
	}
	if err != nil && !os.IsNotExist(err) {
		return err
	}

	ctx.installDir = dir
	ctx.srcDir = filepath.Join(dir, "src")
	ctx.nodeDir = filepath.Join(dir, ".tools")
	ctx.bundleDir = filepath.Join(dir, "dist", "gozik-yt-music-server")

	if err := os.MkdirAll(dir, 0o755); err != nil {
		return err
	}
	return nil
}

// buildWizard assembles the wizard screens.
func buildWizard(a fyne.App, w fyne.Window, ctx *installContext) {
	var (
		welcomeContent  fyne.CanvasObject
		licenseContent  fyne.CanvasObject
		optionsContent  fyne.CanvasObject
		progressContent fyne.CanvasObject
		finishContent   fyne.CanvasObject
	)

	next := func(content fyne.CanvasObject) {}

	// ---- Welcome screen ------------------------------------------------------
	welcomeTitle := widget.NewLabelWithStyle("Welcome", fyne.TextAlignLeading, fyne.TextStyle{Bold: true})
	welcomeText := widget.NewLabel(fmt.Sprintf(
		"This installer will download and build gozik YouTube Music for %s/%s.\n\n"+
			"It will download a portable Python runtime, the source code, and a Node.js binary, "+
			"then build the application locally on your machine.",
		runtime.GOOS, runtime.GOARCH,
	))
	welcomeText.Wrapping = fyne.TextWrapWord

	welcomeBtn := widget.NewButton("Next", nil)
	welcomeContent = container.NewBorder(
		welcomeTitle,
		container.NewHBox(layout.NewSpacer(), welcomeBtn),
		nil, nil,
		container.NewPadded(welcomeText),
	)

	// ---- License screen ------------------------------------------------------
	licenseTitle := widget.NewLabelWithStyle("License Agreement", fyne.TextAlignLeading, fyne.TextStyle{Bold: true})
	licenseText := widget.NewMultiLineEntry()
	licenseText.SetText(termsText())
	licenseText.Disable()
	licenseText.Wrapping = fyne.TextWrapWord

	agreeCheck := widget.NewCheck("I agree to the terms and conditions", func(checked bool) {
		ctx.agreedToTerms = checked
	})

	licenseBack := widget.NewButton("Back", nil)
	licenseNext := widget.NewButton("Next", nil)
	licenseContent = container.NewBorder(
		licenseTitle,
		container.NewHBox(licenseBack, layout.NewSpacer(), agreeCheck, licenseNext),
		nil, nil,
		container.NewPadded(licenseText),
	)

	// ---- Options screen ------------------------------------------------------
	optionsTitle := widget.NewLabelWithStyle("Installation Options", fyne.TextAlignLeading, fyne.TextStyle{Bold: true})

	tagEntry := widget.NewEntry()
	tagEntry.SetText(ctx.tag)
	tagEntry.SetPlaceHolder("latest or v1.2.3")

	installDirEntry := widget.NewEntry()
	installDirEntry.SetText(ctx.installDir)
	installDirEntry.Disable()

	browseBtn := widget.NewButton("Browse...", func() {
		dialog.ShowFolderOpen(func(uri fyne.ListableURI, err error) {
			if err != nil {
				dialog.ShowError(err, w)
				return
			}
			if uri == nil {
				return
			}
			if err := ctx.setInstallDir(uri.Path()); err != nil {
				dialog.ShowError(err, w)
				return
			}
			installDirEntry.SetText(ctx.installDir)
		}, w)
	})
	installDirBox := container.NewBorder(nil, nil, nil, browseBtn, installDirEntry)

	shortcutCheck := widget.NewCheck("Create application menu shortcut", func(v bool) { ctx.createShortcut = v })
	shortcutCheck.SetChecked(ctx.createShortcut)

	startCheck := widget.NewCheck("Start gozik YouTube Music after installation", func(v bool) { ctx.startService = v })
	startCheck.SetChecked(ctx.startService)

	optionsForm := widget.NewForm(
		widget.NewFormItem("Version / tag", tagEntry),
		widget.NewFormItem("Install directory", installDirBox),
	)

	optionsBack := widget.NewButton("Back", nil)
	optionsInstall := widget.NewButton("Install", nil)
	optionsInstall.Importance = widget.HighImportance

	optionsContent = container.NewBorder(
		optionsTitle,
		container.NewHBox(optionsBack, layout.NewSpacer(), shortcutCheck, startCheck, optionsInstall),
		nil, nil,
		container.NewPadded(container.NewVBox(optionsForm, widget.NewLabel(""))),
	)

	// ---- Progress screen -----------------------------------------------------
	progressTitle := widget.NewLabelWithStyle("Installing", fyne.TextAlignLeading, fyne.TextStyle{Bold: true})
	progressBar := widget.NewProgressBar()
	progressBar.Min = 0
	progressBar.Max = 1
	progressLog := widget.NewMultiLineEntry()
	progressLog.Disable()
	progressLog.Wrapping = fyne.TextWrapWord

	progressContent = container.NewBorder(
		progressTitle,
		nil,
		nil, nil,
		container.NewPadded(container.NewVBox(progressBar, progressLog)),
	)

	// ---- Finish screen -------------------------------------------------------
	finishTitle := widget.NewLabelWithStyle("Installation Complete", fyne.TextAlignLeading, fyne.TextStyle{Bold: true})
	finishText := widget.NewLabel("")
	finishText.Wrapping = fyne.TextWrapWord
	finishClose := widget.NewButton("Close", func() { a.Quit() })
	finishClose.Importance = widget.HighImportance

	finishContent = container.NewBorder(
		finishTitle,
		container.NewHBox(layout.NewSpacer(), finishClose),
		nil, nil,
		container.NewPadded(finishText),
	)

	// ---- Navigation ----------------------------------------------------------
	screens := map[string]fyne.CanvasObject{
		"welcome":  welcomeContent,
		"license":  licenseContent,
		"options":  optionsContent,
		"progress": progressContent,
		"finish":   finishContent,
	}

	next = func(content fyne.CanvasObject) {
		for _, obj := range screens {
			if obj == content {
				w.SetContent(content)
				return
			}
		}
	}

	welcomeBtn.OnTapped = func() { next(licenseContent) }
	licenseBack.OnTapped = func() { next(welcomeContent) }
	licenseNext.OnTapped = func() {
		if !ctx.agreedToTerms {
			dialog.ShowInformation("Agreement Required", "Please accept the terms and conditions to continue.", w)
			return
		}
		next(optionsContent)
	}
	optionsBack.OnTapped = func() { next(licenseContent) }
	optionsInstall.OnTapped = func() {
		ctx.tag = strings.TrimSpace(tagEntry.Text)
		if ctx.tag == "" {
			ctx.tag = defaultTag
		}
		ctx.tag = normalizeTag(ctx.tag)
		next(progressContent)
		go runInstall(ctx, progressBar, progressLog, func() {
			if ctx.err != nil {
				finishText.SetText(fmt.Sprintf("Installation failed:\n%v", ctx.err))
			} else {
				text := fmt.Sprintf(
					"gozik YouTube Music has been installed to:\n%s\n\n"+
						"Autostart has been %s.",
					ctx.installDir,
					autostartStatus(ctx),
				)
				if len(ctx.warnings) > 0 {
					text += "\n\nWarnings:\n" + strings.Join(ctx.warnings, "\n")
				}
				finishText.SetText(text)
			}
			// Run on main thread.
			fyne.CurrentApp().Driver().CanvasForObject(finishText).Refresh(finishText)
			next(finishContent)
		})
	}

	w.SetContent(welcomeContent)
}

// runInstall performs the actual installation in the background.
func runInstall(ctx *installContext, bar *widget.ProgressBar, logBox *widget.Entry, done func()) {
	logger := &guiLogger{box: logBox}
	setProgress := func(p float64) { bar.SetValue(p) }

	defer func() {
		setProgress(1)
		done()
	}()

	setProgress(0.05)
	logger.Logf("Resolving version %s...", ctx.tag)
	if ctx.tag == defaultTag || ctx.tag == "" {
		latest, err := resolveLatestTag()
		if err != nil {
			ctx.err = fmt.Errorf("failed to resolve latest tag: %w", err)
			logger.Logf("ERROR: %v", ctx.err)
			return
		}
		ctx.tag = latest
		logger.Logf("Latest version is %s", ctx.tag)
	}

	setProgress(0.10)
	logger.Logf("Installing for %s/%s into %s", runtime.GOOS, runtime.GOARCH, ctx.installDir)

	// 1. Bootstrap Python.
	setProgress(0.15)
	logger.Log("Setting up Python runtime...")
	py, pyVer, err := setupPython(ctx)
	if err != nil {
		ctx.err = fmt.Errorf("python setup failed: %w", err)
		logger.Logf("ERROR: %v", ctx.err)
		return
	}
	ctx.pythonExec = py
	ctx.pythonVersion = pyVer
	logger.Logf("Python ready: %s (%s)", py, pyVer)

	// 2. Download source tarball.
	setProgress(0.30)
	logger.Logf("Downloading source %s...", ctx.tag)
	if err := downloadSource(ctx, logger); err != nil {
		ctx.err = fmt.Errorf("source download failed: %w", err)
		logger.Logf("ERROR: %v", ctx.err)
		return
	}
	logger.Log("Source downloaded.")

	// 3. Install Python dependencies into a venv.
	setProgress(0.45)
	logger.Log("Installing Python dependencies...")
	if err := installPythonDeps(ctx, logger); err != nil {
		ctx.err = fmt.Errorf("dependency install failed: %w", err)
		logger.Logf("ERROR: %v", ctx.err)
		return
	}
	logger.Log("Dependencies installed.")

	// 4. Download Node.js binary.
	setProgress(0.55)
	logger.Log("Downloading Node.js binary...")
	if err := setupNode(ctx, logger); err != nil {
		ctx.err = fmt.Errorf("node setup failed: %w", err)
		logger.Logf("ERROR: %v", ctx.err)
		return
	}
	logger.Log("Node.js ready.")

	// 5. Build with PyInstaller.
	setProgress(0.70)
	logger.Log("Building gozik-yt-music bundle with PyInstaller...")
	if err := buildBundle(ctx, logger); err != nil {
		ctx.err = fmt.Errorf("build failed: %w", err)
		logger.Logf("ERROR: %v", ctx.err)
		return
	}
	logger.Log("Build complete.")

	// 6. Install bundle and wrapper.
	setProgress(0.85)
	logger.Log("Installing bundle and wrapper...")
	if err := installBundle(ctx, logger); err != nil {
		ctx.err = fmt.Errorf("install failed: %w", err)
		logger.Logf("ERROR: %v", ctx.err)
		return
	}
	logger.Log("Bundle installed.")

	// 7. Register autostart.
	setProgress(0.95)
	logger.Log("Registering autostart...")
	if err := registerAutostart(ctx, logger); err != nil {
		ctx.err = fmt.Errorf("autostart registration failed: %w", err)
		logger.Logf("ERROR: %v", ctx.err)
		return
	}
	logger.Log("Autostart registered.")

	// 8. Optionally create shortcut and start service. These are best-effort:
	// failures are reported as warnings rather than failing the whole install.
	if ctx.createShortcut {
		logger.Log("Creating application shortcut...")
		if err := createShortcut(ctx, logger); err != nil {
			ctx.warnings = append(ctx.warnings, fmt.Sprintf("shortcut: %v", err))
			logger.Logf("WARNING: %v", err)
		}
	}
	if ctx.startService {
		logger.Log("Starting service...")
		if err := startService(ctx, logger); err != nil {
			ctx.warnings = append(ctx.warnings, fmt.Sprintf("start service: %v", err))
			logger.Logf("WARNING: %v", err)
		}
	}

	logger.Log("Installation finished successfully.")
}

func normalizeTag(tag string) string {
	tag = strings.TrimSpace(tag)
	if tag == "" {
		return defaultTag
	}
	if tag == defaultTag {
		return tag
	}
	if !strings.HasPrefix(tag, "v") {
		return "v" + tag
	}
	return tag
}

func autostartStatus(ctx *installContext) string {
	if ctx.startService {
		return "enabled"
	}
	return "disabled"
}

func termsText() string {
	return `gozik YouTube Music Installer

This installer will download third-party software (Python, Node.js, Python packages) from the internet and build the gozik YouTube Music plugin server on your local machine.

By clicking "I agree" you consent to:
1. Downloading software from python.org, nodejs.org, and PyPI.
2. Building and installing the application under your user profile.
3. Registering a user-level autostart entry so the plugin starts on login.

The installed software is provided as-is under the same license as the gozik project.
`
}
