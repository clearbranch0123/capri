#!/usr/bin/env bash
# Capri AI Companion — Clean Open Beta Release Builder (zero .git, zero secrets, verified checksums)
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$ROOT_DIR"

PYTHON_BIN="${ROOT_DIR}/.venv/bin/python"
if [ ! -x "$PYTHON_BIN" ]; then
    PYTHON_BIN="python3"
fi

VERSION="$("$PYTHON_BIN" -c 'import capri; print(capri.__version__)')"
DIST_DIR="${ROOT_DIR}/dist"
mkdir -p "$DIST_DIR"

# Remove stale release artifacts from previous versions so dist/ stays clean
rm -f "${DIST_DIR}"/capri-*-closed-beta.tar.gz "${DIST_DIR}"/capri-*-open-beta.tar.gz "${DIST_DIR}"/capri-*.whl "${DIST_DIR}"/SHA256SUMS.txt

TARBALL="${DIST_DIR}/capri-${VERSION}-open-beta.tar.gz"
TMP_EXPORT="$(mktemp -d)"
trap 'rm -rf "$TMP_EXPORT"' EXIT

echo "=== CAPRI // BUILDING CLEAN OPEN BETA RELEASE v${VERSION} ==="

# 1. Export tracked source tree via git archive (never includes .git, .env, personal config.yaml, .venv)
git archive --format=tar HEAD | tar -x -C "$TMP_EXPORT"

# Remove local runtime files and any optional maintainer-only paths listed in .releaseignore
rm -f \
    "$TMP_EXPORT/config.yaml" \
    "$TMP_EXPORT/.env" \
    "$TMP_EXPORT/.releaseignore"
if [ -f "${ROOT_DIR}/.releaseignore" ]; then
    "$PYTHON_BIN" - "$TMP_EXPORT" "${ROOT_DIR}/.releaseignore" <<'PY'
import shutil
import sys
from pathlib import Path

export_root = Path(sys.argv[1]).resolve()
ignore_file = Path(sys.argv[2])

for line_no, raw in enumerate(ignore_file.read_text(encoding="utf-8").splitlines(), 1):
    entry = raw.split("#", 1)[0].strip()
    if not entry:
        continue
    rel = Path(entry)
    if rel.is_absolute() or entry.startswith("/") or ".." in rel.parts or entry in {".", "./"}:
        raise SystemExit(f"ERROR: Unsafe .releaseignore entry on line {line_no}: {entry!r}")

    # Walk segments without following symlinks outside export_root
    cur = export_root
    for part in rel.parts:
        if part in {"", "."}:
            continue
        cur = cur / part
        if cur.is_symlink():
            if cur == export_root / rel:
                break
            raise SystemExit(f"ERROR: Symlink traversal in .releaseignore on line {line_no}: {entry!r}")

    if cur == export_root:
        raise SystemExit(f"ERROR: .releaseignore entry targets export root on line {line_no}: {entry!r}")

    resolved_parent = cur.parent.resolve(strict=False)
    if resolved_parent != export_root and export_root not in resolved_parent.parents:
        raise SystemExit(f"ERROR: .releaseignore entry escapes export root on line {line_no}: {entry!r}")

    if cur.is_symlink() or cur.is_file():
        cur.unlink()
    elif cur.is_dir():
        shutil.rmtree(cur)
PY
fi

STAGE_DIR="$(mktemp -d)"
trap 'rm -rf "$TMP_EXPORT" "$STAGE_DIR"' EXIT
mv "$TMP_EXPORT" "$STAGE_DIR/capri-${VERSION}"
tar -czf "$TARBALL" -C "$STAGE_DIR" "capri-${VERSION}"
echo "[1/3] Source bundle created: ${TARBALL}"

# 2. Build clean wheel package directly from the sterile exported source tree
rm -f "${DIST_DIR}"/capri-"${VERSION}"-*.whl
WHEEL_SRC="${STAGE_DIR}/capri-${VERSION}"
if command -v uv >/dev/null 2>&1; then
    uv build --wheel --out-dir "$DIST_DIR" "$WHEEL_SRC" >/dev/null
elif "$PYTHON_BIN" -m build --version >/dev/null 2>&1; then
    "$PYTHON_BIN" -m build --wheel --no-isolation --outdir "$DIST_DIR" "$WHEEL_SRC" >/dev/null
elif "$PYTHON_BIN" -m pip --version >/dev/null 2>&1; then
    "$PYTHON_BIN" -m pip wheel --no-deps --no-build-isolation --wheel-dir "$DIST_DIR" "$WHEEL_SRC" >/dev/null
else
    echo "Error: neither uv, python -m build, nor pip found to build wheel." >&2
    exit 1
fi
WHEEL_PATH="$(ls -1 "${DIST_DIR}"/capri-"${VERSION}"-*.whl | head -n 1)"
echo "[2/3] Wheel package created: ${WHEEL_PATH}"

# 3. Strict hygiene audit on both release artifacts
"$PYTHON_BIN" - "$TARBALL" "$WHEEL_PATH" "$ROOT_DIR" <<'PY'
import os
import re
import sys
import tarfile
import zipfile
from pathlib import Path

tar_path, whl_path, root_dir = sys.argv[1], sys.argv[2], Path(sys.argv[3])
forbidden_names = {
    ".git",
    ".env",
    "config.yaml",
    ".releaseignore",
}
release_ignore = root_dir / ".releaseignore"
if release_ignore.is_file():
    for raw_line in release_ignore.read_text(encoding="utf-8").splitlines():
        clean = raw_line.split("#", 1)[0].strip()
        if clean:
            forbidden_names.add(Path(clean).name)
secret_re = re.compile(
    rb"(?<![A-Za-z0-9_])("
    rb"sk-[A-Za-z0-9_\-]{16,}|"
    rb"gsk_[A-Za-z0-9_\-]{16,}|"
    rb"AIza[0-9A-Za-z_\-]{20,}|"
    rb"ghp_[A-Za-z0-9]{20,}|"
    rb"github_pat_[A-Za-z0-9_]{20,}|"
    rb"glpat-[A-Za-z0-9_\-]{16,}|"
    rb"xox[baprs]-[A-Za-z0-9\-]{14,}"
    rb")\b"
)

tar_files = set()
with tarfile.open(tar_path, "r:gz") as tf:
    for m in tf.getmembers():
        parts = set(m.name.split("/"))
        hit = parts & forbidden_names
        assert not hit, f"Forbidden path in tarball: {m.name} ({hit})"
        if m.isfile():
            rel = m.name.split("/", 1)[1] if "/" in m.name else m.name
            tar_files.add(rel)
            if "/tests/" not in m.name and m.size < 2_000_000 and not m.name.endswith((".png", ".jpg", ".webp", ".wav", ".ttf", ".otf")):
                data = tf.extractfile(m).read()
                if secret_re.search(data):
                    raise AssertionError(f"Potential API key pattern found in tarball file: {m.name}")

required_tar_docs = {"LICENSE", "ASSETS_NOTICE.md", "SECURITY.md", "README.md", "setup.sh"}
missing_docs = sorted(required_tar_docs - tar_files)
assert not missing_docs, f"Tarball is missing required Open Beta files: {missing_docs}"

with zipfile.ZipFile(whl_path, "r") as zf:
    names = set(zf.namelist())
    for name in names:
        parts = set(name.split("/"))
        hit = parts & forbidden_names
        assert not hit, f"Forbidden path in wheel: {name} ({hit})"
        if not name.endswith((".png", ".jpg", ".webp", ".wav", ".mp3", ".glb", ".mesh", ".ttf", ".otf")):
            data = zf.read(name)
            if secret_re.search(data):
                raise AssertionError(f"Potential API key pattern found in wheel file: {name}")

    required_wheel_assets = {
        "capri/assets/ui_click.wav",
        "capri/assets/ui_glitch.wav",
        "capri/assets/wake_chirp.wav",
        "capri/assets/capri-cyberdeck.desktop",
        "capri/assets/sprites_v2/idle.png",
        "capri/assets/sprites/idle.png",
        "capri/assets/3d/qml/Avatar3D.qml",
    }
    missing_assets = sorted(required_wheel_assets - names)
    assert not missing_assets, f"Wheel is missing packaged runtime assets: {missing_assets}"

print("[3/3] Hygiene, licensing & asset packaging audit PASSED: zero .git/.env/keys, LICENSE/ASSETS_NOTICE/SECURITY bundled.")
PY

# 4. Generate verifiable SHA256SUMS.txt for release integrity
SUMS_FILE="${DIST_DIR}/SHA256SUMS.txt"
(
    cd "$DIST_DIR"
    sha256sum "$(basename "$TARBALL")" "$(basename "$WHEEL_PATH")" > SHA256SUMS.txt
    sha256sum -c SHA256SUMS.txt >/dev/null
)
echo "[4/4] Release checksums verified: ${SUMS_FILE}"

echo ""
echo "Ready for Open Beta distribution:"
echo "  - Tarball:  ${TARBALL}"
echo "  - Wheel:    ${WHEEL_PATH}"
echo "  - Checksum: ${SUMS_FILE}"
