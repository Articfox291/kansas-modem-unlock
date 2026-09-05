#!/usr/bin/env python3
"""Self-bootstrap: download official platform-tools when missing (stdlib).

Sources are Google's official repository endpoints only. There is no
maintained checksum file for the `-latest` zips (they rotate per release),
so verification is: HTTPS-only official domain + valid zip structure +
expected binaries present + `adb version` / `fastboot --version` execute.
Strict setups can pin an exact URL + sha256 in config (tools.pinned).

Binaries land in <repo>/vendor/platform-tools/ (git-ignored, never
committed) and are picked up automatically by unlock.py/wizard.py resolution.
"""
from __future__ import annotations

import hashlib
import platform
import shutil
import sys
import urllib.request
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
VENDOR = HERE / "vendor" / "platform-tools"

URLS = {
    ("Windows", "AMD64"): "https://dl.google.com/android/repository/platform-tools-latest-windows.zip",
    ("Windows", "ARM64"): "https://dl.google.com/android/repository/platform-tools-latest-windows.zip",
    ("Linux", "x86_64"): "https://dl.google.com/android/repository/platform-tools-latest-linux.zip",
    ("Linux", "aarch64"): "https://dl.google.com/android/repository/platform-tools-latest-linux.zip",
    ("Darwin", "x86_64"): "https://dl.google.com/android/repository/platform-tools-latest-darwin.zip",
    ("Darwin", "arm64"): "https://dl.google.com/android/repository/platform-tools-latest-darwin.zip",
}

NEED_BINARIES = ("adb", "fastboot")


def _exe(name):
    return name + (".exe" if platform.system() == "Windows" else "")


def vendor_paths():
    return {b: VENDOR / _exe(b) for b in NEED_BINARIES}


def vendor_ready():
    return all(p.is_file() for p in vendor_paths().values())


def _fetch(url, dst, expected_sha=None):
    req = urllib.request.Request(url, headers={"User-Agent": "kansas-modem-unlock-bootstrap"})
    with urllib.request.urlopen(req, timeout=120) as r, open(dst, "wb") as f:
        total = int(r.headers.get("Content-Length") or 0)
        got, h = 0, hashlib.sha256()
        while True:
            chunk = r.read(1 << 20)
            if not chunk:
                break
            f.write(chunk)
            h.update(chunk)
            got += len(chunk)
            if total:
                print(f"\r  downloading: {got // (1 << 20)}MB / "
                      f"{total // (1 << 20)}MB", end="", flush=True)
        print()
    if expected_sha and h.hexdigest() != expected_sha.lower():
        dst.unlink(missing_ok=True)
        raise RuntimeError("sha256 mismatch on download (refusing)")
    return dst


def _verify(zip_path):
    with zipfile.ZipFile(zip_path) as z:
        if z.testzip() is not None:
            raise RuntimeError("downloaded zip is corrupt (testzip failed)")
        names = set(z.namelist())
        for b in NEED_BINARIES:
            cand = f"platform-tools/{_exe(b)}"
            if cand not in names:
                raise RuntimeError(f"zip missing expected binary: {cand}")
        # Extract the whole platform-tools tree: adb/fastboot need their
        # sibling DLLs/shared libs to execute (partial extract = dead tools).
        VENDOR.mkdir(parents=True, exist_ok=True)
        for m in z.infolist():
            if not m.filename.startswith("platform-tools/") or m.is_dir():
                continue
            dst = VENDOR / m.filename[len("platform-tools/"):]
            with z.open(m) as src, open(dst, "wb") as f:
                shutil.copyfileobj(src, f)
        if platform.system() != "Windows":
            for b in NEED_BINARIES:
                (VENDOR / _exe(b)).chmod(0o755)
    # binaries must execute and report versions
    import subprocess
    for b in NEED_BINARIES:
        flag = "version" if b == "adb" else "--version"
        r = subprocess.run([str(VENDOR / _exe(b)), flag],
                           capture_output=True, text=True, timeout=60)
        out = (r.stdout + r.stderr).strip().splitlines()
        print(f"  {b}: {(out[0] if out else '?')[:80]}")
        if r.returncode != 0:
            raise RuntimeError(f"{b} binary failed to run after extract")


def ensure(confirm=None, pinned=None):
    """Ensure vendored platform-tools exist. Returns {adb, fastboot} paths."""
    if vendor_ready():
        return {k: str(v) for k, v in vendor_paths().items()}
    key = (platform.system(), platform.machine())
    url = (pinned or {}).get("url") or URLS.get(key)
    if not url:
        raise RuntimeError(f"no platform-tools build known for {key}")
    if confirm is not None and not confirm(
            f"download ~15-50MB official platform-tools for {key[0]}?"):
        raise RuntimeError("declined (install platform-tools manually, or set "
                           "ADB_PATH/FASTBOOT_PATH)")
    if VENDOR.exists():
        shutil.rmtree(VENDOR)
    VENDOR.mkdir(parents=True, exist_ok=True)
    tmp = VENDOR.with_suffix(".zip")
    print(f"downloading official platform-tools ({key[0]})...")
    _fetch(url, tmp, (pinned or {}).get("sha256"))
    print("verifying (structure + binaries + version output)...")
    _verify(tmp)
    tmp.unlink(missing_ok=True)
    print(f"ready: {VENDOR}")
    return {k: str(v) for k, v in vendor_paths().items()}


def main():
    import argparse
    ap = argparse.ArgumentParser(description="fetch+verify platform-tools")
    ap.add_argument("--yes", action="store_true",
                    help="download without asking")
    ap.add_argument("--url", default="", help="pinned mirror URL (optional)")
    ap.add_argument("--sha256", default="", help="pinned sha256 (optional)")
    args = ap.parse_args()
    pinned = {"url": args.url, "sha256": args.sha256} if args.url else None
    if args.yes:
        confirm = lambda m: True  # noqa: E731
    else:
        def confirm(m):
            print(m)
            return input("type YES to download: ").strip() == "YES"
    paths = ensure(confirm=confirm, pinned=pinned)
    print(paths)
    return 0


if __name__ == "__main__":
    sys.exit(main())
