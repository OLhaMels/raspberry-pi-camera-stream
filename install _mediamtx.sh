#!/usr/bin/env bash
# install_mediamtx.sh - download and install the latest MediaMTX release
# for this machine's OS/architecture, with checksum verification.
#
# Usage:
#   ./install_mediamtx.sh                # install ./mediamtx in the current directory
#   ./install_mediamtx.sh --dir PATH     # install into PATH instead
#   ./install_mediamtx.sh --system       # install to /usr/local/bin (uses sudo if needed)
#   ./install_mediamtx.sh --force        # reinstall even if already up to date
#
# Re-running it is safe and cheap: with a target already at the latest
# version, it does nothing beyond one HTTP request to check.

set -euo pipefail

REPO="bluenviron/mediamtx"
DEST_DIR="$(pwd)"
FORCE=0

usage() {
    cat <<'EOF'
Usage: install_mediamtx.sh [--dir PATH | --system] [--force]

  --dir PATH   Install the "mediamtx" binary into PATH (default: current directory)
  --system     Install to /usr/local/bin (uses sudo if needed)
  --force      Reinstall even if the target is already the latest version
  -h, --help   Show this help
EOF
}

while [ $# -gt 0 ]; do
    case "$1" in
        --dir)
            DEST_DIR="$2"
            shift 2
            ;;
        --system)
            DEST_DIR="/usr/local/bin"
            shift
            ;;
        --force)
            FORCE=1
            shift
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            echo "Unknown argument: $1" >&2
            usage >&2
            exit 1
            ;;
    esac
done

# ---- prerequisites -------------------------------------------------------

for cmd in curl tar sha256sum; do
    if ! command -v "$cmd" >/dev/null 2>&1; then
        echo "Missing required tool: $cmd" >&2
        exit 1
    fi
done

if [ "$(uname -s)" != "Linux" ]; then
    echo "This script only installs Linux builds (detected $(uname -s))." >&2
    exit 1
fi

case "$(uname -m)" in
    aarch64|arm64) ARCH="arm64" ;;   # Pi 3/4/5 running a 64-bit OS
    armv7l)        ARCH="armv7" ;;   # Pi 2/3/4 running a 32-bit OS
    armv6l)        ARCH="armv6" ;;   # Pi Zero/1
    x86_64)        ARCH="amd64" ;;
    *)
        echo "Unsupported architecture: $(uname -m)" >&2
        exit 1
        ;;
esac

# ---- find the latest version without touching GitHub's (rate-limited) API
#
# github.com/OWNER/REPO/releases/latest redirects to
# .../releases/tag/vX.Y.Z, so the redirect target alone gives us the
# version - one HEAD request, no API quota spent.

LOCATION=$(curl -fsSI "https://github.com/${REPO}/releases/latest" \
    | tr -d '\r' | awk 'tolower($1) == "location:" {print $2}')
VERSION="${LOCATION##*/tag/}"

if [ -z "$VERSION" ]; then
    echo "Could not determine the latest MediaMTX version - is github.com reachable?" >&2
    exit 1
fi

ASSET="mediamtx_${VERSION}_linux_${ARCH}.tar.gz"
BASE_URL="https://github.com/${REPO}/releases/download/${VERSION}"
TARGET="${DEST_DIR}/mediamtx"

echo "Latest MediaMTX release: ${VERSION} (linux/${ARCH})"

# ---- skip the download if the target is already current -----------------

if [ "$FORCE" -eq 0 ] && [ -x "$TARGET" ]; then
    CURRENT=$("$TARGET" --version 2>/dev/null || true)
    if [ "$CURRENT" = "$VERSION" ]; then
        echo "${TARGET} is already ${VERSION} - nothing to do (use --force to reinstall)."
        exit 0
    fi
fi

# ---- download, verify, extract -------------------------------------------

WORKDIR=$(mktemp -d)
trap 'rm -rf "$WORKDIR"' EXIT

echo "Downloading ${ASSET}..."
curl -fL --progress-bar -o "${WORKDIR}/${ASSET}" "${BASE_URL}/${ASSET}"
curl -fsSL -o "${WORKDIR}/checksums.sha256" "${BASE_URL}/checksums.sha256"

echo "Verifying checksum..."
if ! ( cd "$WORKDIR" && grep "${ASSET}\$" checksums.sha256 | sha256sum --check --status ); then
    echo "Checksum verification failed - aborting, nothing installed." >&2
    exit 1
fi

echo "Extracting..."
# Pull just the binary out of the tarball - it also contains a sample
# mediamtx.yml and a LICENSE file that would otherwise land in $WORKDIR
# (and that you don't want clobbering a config you've already written).
tar xzf "${WORKDIR}/${ASSET}" -C "$WORKDIR" mediamtx

# ---- install ---------------------------------------------------------------

mkdir -p "$DEST_DIR"
if [ -w "$DEST_DIR" ]; then
    mv "${WORKDIR}/mediamtx" "$TARGET"
    chmod +x "$TARGET"
else
    echo "No write access to ${DEST_DIR} - using sudo."
    sudo mv "${WORKDIR}/mediamtx" "$TARGET"
    sudo chmod +x "$TARGET"
fi

echo "Installed MediaMTX ${VERSION} to ${TARGET}"
"$TARGET" --version