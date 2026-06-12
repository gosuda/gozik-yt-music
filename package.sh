#!/usr/bin/env bash
# =============================================================================
# package.sh — PyInstaller onedir build script for gozik-yt-music
#
# Produces a self-contained Linux application directory at:
#   ./dist/gozik-yt-music-server/
#
# The bundle contains the Python interpreter, all bytecode / extension modules,
# and runtime data files. Unlike Nuitka, no C translation of Python code is
# performed, which avoids the opaque crashes that sometimes result from
# Python→C compilation of large dynamic packages (yt-dlp, grpc, protobuf).
#
# Requirements on the BUILD machine:
#   - Python 3.11+ (3.13 recommended)
#   - Virtualenv at .venv/ with all dependencies installed:
#       python3 -m venv .venv
#       .venv/bin/pip install -r requirements.txt
#   - Proto stubs generated:  bash codegen.sh
#
# Usage:
#   bash package.sh               # production build
#   bash package.sh --debug       # PyInstaller debug / console traces
# =============================================================================

set -euo pipefail

# ---------------------------------------------------------------------------
# Script-level flag parsing
# ---------------------------------------------------------------------------
DEBUG_BUILD=0
for arg in "$@"; do
    case "$arg" in
        --debug) DEBUG_BUILD=1 ;;
        *)
            echo "Unknown flag: $arg" >&2
            echo "Usage: bash package.sh [--debug]" >&2
            exit 1
            ;;
    esac
done

# ---------------------------------------------------------------------------
# Path constants
# ---------------------------------------------------------------------------
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="${SCRIPT_DIR}/.venv"
PYTHON="${VENV_DIR}/bin/python3"
PIP="${VENV_DIR}/bin/pip"
PYINSTALLER="${VENV_DIR}/bin/pyinstaller"
OUTPUT_DIR="dist"
ENTRY_POINT="server.py"
BINARY_NAME="gozik-yt-music-server"
BUILD_LOG="${OUTPUT_DIR}/build.log"
BUILD_MANIFEST="${SCRIPT_DIR}/build_manifest.json"

# ---------------------------------------------------------------------------
# Colour helpers (suppressed when stdout is not a tty)
# ---------------------------------------------------------------------------
if [ -t 1 ]; then
    RED='\033[0;31m'; GRN='\033[0;32m'; YLW='\033[0;33m'
    BLU='\033[0;34m'; CYN='\033[0;36m'; NC='\033[0m'
else
    RED=''; GRN=''; YLW=''; BLU=''; CYN=''; NC=''
fi

log_info()  { echo -e "${BLU}[INFO]${NC}  $*"; }
log_ok()    { echo -e "${GRN}[ OK ]${NC}  $*"; }
log_warn()  { echo -e "${YLW}[WARN]${NC}  $*"; }
log_error() { echo -e "${RED}[ERR ]${NC}  $*" >&2; }
log_step()  { echo -e "\n${CYN}══ $* ${NC}"; }

# ---------------------------------------------------------------------------
# Step 1 — Environment validation
# ---------------------------------------------------------------------------
log_step "Step 1: Environment validation"

if [[ ! -f "${PYTHON}" ]]; then
    log_error "Virtualenv Python not found at ${PYTHON}"
    log_error "Create it with:"
    log_error "  python3 -m venv .venv"
    log_error "  .venv/bin/pip install -r requirements.txt"
    exit 1
fi

PY_VERSION="$("${PYTHON}" -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}")')"
log_info "Python : ${PY_VERSION} (${PYTHON})"

# Install PyInstaller if absent.
if ! "${PYTHON}" -c "import PyInstaller" 2>/dev/null; then
    log_warn "PyInstaller not found inside venv — installing ..."
    "${PIP}" install --quiet pyinstaller
fi

PYINST_VER="$("${PYTHON}" -c 'import PyInstaller; print(PyInstaller.__version__)' 2>/dev/null || echo "unknown")"
log_info "PyInstaller : ${PYINST_VER}"

# Validate proto stubs.
if [[ ! -f "${SCRIPT_DIR}/generated/music_provider_pb2.py" ]]; then
    log_warn "Proto stubs missing — running codegen.sh ..."
    bash "${SCRIPT_DIR}/codegen.sh"
fi
log_info "Proto stubs: present"

# Gather system info for the manifest.
BUILD_TIMESTAMP="$(date -u +"%Y-%m-%dT%H:%M:%SZ")"
GIT_COMMIT="$(git -C "${SCRIPT_DIR}" rev-parse --short HEAD 2>/dev/null || echo "n/a")"
GIT_TAG="$(git -C "${SCRIPT_DIR}" describe --tags --always 2>/dev/null || true)"
if [[ "$GIT_TAG" =~ ^v?([0-9]+)(\.[0-9]+)?(\.[0-9]+)?(\.[0-9]+)? ]]; then
    MAJOR="${BASH_REMATCH[1]}"
    MINOR="${BASH_REMATCH[2]:-.0}"
    PATCH="${BASH_REMATCH[3]:-.0}"
    BUILD="${BASH_REMATCH[4]:-.0}"
    VERSION="${MAJOR}${MINOR}${PATCH}${BUILD}"
else
    VERSION="0.0.0.0"
fi
JOBS="$(nproc 2>/dev/null || sysctl -n hw.ncpu 2>/dev/null || echo 4)"
CERTIFI_PEM="$("${PYTHON}" -c 'import certifi; print(certifi.where())')"

BUILD_OS_RAW="$(uname -s | tr '[:upper:]' '[:lower:]')"
BUILD_ARCH_RAW="$(uname -m)"
case "${BUILD_ARCH_RAW}" in
    amd64) BUILD_ARCH="x86_64" ;;
    aarch64) BUILD_ARCH="arm64" ;;
    *) BUILD_ARCH="${BUILD_ARCH_RAW}" ;;
esac
PLATFORM="${BUILD_OS_RAW}-${BUILD_ARCH}"

log_info "Parallel jobs  : ${JOBS}"
log_info "Platform       : ${PLATFORM}"
log_info "certifi bundle : ${CERTIFI_PEM}"

# ---------------------------------------------------------------------------
# Step 2 — Ensure latest yt-dlp nightly
# ---------------------------------------------------------------------------
log_step "Step 2: Updating yt-dlp to latest nightly"

if [[ "${YTDLP_SKIP_NIGHTLY:-0}" == "1" ]]; then
    log_info "YTDLP_SKIP_NIGHTLY=1 — using existing yt-dlp (stable)"
else
    log_info "Installing latest yt-dlp nightly via pip ..."
    "${PIP}" install -U --pre yt-dlp
fi

YTDLP_ACTUAL_VER="$("${PYTHON}" -c 'import yt_dlp.version; print(yt_dlp.version.__version__)' 2>/dev/null || echo "unknown")"
log_info "yt-dlp version: ${YTDLP_ACTUAL_VER}"

# ---------------------------------------------------------------------------
# Step 3 — Prepare output directory and certifi copy
# ---------------------------------------------------------------------------
log_step "Step 3: Prepare output directory"
mkdir -p "${OUTPUT_DIR}"
log_info "Output directory: ${OUTPUT_DIR}"

# certifi PEM lives inside .venv; copy it locally so PyInstaller bundles it
# from a relative path instead of an absolute build-machine path.
mkdir -p certifi
cp "${CERTIFI_PEM}" certifi/cacert.pem

# ---------------------------------------------------------------------------
# Download Node.js standalone binary for yt-dlp JS challenges
# ---------------------------------------------------------------------------
NODE_OS="$(uname -s | tr '[:upper:]' '[:lower:]')"
NODE_ARCH="$(uname -m)"
NODE_URL=""
case "${NODE_ARCH}" in
    x86_64|amd64) NODE_ARCH="x64" ;;
    aarch64|arm64) NODE_ARCH="arm64" ;;
    *)
        log_warn "Unsupported architecture: ${NODE_ARCH} — Node.js download skipped"
        ;;
esac

if [[ -n "${NODE_URL}" ]]; then
    NODE_URL="https://nodejs.org/dist/v22.14.0/node-v22.14.0-${NODE_OS}-${NODE_ARCH}.tar.xz"
    if [[ ! -x "${SCRIPT_DIR}/.tools/bin/node" ]]; then
        log_step "Downloading Node.js standalone binary (${NODE_OS}-${NODE_ARCH})"
        mkdir -p "${SCRIPT_DIR}/.tools"
        if curl -fsSL "${NODE_URL}" | tar -xJ --strip-components=1 -C "${SCRIPT_DIR}/.tools"; then
            log_ok "Node.js downloaded to ${SCRIPT_DIR}/.tools"
        else
            log_warn "Node.js download failed — yt-dlp may not be able to solve JS challenges"
        fi
    else
        log_info "Node.js already present at ${SCRIPT_DIR}/.tools/bin/node"
    fi
fi

# ---------------------------------------------------------------------------
# Step 4 — Build the PyInstaller argument list
# ---------------------------------------------------------------------------
log_step "Step 4: Assembling PyInstaller command"

PYINST_ARGS=(
    # ---- Entry point --------------------------------------------------------
    "${ENTRY_POINT}"

    # ---- Naming & layout ----------------------------------------------------
    "--name=${BINARY_NAME}"
    "--onedir"
    "--distpath=${OUTPUT_DIR}"
    "--workpath=build"

    # ---- Hidden imports -----------------------------------------------------
    # These packages perform dynamic/runtime imports that PyInstaller's
    # bytecode analysis cannot follow statically.
    "--hidden-import=ytmusicapi"
    "--hidden-import=grpc"
    "--hidden-import=grpc._cython"
    "--hidden-import=grpc._cython.cygrpc"
    "--hidden-import=google.protobuf"
    "--hidden-import=google.protobuf.internal"
    "--hidden-import=yt_dlp"
    "--hidden-import=yt_dlp.extractor"
    "--hidden-import=yt_dlp.extractor.youtubetab"
    "--hidden-import=yt_dlp.extractor.youtube"
    "--hidden-import=requests"
    "--hidden-import=certifi"
    "--hidden-import=charset_normalizer"
    "--hidden-import=idna"
    "--hidden-import=urllib3"
    "--hidden-import=generated"
    "--hidden-import=handlers"

    # ---- Collect full packages ----------------------------------------------
    # Ensures every submodule and data file is pulled in for packages that
    # discover resources at runtime.
    "--collect-all=ytmusicapi"
    "--collect-all=grpc"
    "--collect-all=google.protobuf"
    "--collect-all=yt_dlp"
    "--collect-all=certifi"

    # ---- Data files ---------------------------------------------------------
    "--add-data=generated:generated"
    "--add-data=certifi/cacert.pem:certifi"

    # ---- Binaries -----------------------------------------------------------
    # Node.js is required by yt-dlp to solve YouTube JS challenges.
    "--add-binary=.tools/bin/node:."

    # ---- Exclude unused large packages --------------------------------------
    "--exclude-module=tkinter"
    "--exclude-module=unittest"
    "--exclude-module=test"
    "--exclude-module=grpc_tools"
    "--exclude-module=lib2to3"
    "--exclude-module=pydoc"
    "--exclude-module=doctest"

    # ---- Non-interactive / clean build --------------------------------------
    "--noconfirm"
    "--clean"
)

if [[ "${DEBUG_BUILD}" -eq 1 ]]; then
    PYINST_ARGS+=("--debug=all")
    PYINST_ARGS+=("--console")
    log_warn "Build mode: DEBUG (PyInstaller debug traces enabled)"
else
    log_info "Build mode: RELEASE"
fi

# ---------------------------------------------------------------------------
# Step 5 — Write pre-build pass of build_manifest.json
# ---------------------------------------------------------------------------
log_step "Step 5: Writing pre-build build_manifest.json"

parse_req_version() {
    grep -iE "^${1}==" "${SCRIPT_DIR}/requirements.txt" | cut -d= -f3 || echo "unknown"
}
GRPCIO_VER="$(parse_req_version grpcio)"
YTMUSICAPI_REQ_VER="$(parse_req_version ytmusicapi)"
YTDLP_REQ_VER="$(parse_req_version yt-dlp)"
PROTOBUF_VER=$("${PYTHON}" -c 'import google.protobuf; print(google.protobuf.__version__)' 2>/dev/null || echo "unknown")

cat > "${BUILD_MANIFEST}" <<MANIFEST
{
  "_schema_version": "1.1",
  "_comment": "Build configuration tracker for gozik-yt-music. Auto-updated by package.sh.",
  "build_timestamp": "${BUILD_TIMESTAMP}",
  "git_commit": "${GIT_COMMIT}",
  "build_host": "$(hostname)",
  "build_os": "$(uname -srm)",
  "project": {
    "name": "gozik-yt-music-server",
    "description": "YouTube Music gRPC plugin server for the gozik desktop player",
    "entry_point": "server.py",
    "grpc_service": "MusicProviderService",
    "bind_address": "127.0.0.1:50051",
    "proto_source": "../gozik/api/music/v1/music_provider.proto"
  },
  "binary": {
    "name": "${BINARY_NAME}",
    "output_path": "dist/${BINARY_NAME}/${BINARY_NAME}",
    "output_directory": "dist/${BINARY_NAME}",
    "format": "pyinstaller-onedir",
    "platform": "${PLATFORM}",
    "note": "Self-contained directory produced by PyInstaller. Bundles the Python interpreter, bytecode, extension modules, and data files without translating Python to C."
  },
  "toolchain": {
    "python_version": "${PY_VERSION}",
    "pyinstaller_version": "${PYINST_VER}",
    "parallel_jobs": ${JOBS}
  },
  "pyinstaller_flags_explained": {
    "--onedir": "Produces an application directory instead of a single file. Faster startup and easier debugging than --onefile.",
    "--hidden-import=ytmusicapi / grpc / yt_dlp / google.protobuf": "Dynamic mixin loading and runtime __import__ strings cannot be detected by static analysis.",
    "--collect-all=PACKAGE": "Recursively copies every submodule and package data file into the bundle.",
    "--add-data=generated:generated": "Project protobuf stubs are included as a package directory.",
    "--add-data=certifi/cacert.pem:certifi": "Mozilla CA bundle required for HTTPS by ytmusicapi and yt-dlp.",
    "--exclude-module=...": "Excludes GUI toolkit, test frameworks, and build-time tools to reduce bundle size.",
    "--noconfirm": "Suppresses interactive prompts, required for CI/automated builds.",
    "--clean": "Removes PyInstaller's temporary build/ directory before building."
  },
  "pinned_dependencies": {
    "grpcio": "${GRPCIO_VER}",
    "grpcio-tools": "$(parse_req_version grpcio-tools)",
    "ytmusicapi": "${YTMUSICAPI_REQ_VER}",
    "yt-dlp": "${YTDLP_ACTUAL_VER}",
    "yt-dlp_pinned_in_requirements": "${YTDLP_REQ_VER}",
    "protobuf_runtime": "${PROTOBUF_VER}",
    "pyinstaller": "${PYINST_VER}"
  },
  "included_packages": [
    "ytmusicapi", "grpc", "grpc._cython",
    "google.protobuf", "google.protobuf.internal",
    "yt_dlp", "yt_dlp.extractor",
    "requests", "certifi", "charset_normalizer", "idna", "urllib3",
    "generated", "handlers"
  ],
  "excluded_packages": [
    "tkinter", "unittest", "test",
    "grpc_tools", "lib2to3", "pydoc", "doctest"
  ],
  "data_files": [
    {
      "source": "${CERTIFI_PEM}",
      "destination": "certifi/cacert.pem",
      "description": "Mozilla CA certificate bundle; required for HTTPS by ytmusicapi and yt-dlp"
    },
    {
      "source": "generated/",
      "destination": "generated/",
      "description": "Compiled protobuf stubs for music_provider and provider_link services"
    }
  ],
  "build_requirements": {
    "python": "3.11+ (3.13 recommended)",
    "upx": "NOT used — UPX triggers AV false positives and is unnecessary for onedir bundles",
    "estimated_disk_build": "~500 MB",
    "estimated_bundle_size": "~150-250 MB"
  },
  "post_build": {
    "bundle_size_bytes": null,
    "sha256": null,
    "build_duration_seconds": null,
    "note": "Populated by package.sh post-build pass."
  }
}
MANIFEST

log_ok "build_manifest.json written (pre-build)"

# ---------------------------------------------------------------------------
# Step 6 — Run the PyInstaller build
# ---------------------------------------------------------------------------
log_step "Step 6: PyInstaller build"
log_info "Entry point : ${ENTRY_POINT}"
log_info "Output dir  : ${OUTPUT_DIR}"
log_info "Binary name : ${BINARY_NAME}"
log_info "Build log   : ${BUILD_LOG}"
echo ""
log_info "Starting build — this typically takes 1–3 minutes ..."
echo ""

BUILD_START="$(date +%s)"

# Run PyInstaller from the project root so every path is relative.
cd "${SCRIPT_DIR}"
"${PYINSTALLER}" "${PYINST_ARGS[@]}" 2>&1 | tee "${BUILD_LOG}"

BUILD_END="$(date +%s)"
BUILD_DURATION=$(( BUILD_END - BUILD_START ))

# ---------------------------------------------------------------------------
# Step 7 — Verify the bundle was produced
# ---------------------------------------------------------------------------
log_step "Step 7: Verifying bundle"
BUNDLE_DIR="${OUTPUT_DIR}/${BINARY_NAME}"
BUNDLE_BINARY="${BUNDLE_DIR}/${BINARY_NAME}"

if [[ ! -x "${BUNDLE_BINARY}" ]]; then
    log_error "Build FAILED — expected binary not found: ${BUNDLE_BINARY}"
    log_error "Review the build log: ${BUILD_LOG}"
    exit 1
fi

log_ok "Bundle produced: ${BUNDLE_DIR}"

# Make sure the binary is executable.
chmod 0755 "${BUNDLE_BINARY}"

# Compute bundle size (entire directory).
BUNDLE_SIZE_BYTES="$(du -sb "${BUNDLE_DIR}" | awk '{print $1}')"
BUNDLE_SIZE_MB="$(awk "BEGIN {printf \"%.2f\", ${BUNDLE_SIZE_BYTES}/1048576}")"
BUNDLE_SHA256=$(find "${BUNDLE_DIR}" -type f -print0 | sort -z | xargs -0 sha256sum | sha256sum | awk '{print $1}')

log_ok "Size    : ${BUNDLE_SIZE_MB} MB  (${BUNDLE_SIZE_BYTES} bytes)"
log_ok "SHA-256 : ${BUNDLE_SHA256}"
log_ok "Duration: ${BUILD_DURATION}s"

# ---------------------------------------------------------------------------
# Step 8 — Update build_manifest.json with post-build metadata
# ---------------------------------------------------------------------------
log_step "Step 8: Updating build_manifest.json"

"${PYTHON}" -c "
import json, pathlib
p = pathlib.Path('${BUILD_MANIFEST}')
m = json.loads(p.read_text())
m['post_build']['bundle_size_bytes'] = ${BUNDLE_SIZE_BYTES}
m['post_build']['sha256'] = '${BUNDLE_SHA256}'
m['post_build']['build_duration_seconds'] = ${BUILD_DURATION}
p.write_text(json.dumps(m, indent=2))
"
log_ok "build_manifest.json updated with post-build metadata"

# ---------------------------------------------------------------------------
# Step 9 — Smoke test
# ---------------------------------------------------------------------------
log_step "Step 9: Smoke test"

log_info "Running: ${BUNDLE_BINARY} --help"
if timeout 10 "${BUNDLE_BINARY}" --help &>/dev/null; then
    log_ok "Smoke test PASSED — bundle launches and accepts --help"
else
    log_warn "Smoke test inconclusive (exit code non-zero or timed out)"
    log_warn "Test manually: ${BUNDLE_BINARY} --help"
fi

# ---------------------------------------------------------------------------
# Step 10 — Final summary
# ---------------------------------------------------------------------------
log_step "Build complete"
echo ""
echo "  Bundle  : ${BUNDLE_DIR}"
echo "  Binary  : ${BUNDLE_BINARY}"
echo "  Size    : ${BUNDLE_SIZE_MB} MB"
echo "  SHA-256 : ${BUNDLE_SHA256}"
echo "  Duration: ${BUILD_DURATION}s"
echo "  Manifest: ${BUILD_MANIFEST}"
echo "  Log     : ${BUILD_LOG}"
echo ""
log_info "To start the server:  ${BUNDLE_BINARY} --port 50051"
log_info "To install system-wide:  sudo make install"
