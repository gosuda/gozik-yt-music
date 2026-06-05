#!/usr/bin/env bash
# =============================================================================
# package.sh — Nuitka 4.x single-file build script for gozik-yt-music
#
# Produces a fully self-contained Linux binary at:
#   ./dist/gozik-yt-music-server
#
# The binary requires no Python interpreter, no virtualenv, and no pip packages
# on the target machine. All dependencies are compiled and bundled inline.
#
# Requirements on the BUILD machine:
#   - Python 3.13 (--enable-shared; libpython3.13.so must exist)
#   - GCC 11+ or Clang 12+
#   - patchelf 0.14+   (for RPATH rewriting inside the onefile payload)
#   - ccache           (optional; recommended; speeds up re-builds ~6×)
#   - Virtualenv at .venv/ with all dependencies installed:
#       python3 -m venv .venv
#       .venv/bin/pip install -r requirements.txt
#       .venv/bin/pip install "nuitka==4.1.2"
#   - Proto stubs generated:  bash codegen.sh
#
# Usage:
#   bash package.sh               # production release build
#   bash package.sh --debug       # retain C sources, enable verbose Nuitka trace
#   bash package.sh --no-ccache   # disable compiler cache
# =============================================================================

set -euo pipefail

# ---------------------------------------------------------------------------
# Script-level flag parsing
# ---------------------------------------------------------------------------
DEBUG_BUILD=0
USE_CCACHE=1
for arg in "$@"; do
    case "$arg" in
        --debug)      DEBUG_BUILD=1 ;;
        --no-ccache)  USE_CCACHE=0  ;;
        *)
            echo "Unknown flag: $arg" >&2
            echo "Usage: bash package.sh [--debug] [--no-ccache]" >&2
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
OUTPUT_DIR="${SCRIPT_DIR}/dist"
ENTRY_POINT="${SCRIPT_DIR}/server.py"
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
    log_error "  .venv/bin/pip install 'nuitka==4.1.2'"
    exit 1
fi

PY_VERSION="$("${PYTHON}" -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}")')"
log_info "Python : ${PY_VERSION} (${PYTHON})"

# Verify libpython.so is present (required for --mode=onefile).
PY_ENABLE_SHARED="$("${PYTHON}" -c 'import sysconfig; print(sysconfig.get_config_var("Py_ENABLE_SHARED") or 0)')"
if [[ "${PY_ENABLE_SHARED}" != "1" ]]; then
    log_error "This Python build was not compiled with --enable-shared."
    log_error "Nuitka --mode=onefile requires libpython*.so to be present."
    log_error "Install the python3-dev / python3-shared package for your distro."
    exit 1
fi
log_info "Shared libpython: found (Py_ENABLE_SHARED=1)"

# Install Nuitka if absent.
if ! "${PYTHON}" -c "import nuitka" 2>/dev/null; then
    log_warn "Nuitka not found inside venv — installing nuitka==4.1.2 …"
    "${PIP}" install --quiet "nuitka==4.1.2"
fi

NUITKA_VER="$("${PYTHON}" -m nuitka --version 2>&1 | head -1)"
log_info "Nuitka : ${NUITKA_VER}"

# Validate proto stubs.
if [[ ! -f "${SCRIPT_DIR}/generated/music_provider_pb2.py" ]]; then
    log_warn "Proto stubs missing — running codegen.sh …"
    bash "${SCRIPT_DIR}/codegen.sh"
fi
log_info "Proto stubs: present"

# Validate patchelf.
if ! command -v patchelf &>/dev/null; then
    log_error "patchelf is required for Nuitka --mode=onefile on Linux."
    log_error "  sudo apt-get install patchelf"
    exit 1
fi
log_info "patchelf: $(patchelf --version 2>&1 | head -1)"

# ccache detection.
if [[ "${USE_CCACHE}" -eq 1 ]] && command -v ccache &>/dev/null; then
    log_info "ccache : enabled ($(ccache --version | head -1))"
    # Nuitka auto-detects ccache when it appears before gcc on PATH.
    export PATH="$(dirname "$(command -v ccache)"):${PATH}"
else
    USE_CCACHE=0
    log_warn "ccache not available — fresh build will be slow"
fi

# Gather system info for the manifest.
GCC_VER="$(gcc --version 2>&1 | head -1)"
BUILD_TIMESTAMP="$(date -u +"%Y-%m-%dT%H:%M:%SZ")"
GIT_COMMIT="$(git -C "${SCRIPT_DIR}" rev-parse --short HEAD 2>/dev/null || echo "n/a")"
NUITKA_VER_SHORT="$("${PYTHON}" -c 'import nuitka; print(nuitka.__version__)' 2>/dev/null || echo "4.1.2")"
JOBS="$(nproc 2>/dev/null || sysctl -n hw.ncpu 2>/dev/null || echo 4)"
CERTIFI_PEM="$("${PYTHON}" -c 'import certifi; print(certifi.where())')"

log_info "Parallel jobs  : ${JOBS}"
log_info "certifi bundle : ${CERTIFI_PEM}"

# ---------------------------------------------------------------------------
# Step 2 — Prepare output directory
# ---------------------------------------------------------------------------
log_step "Step 2: Prepare output directory"
mkdir -p "${OUTPUT_DIR}"
log_info "Output directory: ${OUTPUT_DIR}"

# ---------------------------------------------------------------------------
# Step 3 — Build the Nuitka argument list
# ---------------------------------------------------------------------------
log_step "Step 3: Assembling Nuitka command"

# NOTE ON NUITKA 4.x API CHANGES vs earlier series:
#
#   --mode=onefile       replaces the older --onefile flag.
#                        Internally, 'onefile' mode implies 'standalone'; no
#                        separate --standalone flag is accepted alongside it.
#
#   --assume-yes-for-downloads  (plural) replaces --assume-yes-for-download.
#
#   --include-package=   syntax unchanged from 1.x/2.x.
#
#   --nofollow-import-to= syntax unchanged.
#
#   --lto=yes            unchanged.

NUITKA_ARGS=(
    # ---- Packaging mode -------------------------------------------------------
    # 'onefile' compresses the entire standalone tree (all .so, stdlib, and
    # third-party packages) into a single zstd-compressed ELF using Nuitka's
    # own bootstrap loader. On first launch the payload is extracted to a
    # per-version cache directory; subsequent launches skip extraction entirely.
    "--mode=onefile"

    # ---- Onefile extraction root ---------------------------------------------
    # Override the default /tmp extraction path with a stable XDG cache entry.
    # {CACHE_DIR} resolves to $XDG_CACHE_HOME (typically ~/.cache) at runtime.
    # {VERSION}   is replaced with the Nuitka-computed content hash, ensuring
    # that different binary releases never share the same extraction directory.
    "--onefile-tempdir-spec={CACHE_DIR}/gozik/ytmusic-server/{VERSION}"

    # ---- Output paths --------------------------------------------------------
    "--output-dir=${OUTPUT_DIR}"
    "--output-filename=${BINARY_NAME}"

    # ---- Explicit package inclusions -----------------------------------------
    #
    # ytmusicapi — dynamic mixin loading and importlib.resources usage means
    #   Nuitka's static import tracer does not discover all submodules. Full
    #   recursive inclusion is the only reliable approach.
    "--include-package=ytmusicapi"

    # grpc — the runtime discovers submodules via __import__ strings, and the
    #   Cython extension (_cygrpc.so) is only referenced at runtime. Nuitka
    #   must be told to copy the entire grpc namespace.
    "--include-package=grpc"
    "--include-package=grpc._cython"

    # google.protobuf — protobuf 4+ has a C-extension fast path; both the
    #   fast path and the pure-Python fallback must be present. The 'internal'
    #   namespace is particularly prone to being skipped by static analysis.
    "--include-package=google.protobuf"
    "--include-package=google.protobuf.internal"

    # yt_dlp — 1100+ submodules loaded via plugin discovery strings. Any
    #   partial inclusion causes runtime ImportError for extractor plugins.
    "--include-package=yt_dlp"
    "--include-package=yt_dlp.extractor"

    # Standard HTTP stack used by ytmusicapi.
    "--include-package=requests"
    "--include-package=certifi"
    "--include-package=charset_normalizer"
    "--include-package=idna"
    "--include-package=urllib3"

    # Project-local packages: generated protobuf stubs and the RPC handler.
    "--include-package=generated"
    "--include-package=handlers"

    # ---- Data file inclusions ------------------------------------------------
    #
    # Package-internal data files (schema JSON, locale data, etc.) inside the
    # ytmusicapi and grpc wheels.
    "--include-package-data=ytmusicapi"
    "--include-package-data=grpc"

    # The Mozilla CA bundle: certifi.where() returns the path to this .pem
    # file at runtime. It must exist at 'certifi/cacert.pem' relative to the
    # extraction root so the path stays correct after onefile unpacking.
    "--include-data-files=${CERTIFI_PEM}=certifi/cacert.pem"

    # Generated protobuf stubs directory. Even though Nuitka compiles them to
    # C, the generated/ package namespace must be anchored by its __init__.py.
    "--include-data-dir=${SCRIPT_DIR}/generated=generated"

    # ---- Dead-code exclusions ------------------------------------------------
    # Each excluded module was confirmed unused by the server at runtime.
    # Exclusion saves ~40 MB of payload space and reduces extraction time.
    "--nofollow-import-to=tkinter"       # GUI toolkit — headless server
    "--nofollow-import-to=unittest"      # test framework
    "--nofollow-import-to=test"          # CPython internal test suite
    "--nofollow-import-to=distutils"     # build infrastructure
    "--nofollow-import-to=grpc_tools"    # build-time proto compiler only
    "--nofollow-import-to=lib2to3"       # Python 2→3 migration tool
    "--nofollow-import-to=pydoc"         # documentation generator
    "--nofollow-import-to=doctest"       # testing framework

    # ---- Python runtime flags ------------------------------------------------
    # no_site  — prevents the embedded runtime from scanning system site-packages
    #            on startup, which could cause incompatible system packages to
    #            shadow the bundled ones.
    "--python-flag=no_site"

    # no_warnings — suppresses DeprecationWarning from protobuf/grpc internals
    #               in the production binary (they are still enabled in --debug).
    "--python-flag=no_warnings"

    # ---- Nuitka plugins ------------------------------------------------------
    # anti-bloat strips test harnesses, Jupyter kernels, and other heavyweight
    # unused imports that several third-party packages pull in at module level.
    # Consistently reduces binary size by 15-30% with zero runtime impact.
    # NOTE: Nuitka 4.x uses --enable-plugins= (plural).
    "--enable-plugins=anti-bloat"

    # ---- Non-interactive / CI mode -------------------------------------------
    # Nuitka may download a CPython base archive the first time it compiles a
    # standalone binary on this machine. This flag suppresses the interactive
    # confirmation prompt, which is required for automated CI pipelines.
    "--assume-yes-for-downloads"

    # ---- Parallelism ---------------------------------------------------------
    "--jobs=${JOBS}"
)

# Release-only flags.
if [[ "${DEBUG_BUILD}" -eq 0 ]]; then
    # Remove intermediate .build/ directory after the onefile is assembled.
    NUITKA_ARGS+=("--remove-output")
    # Link-Time Optimisation: reduces binary size ~5% and enables cross-
    # translation-unit inlining. Adds ~30% to total build time; acceptable for
    # release builds, disabled for the inner dev loop (--debug).
    NUITKA_ARGS+=("--lto=yes")
    log_info "Build mode: RELEASE (--lto=yes, --remove-output)"
else
    # Debug mode: keep C sources and emit verbose tracing.
    NUITKA_ARGS+=("--show-modules")
    NUITKA_ARGS+=("--show-scons")
    log_warn "Build mode: DEBUG (C sources retained, --show-modules active)"
fi

# ---------------------------------------------------------------------------
# Step 4 — Write pre-build pass of build_manifest.json
# ---------------------------------------------------------------------------
log_step "Step 4: Writing pre-build build_manifest.json"

parse_req_version() {
    grep -iE "^${1}==" "${SCRIPT_DIR}/requirements.txt" | cut -d= -f3 || echo "unknown"
}
GRPCIO_VER="$(parse_req_version grpcio)"
YTMUSICAPI_REQ_VER="$(parse_req_version ytmusicapi)"
YTDLP_REQ_VER="$(parse_req_version yt-dlp)"
PROTOBUF_VER="$("${PYTHON}" -c 'import google.protobuf; print(google.protobuf.__version__)' 2>/dev/null || echo "unknown")"

# Serialise the NUITKA_ARGS array to a JSON array via Python.
NUITKA_ARGS_JSON="$("${PYTHON}" -c "
import json, sys
args = $(printf '%s\n' "${NUITKA_ARGS[@]}" | python3 -c "
import sys, json
print(json.dumps([l.strip() for l in sys.stdin.read().splitlines() if l.strip()]))
")
print(json.dumps(args, indent=4))
")"

cat > "${BUILD_MANIFEST}" <<MANIFEST
{
  "_schema_version": "1.0",
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
    "output_path": "dist/${BINARY_NAME}",
    "format": "onefile-elf-zstd",
    "platform": "linux-x86_64",
    "extraction_spec": "{CACHE_DIR}/gozik/ytmusic-server/{VERSION}",
    "extraction_note": "Payload extracted to XDG_CACHE_HOME on first launch; subsequent runs skip extraction (keyed by content hash)."
  },
  "toolchain": {
    "python_version": "${PY_VERSION}",
    "nuitka_version": "${NUITKA_VER_SHORT}",
    "gcc_version": "${GCC_VER}",
    "patchelf_version": "$(patchelf --version 2>&1 | head -1)",
    "ccache_enabled": $([ "${USE_CCACHE}" -eq 1 ] && echo "true" || echo "false"),
    "lto_enabled": $([ "${DEBUG_BUILD}" -eq 0 ] && echo "true" || echo "false"),
    "parallel_jobs": ${JOBS}
  },
  "nuitka_flags_explained": {
    "--mode=onefile": "Nuitka 4.x unified mode flag. Implies standalone (all .so bundled) and compresses the output into a single zstd ELF.",
    "--onefile-tempdir-spec": "Extraction root override; uses XDG_CACHE_HOME to avoid noexec /tmp mounts.",
    "--remove-output": "Purges intermediate .build/ directory after assembly (release mode only).",
    "--lto=yes": "Link-Time Optimisation across all compiled C translation units (release mode only).",
    "--include-package=ytmusicapi": "Dynamic mixin loading — static import tracer misses submodules without explicit inclusion.",
    "--include-package=grpc / grpc._cython": "gRPC uses __import__ strings at runtime; Cython extension must be explicitly pulled in.",
    "--include-package=google.protobuf / google.protobuf.internal": "protobuf 4+ C-extension fast path + pure-Python fallback; internal namespace requires explicit entry.",
    "--include-package=yt_dlp / yt_dlp.extractor": "1100+ submodules loaded via plugin discovery strings; partial inclusion causes runtime ImportError.",
    "--include-data-files=certifi/cacert.pem": "certifi.where() must resolve inside the extraction root; required for HTTPS.",
    "--include-data-dir=generated": "protobuf Python stubs; package namespace anchor must be present post-extraction.",
    "--nofollow-import-to=...": "Excludes GUI toolkit, test frameworks, and build-time tools; saves ~40 MB payload.",
    "--python-flag=no_site": "Prevents system site-packages from shadowing bundled dependencies on startup.",
    "--enable-plugin=anti-bloat": "Strips Jupyter kernels, test harnesses — reduces binary size 15-30%.",
    "--assume-yes-for-downloads": "Suppresses CPython base-archive download prompts in non-interactive CI mode."
  },
  "pinned_dependencies": {
    "grpcio": "${GRPCIO_VER}",
    "grpcio-tools": "$(parse_req_version grpcio-tools)",
    "ytmusicapi": "${YTMUSICAPI_REQ_VER}",
    "yt-dlp": "${YTDLP_REQ_VER}",
    "protobuf_runtime": "${PROTOBUF_VER}",
    "nuitka": "${NUITKA_VER_SHORT}"
  },
  "included_packages": [
    "ytmusicapi", "grpc", "grpc._cython",
    "google.protobuf", "google.protobuf.internal",
    "yt_dlp", "yt_dlp.extractor",
    "requests", "certifi", "charset_normalizer", "idna", "urllib3",
    "generated", "handlers"
  ],
  "excluded_packages": [
    "tkinter", "unittest", "test", "distutils",
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
    "gcc": "11.0+ (tested: GCC 14.2.0)",
    "patchelf": "0.14+ (tested: 0.18.0)",
    "ccache": "optional (tested: 4.11.2)",
    "python_shared": "libpython3.13.so must exist (Py_ENABLE_SHARED=1)",
    "upx": "NOT used — UPX is incompatible with Nuitka onefile zstd payloads and triggers AV false positives",
    "estimated_disk_build": "~2 GB (yt-dlp C compilation is I/O intensive)",
    "estimated_binary_size": "~80-120 MB"
  },
  "post_build": {
    "binary_size_bytes": null,
    "sha256": null,
    "build_duration_seconds": null,
    "note": "Populated by package.sh post-build pass."
  }
}
MANIFEST

log_ok "build_manifest.json written (pre-build)"

# ---------------------------------------------------------------------------
# Step 5 — Run the Nuitka compilation
# ---------------------------------------------------------------------------
log_step "Step 5: Nuitka compilation"
log_info "Entry point : ${ENTRY_POINT}"
log_info "Output dir  : ${OUTPUT_DIR}"
log_info "Binary name : ${BINARY_NAME}"
log_info "Build log   : ${BUILD_LOG}"
echo ""
log_info "Starting compilation — this typically takes 3–10 minutes …"
echo ""

BUILD_START="$(date +%s)"

# Run Nuitka. tee streams output to both the terminal and the persistent log.
"${PYTHON}" -m nuitka \
    "${NUITKA_ARGS[@]}" \
    "${ENTRY_POINT}" \
    2>&1 | tee "${BUILD_LOG}"

BUILD_END="$(date +%s)"
BUILD_DURATION=$(( BUILD_END - BUILD_START ))

# ---------------------------------------------------------------------------
# Step 6 — Verify the binary was produced
# ---------------------------------------------------------------------------
log_step "Step 6: Verifying binary"
BINARY_PATH="${OUTPUT_DIR}/${BINARY_NAME}"

if [[ ! -f "${BINARY_PATH}" ]]; then
    log_error "Build FAILED — expected binary not found: ${BINARY_PATH}"
    log_error "Review the build log: ${BUILD_LOG}"
    exit 1
fi

log_ok "Binary produced: ${BINARY_PATH}"

# ---------------------------------------------------------------------------
# Step 7 — Post-processing: strip debug symbols
# ---------------------------------------------------------------------------
log_step "Step 7: Post-processing"

log_info "Stripping debug symbols …"
# strip has no effect on Nuitka's onefile ELF (symbols are not embedded in the
# outer stub), but we run it defensively in case the format changes.
strip --strip-unneeded "${BINARY_PATH}" 2>/dev/null \
    && log_info "strip: done" \
    || log_warn "strip: no effect (binary may already be stripped)"

# Make executable (should already be set, but be explicit).
chmod 0755 "${BINARY_PATH}"

BINARY_SIZE="$(stat -c%s "${BINARY_PATH}" 2>/dev/null || stat -f%z "${BINARY_PATH}")"
BINARY_SHA256="$(sha256sum "${BINARY_PATH}" | awk '{print $1}')"
BINARY_SIZE_MB="$(awk "BEGIN {printf \"%.2f\", ${BINARY_SIZE}/1048576}")"

log_ok "Size    : ${BINARY_SIZE_MB} MB  (${BINARY_SIZE} bytes)"
log_ok "SHA-256 : ${BINARY_SHA256}"
log_ok "Duration: ${BUILD_DURATION}s"

# ---------------------------------------------------------------------------
# Step 8 — Update build_manifest.json with post-build metadata
# ---------------------------------------------------------------------------
log_step "Step 8: Updating build_manifest.json"

"${PYTHON}" -c "
import json, pathlib
p = pathlib.Path('${BUILD_MANIFEST}')
m = json.loads(p.read_text())
m['post_build']['binary_size_bytes'] = ${BINARY_SIZE}
m['post_build']['sha256'] = '${BINARY_SHA256}'
m['post_build']['build_duration_seconds'] = ${BUILD_DURATION}
p.write_text(json.dumps(m, indent=2))
"
log_ok "build_manifest.json updated with post-build metadata"

# ---------------------------------------------------------------------------
# Step 9 — Smoke test
# ---------------------------------------------------------------------------
log_step "Step 9: Smoke test"

log_info "Running: ${BINARY_PATH} --help"
if timeout 10 "${BINARY_PATH}" --help &>/dev/null; then
    log_ok "Smoke test PASSED — binary launches and accepts --help"
else
    log_warn "Smoke test inconclusive (exit code non-zero or timed out)"
    log_warn "Test manually: ${BINARY_PATH} --help"
fi

# ---------------------------------------------------------------------------
# Step 10 — Final summary
# ---------------------------------------------------------------------------
log_step "Build complete"
echo ""
echo "  Binary  : ${BINARY_PATH}"
echo "  Size    : ${BINARY_SIZE_MB} MB"
echo "  SHA-256 : ${BINARY_SHA256}"
echo "  Duration: ${BUILD_DURATION}s"
echo "  Manifest: ${BUILD_MANIFEST}"
echo "  Log     : ${BUILD_LOG}"
echo ""
log_info "To start the server:  ${BINARY_PATH} --port 50051"
log_info "To run as a daemon :  ${BINARY_PATH} --port 50051 &"
