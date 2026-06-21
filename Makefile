# Makefile for gozik-yt-music
# Standard GNU-style targets: all, build, install, uninstall, clean, distclean, codegen, dev
#
# Typical workflow:
#   make && sudo make install
#   sudo systemctl enable --now gozik-yt-music-server
#
# Build automatically fetches the latest yt-dlp nightly before packaging.
# To skip the nightly update and use the pinned stable version:
#   YTDLP_SKIP_NIGHTLY=1 make
#
# Development workflow:
#   make dev                     # create venv, install deps, run server.py

# Installation prefix (override with: make PREFIX=/opt)
PREFIX ?= /usr/local
BINDIR  ?= $(PREFIX)/bin
LIBDIR  ?= $(PREFIX)/lib
SYSTEMD_SYSTEM_UNIT_DIR ?= /etc/systemd/system
ICON_DIR ?= $(PREFIX)/share/icons/hicolor/scalable/apps
DESKTOP_DIR ?= $(PREFIX)/share/applications

# User-level install paths (no sudo required)
USER_PREFIX       ?= $(HOME)/.local
USER_BINDIR       ?= $(USER_PREFIX)/bin
USER_LIBDIR       ?= $(USER_PREFIX)/lib
USER_INSTALL_DIR  ?= $(USER_LIBDIR)/$(BINARY_NAME)
USER_INSTALL_WRAPPER ?= $(USER_BINDIR)/$(BINARY_NAME)
SYSTEMD_USER_UNIT_DIR ?= $(HOME)/.config/systemd/user
USER_ICON_DIR     ?= $(HOME)/.local/share/icons/hicolor/scalable/apps
USER_DESKTOP_DIR  ?= $(HOME)/.local/share/applications

BINARY_NAME := gozik-yt-music-server
BUNDLE_DIR  := dist/$(BINARY_NAME)
BUNDLE_BINARY := $(BUNDLE_DIR)/$(BINARY_NAME)
INSTALL_DIR := $(LIBDIR)/$(BINARY_NAME)
INSTALL_WRAPPER := $(BINDIR)/$(BINARY_NAME)
ICON_NAME := gozik-yt-music.svg
ICON_SRC := assets/$(ICON_NAME)

PYTHON := .venv/bin/python3
PIP    := $(PYTHON) -m pip

.PHONY: all build install install-user uninstall uninstall-user clean distclean codegen dev

# -----------------------------------------------------------------------------
# Default target
# -----------------------------------------------------------------------------
all: build

# -----------------------------------------------------------------------------
# Build the standalone application directory via package.sh
# (Automatically pulls the latest yt-dlp nightly; set YTDLP_SKIP_NIGHTLY=1 to skip.)
# -----------------------------------------------------------------------------
build: .download-node
	@echo "==> Ensuring Python virtual environment ..."
	@if [ ! -x ".venv/bin/python3" ] || ! .venv/bin/python3 -m pip --version >/dev/null 2>&1; then \
		rm -rf .venv; \
		python3 -m venv .venv; \
	fi
	$(PIP) install -q -r requirements.txt

	@echo "==> Building $(BINARY_NAME) ..."
	bash package.sh

# -----------------------------------------------------------------------------
# Generate protobuf Python stubs
# -----------------------------------------------------------------------------
codegen:
	bash codegen.sh

# -----------------------------------------------------------------------------
# Development target: ensure venv + deps, then run server.py directly
# -----------------------------------------------------------------------------
.download-node:
	@NODE_OS=$$(uname -s | tr '[:upper:]' '[:lower:]'); \
	NODE_ARCH=$$(uname -m); \
	case "$$NODE_ARCH" in \
		x86_64|amd64) NODE_ARCH="x64" ;; \
		aarch64|arm64) NODE_ARCH="arm64" ;; \
		*) echo "Unsupported architecture: $$NODE_ARCH" >&2; exit 1 ;; \
	esac; \
	NODE_URL="https://nodejs.org/dist/v22.14.0/node-v22.14.0-$${NODE_OS}-$${NODE_ARCH}.tar.xz"; \
	if [ ! -x ".tools/bin/node" ]; then \
		echo "==> Downloading Node.js standalone binary ($${NODE_OS}-$${NODE_ARCH}) ..."; \
		mkdir -p .tools && curl -fsSL "$${NODE_URL}" | tar -xJ --strip-components=1 -C .tools; \
	fi

dev: .download-node
	@if [ ! -f "$(PYTHON)" ]; then \
		echo "==> Creating virtualenv ..."; \
		python3 -m venv .venv; \
	fi
	@echo "==> Installing / updating dependencies ..."
	$(PIP) install -q -r requirements.txt
	@echo "==> Updating yt-dlp to latest nightly ..."
	$(PIP) install -q -U --pre yt-dlp
	@echo "==> Generating protobuf stubs ..."
	PYTHON=$(PYTHON) bash codegen.sh
	@echo "==> Starting development server ..."
	$(PYTHON) server.py

# -----------------------------------------------------------------------------
# Install bundle + systemd service unit
# -----------------------------------------------------------------------------
install:
	@test -d $(BUNDLE_DIR) || { echo "Bundle not found: $(BUNDLE_DIR). Run 'make' first."; exit 1; }
	@echo "==> Installing application bundle to $(DESTDIR)$(INSTALL_DIR) ..."
	install -d $(DESTDIR)$(INSTALL_DIR)
	cp -a $(BUNDLE_DIR)/. $(DESTDIR)$(INSTALL_DIR)/
	chmod 0755 $(DESTDIR)$(INSTALL_DIR)/$(BINARY_NAME)
	@echo "==> Installing wrapper script to $(DESTDIR)$(INSTALL_WRAPPER) ..."
	install -d $(DESTDIR)$(BINDIR)
	@printf '%s\n' \
		'#!/bin/sh' \
		'exec "$(INSTALL_DIR)/$(BINARY_NAME)" "$$@"' \
		> $(DESTDIR)$(INSTALL_WRAPPER)
	chmod 0755 $(DESTDIR)$(INSTALL_WRAPPER)
	@mkdir -p $(DESTDIR)$(SYSTEMD_SYSTEM_UNIT_DIR)
	@printf '%s\n' \
		'[Unit]' \
		'Description=gozik YouTube Music gRPC plugin server' \
		'After=network.target' \
		'' \
		'[Service]' \
		'Type=simple' \
		'ExecStart=$(INSTALL_WRAPPER)' \
		'Restart=on-failure' \
		'RestartSec=5' \
		'StandardOutput=journal' \
		'StandardError=journal' \
		'' \
		'[Install]' \
		'WantedBy=multi-user.target' \
		> $(DESTDIR)$(SYSTEMD_SYSTEM_UNIT_DIR)/$(BINARY_NAME).service
	@chmod 644 $(DESTDIR)$(SYSTEMD_SYSTEM_UNIT_DIR)/$(BINARY_NAME).service
	@# --- Desktop entry --------------------------------------------------------
	@mkdir -p $(DESTDIR)$(ICON_DIR)
	@install -Dm644 $(ICON_SRC) $(DESTDIR)$(ICON_DIR)/$(ICON_NAME)
	@mkdir -p $(DESTDIR)$(DESKTOP_DIR)
	@printf '%s\n' \
		'[Desktop Entry]' \
		'Name=gozik YouTube Music' \
		'Comment=YouTube Music plugin web console' \
		'Exec=$(INSTALL_WRAPPER)' \
		'Type=Application' \
		'Terminal=false' \
		'Icon=$(ICON_DIR)/$(ICON_NAME)' \
		'Categories=AudioVideo;Audio;Player;Network;' \
		'StartupNotify=true' \
		'StartupWMClass=gozik-yt-music' \
		> $(DESTDIR)$(DESKTOP_DIR)/gozik-yt-music-webui.desktop
	@chmod 644 $(DESTDIR)$(DESKTOP_DIR)/gozik-yt-music-webui.desktop
	@update-desktop-database $(DESTDIR)$(DESKTOP_DIR) 2>/dev/null || true
	@echo ""
	@echo "==> $(BINARY_NAME) installed to $(DESTDIR)$(INSTALL_DIR)"
	@echo "==> Wrapper script installed to $(DESTDIR)$(INSTALL_WRAPPER)"
	@echo "==> systemd unit installed to $(DESTDIR)$(SYSTEMD_SYSTEM_UNIT_DIR)/$(BINARY_NAME).service"
	@echo "==> Desktop entry installed to $(DESTDIR)$(DESKTOP_DIR)/gozik-yt-music-webui.desktop"
	@echo "==> Icon installed to $(DESTDIR)$(ICON_DIR)/$(ICON_NAME)"
	@echo ""
	@echo "    Start now : sudo systemctl enable --now $(BINARY_NAME).service"
	@echo "    Status    : sudo systemctl status $(BINARY_NAME).service"

# -----------------------------------------------------------------------------
# User-level install (no sudo, works with GUI popups)
# -----------------------------------------------------------------------------
install-user:
	@test -d $(BUNDLE_DIR) || { echo "Bundle not found: $(BUNDLE_DIR). Run 'make' first."; exit 1; }
	@echo "==> Installing application bundle to $(USER_INSTALL_DIR) ..."
	install -d $(USER_INSTALL_DIR)
	cp -a $(BUNDLE_DIR)/. $(USER_INSTALL_DIR)/
	chmod 0755 $(USER_INSTALL_DIR)/$(BINARY_NAME)
	@echo "==> Installing wrapper script to $(USER_INSTALL_WRAPPER) ..."
	install -d $(USER_BINDIR)
	@printf '%s\n' \
		'#!/bin/sh' \
		'exec "$(USER_INSTALL_DIR)/$(BINARY_NAME)" "$$@"' \
		> $(USER_INSTALL_WRAPPER)
	chmod 0755 $(USER_INSTALL_WRAPPER)
	@mkdir -p $(SYSTEMD_USER_UNIT_DIR)
	@printf '%s\n' \
		'[Unit]' \
		'Description=gozik YouTube Music gRPC plugin server' \
		'After=network.target' \
		'' \
		'[Service]' \
		'Type=simple' \
		'ExecStart=$(USER_INSTALL_WRAPPER)' \
		'Restart=on-failure' \
		'RestartSec=5' \
		'StandardOutput=journal' \
		'StandardError=journal' \
		'PassEnvironment=DISPLAY XAUTHORITY WAYLAND_DISPLAY' \
		'' \
		'[Install]' \
		'WantedBy=default.target' \
		> $(SYSTEMD_USER_UNIT_DIR)/$(BINARY_NAME).service
	@chmod 644 $(SYSTEMD_USER_UNIT_DIR)/$(BINARY_NAME).service
	@# --- Desktop entry --------------------------------------------------------
	@mkdir -p $(USER_ICON_DIR)
	@install -Dm644 $(ICON_SRC) $(USER_ICON_DIR)/$(ICON_NAME)
	@mkdir -p $(USER_DESKTOP_DIR)
	@printf '%s\n' \
		'[Desktop Entry]' \
		'Name=gozik YouTube Music' \
		'Comment=YouTube Music plugin web console' \
		'Exec=$(USER_INSTALL_WRAPPER)' \
		'Type=Application' \
		'Terminal=false' \
		'Icon=$(USER_ICON_DIR)/$(ICON_NAME)' \
		'Categories=AudioVideo;Audio;Player;Network;' \
		'StartupNotify=true' \
		'StartupWMClass=gozik-yt-music' \
		> $(USER_DESKTOP_DIR)/gozik-yt-music-webui.desktop
	@chmod 644 $(USER_DESKTOP_DIR)/gozik-yt-music-webui.desktop
	@update-desktop-database $(USER_DESKTOP_DIR) 2>/dev/null || true
	@echo ""
	@echo "==> $(BINARY_NAME) installed to $(USER_INSTALL_DIR)"
	@echo "==> Wrapper script installed to $(USER_INSTALL_WRAPPER)"
	@echo "==> systemd user unit installed to $(SYSTEMD_USER_UNIT_DIR)/$(BINARY_NAME).service"
	@echo "==> Desktop entry installed to $(USER_DESKTOP_DIR)/gozik-yt-music-webui.desktop"
	@echo "==> Icon installed to $(USER_ICON_DIR)/$(ICON_NAME)"
	@echo ""
	@echo "    Import GUI env vars once (or add to ~/.profile / ~/.bashrc):"
	@echo "        systemctl --user import-environment DISPLAY XAUTHORITY WAYLAND_DISPLAY"
	@echo ""
	@echo "    Start now : systemctl --user enable --now $(BINARY_NAME).service"
	@echo "    Status    : systemctl --user status $(BINARY_NAME).service"

# -----------------------------------------------------------------------------
# Uninstall
# -----------------------------------------------------------------------------
uninstall:
	rm -rf $(DESTDIR)$(INSTALL_DIR)
	rm -f $(DESTDIR)$(INSTALL_WRAPPER)
	rm -f $(DESTDIR)$(SYSTEMD_SYSTEM_UNIT_DIR)/$(BINARY_NAME).service
	rm -f $(DESTDIR)$(DESKTOP_DIR)/gozik-yt-music-webui.desktop
	rm -f $(DESTDIR)$(ICON_DIR)/$(ICON_NAME)
	@update-desktop-database $(DESTDIR)$(DESKTOP_DIR) 2>/dev/null || true
	@echo "==> $(BINARY_NAME) uninstalled."

uninstall-user:
	rm -rf $(USER_INSTALL_DIR)
	rm -f $(USER_INSTALL_WRAPPER)
	rm -f $(SYSTEMD_USER_UNIT_DIR)/$(BINARY_NAME).service
	rm -f $(USER_DESKTOP_DIR)/gozik-yt-music-webui.desktop
	rm -f $(USER_ICON_DIR)/$(ICON_NAME)
	@update-desktop-database $(USER_DESKTOP_DIR) 2>/dev/null || true
	@echo "==> $(BINARY_NAME) user install uninstalled."

# -----------------------------------------------------------------------------
# Clean build artifacts
# -----------------------------------------------------------------------------
clean:
	rm -rf dist/
	rm -rf build/
	rm -rf certifi/
	rm -rf *.build/
	rm -rf *.onefile-build/
	find . -type d -name '__pycache__' -exec rm -rf {} + 2>/dev/null || true

distclean: clean
	rm -rf .venv/
	rm -rf .tools/
	rm -rf generated/
