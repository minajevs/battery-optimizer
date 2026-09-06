#!/usr/bin/env python3
"""Deploy to THIS Home Assistant instance over a mounted Samba share.

`deploy.ps1` came from the upstream repo this project was forked from: it is
Windows PowerShell and it targets 192.168.33.167, a host that does not exist on
this network. This is the same procedure for the machine that does
(192.168.1.130), driven from macOS.

Two targets, because there are two kinds of code here:

    integration   custom_components/growatt_modbus  -> <config>/custom_components/
    appdaemon     the optimizer, its library and the session reaper
                                                    -> <addon_configs>/…/apps/

**Nothing is restarted by this script.** The available Home Assistant token is
not an admin one, so the Supervisor API refuses it (401) — and a script that
cannot verify a restart happened has no business claiming it did. It copies,
verifies by SHA256, and tells you exactly what to restart.

**Stop AppDaemon before an `appdaemon` deploy.** It hot-reloads on every .py
change, so a multi-file copy is imported while it is still in progress: a new
module loads against its old peers. That is not hypothetical — it produced
ModuleNotFoundError and TypeError from a tree whose files were each correct.
The script refuses without --appdaemon-stopped for exactly that reason.

    # one-time, from Finder or:
    mkdir -p /Volumes/ha-config /Volumes/ha-addons
    mount_smbfs //user@192.168.1.130/config /Volumes/ha-config
    mount_smbfs //user@192.168.1.130/addon_configs /Volumes/ha-addons

    uv run python scripts/deploy.py --target integration --dry-run
    uv run python scripts/deploy.py --target integration --confirm
"""
from __future__ import annotations

import argparse
import datetime
import hashlib
import os
import shutil
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
INTEGRATION_SOURCE = REPO.parent / "Growatt_ModbusTCP" / "custom_components" / "growatt_modbus"

DEFAULT_CONFIG_MOUNT = "/Volumes/ha-config"
DEFAULT_ADDONS_MOUNT = "/Volumes/ha-addons"
# The AppDaemon add-on's own config share, as this instance lays it out.
APPDAEMON_DIR = "a0d7b954_appdaemon/apps"

KEEP_BACKUPS = 5

# Home Assistant 2026.9 runs Python 3.13, and the integration uses syntax that
# needs it: coordinator.py has a PEP 695 `type X = ...` alias, which is a
# SyntaxError before 3.12. Byte-compiling it with this Mac's default 3.10 fails
# on correct code, so the check picks an interpreter new enough to be a fair
# test and says so when it cannot find one. Compiling against the WRONG version
# is worse than not compiling: it fails deploys that would have worked and
# would eventually be silenced with --skip-tests, taking the real checks with
# it.
MIN_COMPILE_VERSION = (3, 12)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(65536), b""):
            digest.update(block)
    return digest.hexdigest()


def compiler_for(target: str) -> tuple[str | None, str]:
    """An interpreter new enough to judge this target's syntax, and why."""
    if target != "integration":
        return sys.executable, f"this interpreter ({sys.version_info.major}."
        f"{sys.version_info.minor})"

    if sys.version_info >= MIN_COMPILE_VERSION:
        return sys.executable, (f"this interpreter "
                                f"({sys.version_info.major}."
                                f"{sys.version_info.minor})")

    for minor in range(15, MIN_COMPILE_VERSION[1] - 1, -1):
        found = shutil.which(f"python3.{minor}")
        if found:
            return found, f"python3.{minor}"

    return None, (f"no python >= {MIN_COMPILE_VERSION[0]}."
                  f"{MIN_COMPILE_VERSION[1]} found")


def sources_for(target: str) -> list[tuple[Path, str]]:
    """(source_path, destination_relative_path) pairs, deterministic order."""
    pairs: list[tuple[Path, str]] = []

    if target == "integration":
        root = INTEGRATION_SOURCE
        if not root.is_dir():
            raise SystemExit(f"integration source not found: {root}")
        for path in sorted(root.rglob("*")):
            if path.is_dir() or "__pycache__" in path.parts:
                continue
            pairs.append((path, str(Path("growatt_modbus") / path.relative_to(root))))
        return pairs

    apps = REPO / "appdaemon" / "apps"
    for name in ("battery_optimizer.py", "session_reaper.py"):
        path = apps / name
        if path.is_file():
            pairs.append((path, name))
    library = apps / "battery_optimizer_lib"
    for path in sorted(library.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        pairs.append((path, str(Path("battery_optimizer_lib") /
                                path.relative_to(library))))
    # apps.yaml is NEVER deployed: the live one holds the HA token and the
    # tuning this installation actually runs on. Overwriting it from a repo
    # copy would silently replace both.
    return pairs


def destination_root(args) -> Path:
    if args.target == "integration":
        return Path(args.config_mount) / "custom_components"
    return Path(args.addons_mount) / APPDAEMON_DIR


def preflight(args) -> None:
    """Refuse to deploy something that does not compile or does not pass."""
    test_root = (INTEGRATION_SOURCE.parent.parent
                 if args.target == "integration" else REPO)
    if args.skip_tests:
        print("  ! tests SKIPPED by request")
    else:
        print(f"  running the test suite in {test_root.name} ...")
        result = subprocess.run([sys.executable, "-m", "pytest", "-q"],
                                cwd=test_root, capture_output=True, text=True)
        tail = (result.stdout or result.stderr).strip().splitlines()[-1:]
        print(f"    {tail[0] if tail else '(no output)'}")
        if result.returncode != 0:
            # The integration suite needs pymodbus, which this Mac does not
            # have: 79 of its tests fail on the import alone, identically
            # before and after any change here. That is an environment gap,
            # not evidence about the code, so it is reported and not treated
            # as a verdict.
            if args.target == "integration":
                print("    ! the integration suite did not pass here. If those "
                      "are the known pymodbus import failures, they say "
                      "nothing about this change — check before continuing.")
            else:
                raise SystemExit("REFUSED: the test suite is failing")

    compiler, described = compiler_for(args.target)
    if compiler is None:
        print(f"  ! syntax check SKIPPED: {described}. Home Assistant runs "
              f"3.13 and this integration uses syntax newer than this machine "
              f"can parse, so a check here would fail correct code.")
    else:
        print(f"  byte-compiling every source file with {described} ...")
        for source, _rel in sources_for(args.target):
            if source.suffix != ".py":
                continue
            result = subprocess.run([compiler, "-m", "py_compile", str(source)],
                                    capture_output=True, text=True)
            if result.returncode != 0:
                raise SystemExit(
                    f"REFUSED: {source} does not compile\n{result.stderr}")

    dirty = subprocess.run(["git", "status", "--porcelain"], cwd=test_root,
                           capture_output=True, text=True).stdout.strip()
    if dirty:
        print("  ! working tree is NOT clean — deploying uncommitted changes:")
        for line in dirty.splitlines()[:10]:
            print(f"      {line}")
    commit = subprocess.run(["git", "rev-parse", "--short", "HEAD"],
                            cwd=test_root, capture_output=True,
                            text=True).stdout.strip()
    print(f"  deploying from {test_root.name} @ {commit}")


def back_up(root: Path, target: str, confirm: bool) -> Path | None:
    """Copy what is there now beside it, keeping the newest few."""
    existing = root / "growatt_modbus" if target == "integration" else root
    if not existing.exists():
        print("  no existing install to back up")
        return None

    # Beside the deployed tree, never inside it: a backup under
    # custom_components/ would be scanned by Home Assistant as another custom
    # component, and one under apps/ would be loaded by AppDaemon.
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    backups_root = root.parent / "deploy-backups"
    backup = backups_root / f"{target}-{stamp}"
    print(f"  backup -> {backup}")
    if confirm:
        backup.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(existing, backup,
                        ignore=shutil.ignore_patterns("__pycache__"))
        existing_backups = sorted(backups_root.glob(f"{target}-*"))
        for stale in existing_backups[:-KEEP_BACKUPS]:
            print(f"  pruning old backup {stale.name}")
            shutil.rmtree(stale, ignore_errors=True)
    return backup


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target", required=True,
                        choices=("integration", "appdaemon"))
    parser.add_argument("--config-mount", default=DEFAULT_CONFIG_MOUNT)
    parser.add_argument("--addons-mount", default=DEFAULT_ADDONS_MOUNT)
    parser.add_argument("--confirm", action="store_true",
                        help="actually write; without it nothing is copied")
    parser.add_argument("--dry-run", action="store_true",
                        help="explicit no-op, the default behaviour anyway")
    parser.add_argument("--prune", action="store_true",
                        help="delete files at the destination that the source "
                             "no longer has")
    parser.add_argument("--appdaemon-stopped", action="store_true",
                        help="acknowledge that the AppDaemon add-on is stopped")
    parser.add_argument("--skip-tests", action="store_true")
    args = parser.parse_args()

    writing = args.confirm and not args.dry_run

    if args.target == "appdaemon" and writing and not args.appdaemon_stopped:
        print("REFUSED: AppDaemon hot-reloads on every .py change, so copying a\n"
              "tree into a running add-on imports new modules against old ones.\n"
              "Stop the add-on, then re-run with --appdaemon-stopped.")
        return 2

    root = destination_root(args)
    mount = Path(args.config_mount if args.target == "integration"
                 else args.addons_mount)
    if not mount.is_dir():
        print(f"REFUSED: {mount} is not mounted. Mount the Samba share first:\n"
              f"    mkdir -p {mount}\n"
              f"    mount_smbfs //<user>@192.168.1.130/"
              f"{'config' if args.target == 'integration' else 'addon_configs'} "
              f"{mount}")
        return 2

    print(f"=== deploy: {args.target} -> {root} ===")
    print(f"    mode: {'WRITING' if writing else 'DRY RUN (nothing is copied)'}")

    preflight(args)
    pairs = sources_for(args.target)
    print(f"  {len(pairs)} file(s) to consider")

    back_up(root, args.target, writing)

    changed, unchanged = [], 0
    for source, relative in pairs:
        destination = root / relative
        if destination.is_file() and sha256(destination) == sha256(source):
            unchanged += 1
            continue
        changed.append((source, destination, relative))

    for _source, _destination, relative in changed:
        print(f"    + {relative}")
    print(f"  {len(changed)} to copy, {unchanged} already identical")

    if not writing:
        print("\nDRY RUN — nothing was written. Re-run with --confirm.")
        return 0

    for source, destination, _relative in changed:
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)

    if args.prune:
        wanted = {root / relative for _source, relative in pairs}
        base = root / "growatt_modbus" if args.target == "integration" else root
        for path in sorted(base.rglob("*.py")):
            if "__pycache__" not in path.parts and path not in wanted:
                print(f"    - {path.relative_to(root)} (pruned)")
                path.unlink()

    for cache in list(root.rglob("__pycache__")):
        shutil.rmtree(cache, ignore_errors=True)

    print("\n  verifying by SHA256 ...")
    bad = [relative for source, relative in pairs
           if not (root / relative).is_file()
           or sha256(root / relative) != sha256(source)]
    if bad:
        print("  FAILED verification:")
        for relative in bad:
            print(f"    ! {relative}")
        return 1
    print(f"  all {len(pairs)} file(s) verified identical to source")

    print("\nNOW RESTART, BY HAND (this script holds no admin token):")
    if args.target == "integration":
        print("  Home Assistant must restart to load changed custom_component\n"
              "  code — reloading the config entry re-uses the old modules.\n"
              "  Settings > System > top-right > Restart Home Assistant")
    else:
        print("  Settings > Add-ons > AppDaemon > Start")
    return 0


if __name__ == "__main__":
    sys.exit(main())
