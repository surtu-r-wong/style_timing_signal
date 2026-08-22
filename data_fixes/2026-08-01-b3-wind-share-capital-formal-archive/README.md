# B3 formal-run evidence archive

This directory preserves a small, review-oriented core from the B3 Windows
formal run. It does **not** contain the complete formal-run tree.

## Provenance

- Source branch: fix/b3-wind-share-capital-tail
- Source commit: 41ed581c649712c90463c587265cd1a47e177c44
- Source tag: archive/b3-wind-share-capital-tail-20260814
- Source formal-run root:
  /home/elfbob/claude-code/.worktrees/style_timing_signal/b3-wind-share-capital/data_fixes/2026-08-01-b3-wind-share-capital/run-windows-formal/

The core/ directory contains exactly 10 selected files. Their paths, sizes,
and SHA-256 digests are recorded in inventory.json under core_files.
formal_run_files separately inventories all 32 files found under the source
formal-run root. The complete file content belongs in the external tarball
described below, not in this repository directory.

## External complete archive

The complete archive is stored at:

~~~text
/home/elfbob/claude-code/deploy_backups/2026-08-21-style-timing-p0-evidence/b3-formal-run.tar.gz
~~~

Its member root is run-windows-formal/. The complete archive is 117,548,606
bytes and its SHA-256 digest is
a2bd6043824253816b531ccdc844a847c45393af63d59c1e5fed9a15ca234843.
The same path, size, digest, and member root are recorded in
inventory.json.external_archive.

## Verify the repository core

From the repository root:

~~~bash
python3 tools/verify_b3_formal_archive.py \
  --inventory data_fixes/2026-08-01-b3-wind-share-capital-formal-archive/inventory.json \
  --root data_fixes/2026-08-01-b3-wind-share-capital-formal-archive/core
~~~

The command prints OK only when the core file set exactly matches the inventory
and every file has the recorded size and SHA-256 digest.

## Verify and restore the external archive

Verify the tarball against inventory.json from the repository root:

~~~bash
set -euo pipefail
B3_INVENTORY=data_fixes/2026-08-01-b3-wind-share-capital-formal-archive/inventory.json
B3_TAR=$(python3 -c 'import json, sys; print(json.load(open(sys.argv[1], encoding="utf-8"))["external_archive"]["path"])' "$B3_INVENTORY")
B3_TAR_SIZE=$(python3 -c 'import json, sys; print(json.load(open(sys.argv[1], encoding="utf-8"))["external_archive"]["size"])' "$B3_INVENTORY")
B3_TAR_HASH=$(python3 -c 'import json, sys; print(json.load(open(sys.argv[1], encoding="utf-8"))["external_archive"]["sha256"])' "$B3_INVENTORY")
test -r "$B3_TAR"
printf '%s  %s\n' "$B3_TAR_HASH" "$B3_TAR" | sha256sum -c -
test "$(stat -c %s -- "$B3_TAR")" -eq "$B3_TAR_SIZE"
tar -tzf "$B3_TAR" >/dev/null
python3 - "$B3_TAR" <<'PY'
from pathlib import PurePosixPath
import sys
import tarfile

with tarfile.open(sys.argv[1], "r:gz") as archive:
    members = archive.getmembers()

if not members:
    raise SystemExit("unsafe tar: no members")
seen = set()
for member in members:
    raw_name = member.name.rstrip("/")
    path = PurePosixPath(raw_name)
    parts = raw_name.split("/")
    if (
        not raw_name
        or path.is_absolute()
        or any(part in {"", ".", ".."} for part in parts)
        or path.as_posix() != raw_name
    ):
        raise SystemExit(f"unsafe tar member path: {member.name!r}")
    if parts[0] != "run-windows-formal":
        raise SystemExit(f"unexpected tar member root: {member.name!r}")
    if raw_name in seen:
        raise SystemExit(f"duplicate tar member: {member.name!r}")
    seen.add(raw_name)
    if not (member.isdir() or member.isreg()):
        raise SystemExit(f"unsafe tar member type: {member.name!r}")
print("tar member safety: OK")
PY
~~~

After that verification succeeds, restore into a fresh private temporary
directory without writing into a live worktree. This block rechecks the archive
hash immediately before extraction:

~~~bash
set -euo pipefail
umask 077
B3_INVENTORY=data_fixes/2026-08-01-b3-wind-share-capital-formal-archive/inventory.json
B3_TAR=$(python3 -c 'import json, sys; print(json.load(open(sys.argv[1], encoding="utf-8"))["external_archive"]["path"])' "$B3_INVENTORY")
B3_TAR_HASH=$(python3 -c 'import json, sys; print(json.load(open(sys.argv[1], encoding="utf-8"))["external_archive"]["sha256"])' "$B3_INVENTORY")
test -r "$B3_TAR"
printf '%s  %s\n' "$B3_TAR_HASH" "$B3_TAR" | sha256sum -c -
RESTORE_DIR=$(mktemp -d /tmp/b3-formal-run-restore.XXXXXX)
chmod go-rwx "$RESTORE_DIR"
tar --no-same-owner --no-same-permissions -C "$RESTORE_DIR" -xzf "$B3_TAR"
chmod -R go-rwx "$RESTORE_DIR"
test -d "$RESTORE_DIR/run-windows-formal"
printf 'restored to %s\n' "$RESTORE_DIR"
~~~

The tarball stores its original owner and mode metadata. The safe restore above
intentionally does not apply that ownership or those permissions; it uses a
private umask and removes all group/other permissions from restored content.

## Limitations

- The repository core is a selected 10-file evidence set, not a complete copy.
- The local .gitattributes preserves the core evidence bytes without line-end
  conversion; this is required for the recorded hashes, including Windows CRLF.
- The complete archive is host-local and outside Git; repository checkout alone
  cannot restore it.
- Hash verification establishes byte identity, not analytical correctness or
  acceptance of the B3 result.
- core/RETRIEVAL.md documents important provenance boundaries: parts of the
  execution receipt were superseded by later states/structure/evaluation runs.
  The manifests and audit outputs must be interpreted with that history.
- This recovery archives existing evidence only. It does not rerun B3, repair
  data, or change any production signal.
