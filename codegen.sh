#!/usr/bin/env bash
# codegen.sh — Compile .proto definitions into the generated/ module.
#
# Usage: run from the gozik-yt-music/ directory:
#   bash codegen.sh
#
# The script resolves the source proto files from the sibling gozik/ repository
# at ../gozik/api/music/v1/ and writes the generated Python stubs into
# ./generated/ so they can be imported as `generated.music_provider_pb2` etc.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROTO_SOURCE_DIR="${SCRIPT_DIR}/../gozik/api/music/v1"
OUTPUT_DIR="${SCRIPT_DIR}/generated"
GOOGLEAPIS_DIR="${SCRIPT_DIR}/third_party/googleapis"

# ---------------------------------------------------------------------------
# Validate that the proto source directory exists.
# ---------------------------------------------------------------------------
if [[ ! -d "${PROTO_SOURCE_DIR}" ]]; then
    echo "ERROR: proto source directory not found: ${PROTO_SOURCE_DIR}" >&2
    exit 1
fi

# ---------------------------------------------------------------------------
# Ensure the output directory exists and is a proper Python package.
# ---------------------------------------------------------------------------
mkdir -p "${OUTPUT_DIR}"
touch "${OUTPUT_DIR}/__init__.py"

# ---------------------------------------------------------------------------
# Download googleapis well-known protos (timestamp.proto) if not already
# present. provider_link.proto imports google/protobuf/timestamp.proto which
# is bundled with grpcio-tools, so we only need to point the include path at
# the grpcio-tools installation.
# ---------------------------------------------------------------------------
GRPC_TOOLS_PROTO_PATH=$(python3 -c "
import grpc_tools, os
print(os.path.join(os.path.dirname(grpc_tools.__file__), '_proto'))
")

echo "Proto source  : ${PROTO_SOURCE_DIR}"
echo "Output dir    : ${OUTPUT_DIR}"
echo "grpcio-tools  : ${GRPC_TOOLS_PROTO_PATH}"

# ---------------------------------------------------------------------------
# Compile music_provider.proto
# ---------------------------------------------------------------------------
python3 -m grpc_tools.protoc \
    --proto_path="${PROTO_SOURCE_DIR}" \
    --proto_path="${GRPC_TOOLS_PROTO_PATH}" \
    --python_out="${OUTPUT_DIR}" \
    --grpc_python_out="${OUTPUT_DIR}" \
    "${PROTO_SOURCE_DIR}/music_provider.proto"

echo "Compiled: music_provider.proto"

# ---------------------------------------------------------------------------
# Compile provider_link.proto (imports google/protobuf/timestamp.proto)
# ---------------------------------------------------------------------------
python3 -m grpc_tools.protoc \
    --proto_path="${PROTO_SOURCE_DIR}" \
    --proto_path="${GRPC_TOOLS_PROTO_PATH}" \
    --python_out="${OUTPUT_DIR}" \
    --grpc_python_out="${OUTPUT_DIR}" \
    "${PROTO_SOURCE_DIR}/provider_link.proto"

echo "Compiled: provider_link.proto"

# ---------------------------------------------------------------------------
# Fix relative imports in generated files so they resolve correctly when
# the generated/ directory is imported as a package.
# grpc_tools emits bare `import music_provider_pb2` style imports; we
# convert them to relative package imports (`from generated import ...`).
# ---------------------------------------------------------------------------
for f in "${OUTPUT_DIR}"/*_pb2_grpc.py; do
    sed -i 's/^import \(.*_pb2\) as/from generated import \1 as/' "${f}"
done

echo ""
echo "Code generation complete. Generated files:"
ls -1 "${OUTPUT_DIR}"
