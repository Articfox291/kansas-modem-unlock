#!/usr/bin/env python3
"""backup_all.py — pull every feasible partition image (read-only, root).

What: enumerates /dev/block/by-name via su, pulls each readable partition
with `adb exec-out dd`, records sha256 + manifest. Skipped (size only):
super + userdata (gigabytes; note their sizes), metadata (crypto metadata,
pointless to copy), anything over 2GB.
Why not fastboot: stock fastboot has no partition-read command on this
hardware; use --fastboot-vars (device already in fastboot) to capture the
`getvar all` + partition-size inventory alongside.

Usage:
  python backup_all.py --out DIR [--skip p1,p2] [--fastboot-vars] [--yes]
"""
from __future__ import annotations

import argparse
import hashlib
import shutil
import os
import subprocess
import sys
import time
from pathlib import Path

def _sh(adb, *args, timeout=120):
    return subprocess.run([adb, *args], capture_output=True, text=True,
                          timeout=timeout)


def main():
    ap = argparse.ArgumentParser(description="full-device image backup")
    ap.add_argument("--out", required=True)
    ap.add_argument("--skip", default="super,userdata,metadata",
                    help="comma names to record-size-only (default: %(default)s)")
    ap.add_argument("--fastboot-vars", action="store_true",
                    help="also capture fastboot getvar inventory (reboot to "
                    "bootloader first; device must already be there)")
    ap.add_argument("--yes", action="store_true",
                    help="skip the final confirmation")
    ap.add_argument("--list-only", action="store_true",
                    help="enumerate + size everything, pull nothing")
    ap.add_argument("--adb", default=os.environ.get("ADB_PATH", "adb"))
    ap.add_argument("--fastboot", default=os.environ.get("FASTBOOT_PATH", "fastboot"))
    args = ap.parse_args()

    skip = {s.strip() for s in args.skip.split(",") if s.strip()}
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    if args.fastboot_vars:
        print("capturing fastboot inventory (device must be in fastboot)...")
        r = subprocess.run([args.fastboot, "getvar", "all"],
                           capture_output=True, text=True, timeout=120)
        (out / "fastboot_getvar_all.txt").write_text(r.stdout + r.stderr)
        print("saved fastboot_getvar_all.txt; reboot to system, then re-run "
              "without --fastboot-vars")
        return 0

    names = _sh(args.adb, "shell", "ls", "/dev/block/by-name").stdout.split()
    names = sorted({n.strip() for n in names if n.strip()})
    print(f"{len(names)} partitions enumerated")
    plan, skipped = [], []
    for n in names:
        if n in skip:
            skipped.append(n)
            continue
        r = _sh(args.adb, "shell", "su", "-c",
                f"blockdev --getsize64 /dev/block/by-name/{n}")
        try:
            size = int(r.stdout.strip().split()[0])
        except (ValueError, IndexError):
            print(f"  skip (unreadable size): {n}")
            skipped.append(n)
            continue
        if size > 2 * (1 << 30):
            print(f"  size-only ({size // (1 << 20)}MB): {n}")
            skipped.append(n)
            continue
        if size == 0:
            skipped.append(n)
            continue
        plan.append((n, size))
    total = sum(s for _, s in plan)
    print(f"pulling {len(plan)} partitions (~{total // (1 << 20)}MB); "
          f"size-only: {len(skipped)}")
    free = shutil.disk_usage(str(out)).free
    if free < total + (1 << 30):
        print(f"REFUSING: need ~{(total + (1 << 30)) // (1 << 20)}MB free, "
              f"have {free // (1 << 20)}MB. Free space or extend --skip.")
        return 2
    if args.list_only:
        for n, size in plan:
            print(f"  would pull: {n} {size // (1 << 20)}MB")
        print(f"size-only ({len(skipped)}): {', '.join(sorted(skipped))}")
        return 0
    if not args.yes:
        ans = input("type PULL in capitals to start (read-only pulls): ").strip()
        if ans != "PULL":
            print("aborted (nothing pulled)")
            return 2
    manifest = []
    for n, size in plan:
        dst = out / f"{n}.img"
        t0 = time.time()
        with open(dst, "wb") as f:
            r = subprocess.run(
                [args.adb, "exec-out", "su", "-c",
                 f"dd if=/dev/block/by-name/{n} bs=4M 2>/dev/null"],
                stdout=f, timeout=1800)
        if r.returncode != 0 or dst.stat().st_size != size:
            print(f"  FAILED/MISMATCH: {n} (expected {size}, "
                  f"got {dst.stat().st_size if dst.exists() else 'none'})")
            manifest.append(f"{n} FAILED size_want={size}")
            continue
        h = hashlib.sha256()
        with open(dst, "rb") as f:
            for b in iter(lambda: f.read(4 << 20), b""):
                h.update(b)
        manifest.append(f"{n} {size} {h.hexdigest()}")
        print(f"  {n} {size // (1 << 20)}MB sha={h.hexdigest()[:12]}... "
              f"{int(time.time() - t0)}s")
    (out / "MANIFEST.txt").write_text("\n".join(manifest) + "\n")
    (out / "SKIPPED.txt").write_text("\n".join(sorted(skipped)) + "\n")
    print(f"done: {out}/MANIFEST.txt, skipped list in SKIPPED.txt")
    return 0


if __name__ == "__main__":
    sys.exit(main())
