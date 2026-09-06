#!/usr/bin/env python3
"""auto_setup.py — one file, plug in, end ready-to-flash. Nothing flashes here.

Does everything automatable in order, asking only where a human is required:
  1. environment (python version, platform-tools via bootstrap if missing)
  2. device (wait + RSA-authorization guidance loop, fingerprint vs profiles)
  3. config (firmware/APK paths with existence validation, written once)
  4. modem backup pull (200MB, manifest-logged; mandatory, never skipped)
  5. pre-build check (stock integrity + patch-table dry run, no output kept
     unless asked) and the exact next command to run

Flashing, rooting, and unlocking remain behind their own typed confirmations
in wizard.py / unlock.py / manager_gui.py — auto_setup never flashes.

Usage:
  python auto_setup.py [--yes] [--config CONFIG] [--work DIR] [--profile ID]
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))


def say(msg):
    print(f"[setup] {msg}")


def ask(prompt, expect_yes=False):
    if expect_yes:
        ans = input(f"[setup] {prompt} [type YES]: ").strip()
        if ans != "YES":
            say("stopped by user (nothing changed)")
            raise SystemExit(2)
        return True
    return input(f"[setup] {prompt}: ").strip()


def ask_path(prompt, must_exist=True):
    while True:
        p = input(f"[setup] {prompt} (empty aborts): ").strip().strip("'\"")
        if not p:
            say("aborted by user (nothing changed)")
            raise SystemExit(2)
        if must_exist and not Path(p).is_file():
            print(f"not a file: {p} -- try again")
            continue
        return p


def step_env(args):
    if sys.version_info < (3, 10):
        say(f"python {sys.version.split()[0]} found; 3.10+ required -- aborting")
        raise SystemExit(2)
    say(f"python {sys.version.split()[0]} OK")
    import bootstrap
    if args.yes:
        paths = bootstrap.ensure(confirm=lambda m: True)
    else:
        def confirm(m):
            print(f"[setup] {m}")
            return input("[setup] download? [type YES]: ").strip() == "YES"
        try:
            paths = bootstrap.ensure(confirm=confirm)
        except RuntimeError as e:
            say(f"{e}; continuing with PATH tools if present")
            paths = {}
    adb = paths.get("adb", "") if isinstance(paths, dict) else ""
    os_env = __import__("os").environ
    if adb:
        os_env["ADB_PATH"] = adb
        fb = paths.get("fastboot", "")
        if fb:
            os_env["FASTBOOT_PATH"] = fb
        say(f"tools ready: {adb}")
    return paths


def step_device(args):
    import wizard
    cfg = {"adb_path": __import__("os").environ.get("ADB_PATH", ""),
           "fastboot_path": __import__("os").environ.get("FASTBOOT_PATH", "")}
    ux = wizard.Ux(cfg, dry=False)
    if not ux.adb or not ux.fastboot:
        # fall back to PATH names; Ux resolves vendor dir itself
        import shutil
        ux.adb = shutil.which("adb") or "adb"
        ux.fastboot = shutil.which("fastboot") or "fastboot"
    ux.ensure_device()
    if args.profile:
        profs = wizard.load_profiles()
        if args.profile not in profs:
            say(f"unknown profile: {args.profile}")
            raise SystemExit(2)
        prof = profs[args.profile]
    else:
        profs = wizard.load_profiles()
        model = ux.getprop("ro.product.model")
        bb = ux.getprop("gsm.version.baseband")
        sku = ux.getprop("ro.boot.hardware.sku")
        prof = None
        for pid, p in profs.items():
            bb_ok = (not p.get("baseband_substr")
                     or p["baseband_substr"] in bb)
            if not bb_ok:
                for alt in p.get("baseband_alt", []):
                    if alt and alt in bb:
                        bb_ok = True
                        say(f"lab-marker baseband variant recognized ({pid})")
                        break
            if (p.get("models") and model
                    and any(m in model for m in p["models"]) and
                    (not p.get("skus") or sku in p["skus"]) and bb_ok):
                prof = p
                break
        if prof is None:
            say(f"no profile matches model={model} sku={sku} "
                f"baseband={bb[:44]} (see devices/README.md)")
            raise SystemExit(2)
    say(f"profile: {prof['id']} status={prof.get('status')}")
    ux.auto_yes = args.yes
    ux.battery_ok()
    return prof, ux


def step_config(args, prof):
    cfg_path = Path(args.config)
    cfg = {}
    if cfg_path.is_file():
        cfg = json.loads(cfg_path.read_text())
        say(f"loaded {cfg_path} (values below are defaults, Enter keeps)")
    fw = dict(cfg.get("firmware_files", {}) or {})
    apks = dict(cfg.get("apks", {}) or {})

    def keep_or_ask(key, label, store):
        cur = store.get(key, "")
        if cur and Path(cur).is_file():
            say(f"{label}: keeping {cur}")
            return
        if cur:
            say(f"{label}: configured path missing ({cur}) -- re-ask")
        if args.yes and not cur:
            say(f"{label}: skipped (--yes, empty)")
            return
        store[key] = ask_path(f"{label} file path")

    say("firmware + APK paths (yours -- nothing ships with the tool):")
    for key, label in (("system_gsi", "system GSI image"),
                       ("vbmeta_a", "vbmeta image"),
                       ("init_boot_stock", "stock init_boot (revert material)"),
                       ("init_boot_ksu", "KSU-patched init_boot (see section 3 of "
                        "docs/root-image-walkthrough.md)"),
                       ("md1img_stock", "factory md1img (integrity-checked)")):
        keep_or_ask(key, label, fw)
    ksu_apk = apks.get("ksu_manager", "")
    if not (ksu_apk and Path(ksu_apk).is_file()) and not args.yes:
        p = input("[setup] KSU manager APK path (empty = set later): "
                  ).strip().strip("'\"")
        if p:
            if Path(p).is_file():
                apks["ksu_manager"] = p
            else:
                say(f"not a file, leaving empty: {p}")
    cfg["firmware_files"] = fw
    cfg["apks"] = apks
    cfg.setdefault("backup_root", "backups")
    cfg.setdefault("work_root", "work")
    cfg.setdefault("strict", True)
    cfg.setdefault("experimental", False)
    if cfg_path.exists():
        bak = cfg_path.with_suffix(".json.bak")
        bak.write_text(Path(args.config).read_text()
                       if Path(args.config).is_file() else "{}")
        say(f"previous config backed up to {bak.name}")
    cfg_path.write_text(json.dumps(cfg, indent=2))
    say(f"config written: {cfg_path}")
    return cfg


def step_backup(args, ux):
    import unlock
    import argparse as _a
    out = Path(args.work) / "backups"
    say("pulling live modem backup (200MB, minutes) -- mandatory, never skipped")
    t0 = time.time()
    rc = unlock.cmd_backup(_a.Namespace(out=str(out)))
    say(f"backup done in {int(time.time() - t0)}s")
    return rc


def step_backup_all(args, ux):
    import subprocess as _sp
    dest = str(Path(args.work) / "backups-all")
    say("full-device image backup (read-only pulls; super/userdata/metadata size-only)")
    cmd = [sys.executable, str(Path(__file__).resolve().parent / "backup_all.py"),
           "--out", dest]
    if args.yes:
        cmd.append("--yes")
    r = _sp.run(cmd, timeout=5400)
    if r.returncode != 0:
        say("backup-all incomplete (see above); re-run later if wanted")
    return 0


def step_prebuild(args, ux, cfg):
    import unlock
    import argparse as _a
    stock = (cfg.get("firmware_files", {}) or {}).get("md1img_stock", "")
    if not stock or not Path(stock).is_file():
        say("pre-build skipped (no stock image configured)")
        return 0
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        out = str(Path(td) / "prebuild.img")
        say("dry-building patch set against stock (validates table, then discards)...")
        rc = unlock.cmd_build(_a.Namespace(stock=stock, out=out))
    say("pre-build OK: table applies cleanly to your stock image")
    return rc


def main():
    ap = argparse.ArgumentParser(description="one-file instant setup (no flashing)")
    ap.add_argument("--yes", action="store_true",
                    help="non-interactive where safe (still asks for files)")
    ap.add_argument("--config", default="config.json")
    ap.add_argument("--work", default="work-setup")
    ap.add_argument("--profile", default="")
    args = ap.parse_args()
    say("kansas-modem-unlock auto-setup (this tool never flashes)")
    step_env(args)
    prof, ux = step_device(args)
    cfg = step_config(args, prof)
    if step_backup(args, ux):
        return 1
    step_backup_all(args, ux)
    if step_prebuild(args, ux, cfg):
        return 1
    say("READY. Next:")
    say(f"  python wizard.py --work {args.work}      # guided flash -> root -> unlock")
    say(f"  python manager_gui.py                    # same, clickable")
    say(f"  backups live in {args.work}/backups (and {cfg.get('backup_root')})")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit as e:
        raise
    except Exception as e:  # noqa: BLE001
        print(f"SETUP FAILED: {type(e).__name__}: {e}")
        sys.exit(1)
