#!/usr/bin/env python3
"""wizard.py — interactive plug-in flow: detect -> flash -> root -> unlock.

Plug in ONE supported device and follow the steps. Every phase is
check -> instruct -> verify with typed confirmations; progress resumes from
state.json; --dry-run walks the whole flow making zero device writes.

Device support comes from devices/*.json (schema v1, see devices/README.md).
Only profiles with status verified-live may flash; anything else is
audit/status only. The kansas profile is currently the only verified one.

The modem-unlock phase reuses unlock.py (drift-guarded: profile fingerprint
must equal unlock.py's constants or the phase refuses).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent


class Refuse(Exception):
    pass


class Ux:
    """Device/session interface. dry=True logs instead of writing."""

    def __init__(self, cfg, dry=False):
        self.cfg = cfg
        self.dry = dry
        self.mode = "adb"  # or "fastboot"
        os.environ.setdefault("ADB_PATH", cfg.get("adb_path", ""))
        os.environ.setdefault("FASTBOOT_PATH", cfg.get("fastboot_path", ""))
        self.adb = os.environ.get("ADB_PATH") or "adb"
        self.fastboot = os.environ.get("FASTBOOT_PATH") or "fastboot"
        if dry:
            self.log("DRY RUN: no device writes will happen")

    def log(self, msg):
        print(f"[wizard] {msg}")

    def abort(self, msg):
        raise Refuse(msg)

    def confirm(self, prompt, expect="YES"):
        if self.dry:
            self.log(f"would confirm: {prompt} (expect {expect})")
            return
        ans = input(f"[confirm] {prompt} [type {expect}]: ").strip()
        if ans != expect:
            self.abort("aborted by user (nothing flashed)")

    def _run(self, cmd, timeout=120):
        return subprocess.run(cmd, capture_output=True, text=True,
                              timeout=timeout)

    def shell(self, *args, timeout=60):
        if self.dry:
            self.log(f"would run: adb shell {' '.join(args)}")
            return ""
        r = self._run([self.adb, "shell", *args], timeout=timeout)
        if "no devices" in (r.stderr + r.stdout).lower():
            self.abort("no adb device (cable? RSA prompt accepted?)")
        return r.stdout.strip()

    def getprop(self, name):
        return self.shell("getprop", name)

    def active_slot(self):
        s = self.getprop("ro.boot.slot_suffix") if not self.dry else "_a"
        return "b" if s.strip() == "_b" else "a"

    def installed_packages(self):
        out = self.shell("pm list packages")
        return out

    def shell_su_ok(self, probe, marker):
        out = self.shell(*probe.split())
        return marker in out

    @staticmethod
    def sha256(path):
        h = hashlib.sha256()
        with open(path, "rb") as f:
            for b in iter(lambda: f.read(4 << 20), b""):
                h.update(b)
        return h.hexdigest()

    def fastboot_flash(self, part, src):
        if self.dry:
            self.log(f"would fastboot flash {part} <- {src}")
            return
        self.shell("reboot", "bootloader")
        self.mode = "fastboot"
        time.sleep(12)
        r = self._run([self.fastboot, "devices"], timeout=60)
        if "fastboot" not in r.stdout:
            self.abort("device not in fastboot (Vol-Down+Power, cable trick)")
        r = self._run([self.fastboot, "flash", part, str(src)], timeout=600)
        print((r.stdout + r.stderr)[-400:])
        if r.returncode != 0 or "OKAY" not in (r.stdout + r.stderr):
            self.abort(f"fastboot flash {part} failed; check cable and retry")

    def reboot(self, wait=True):
        if self.dry:
            self.log("would reboot and wait for device")
            return
        if self.mode == "fastboot":
            self._run([self.fastboot, "reboot"], timeout=60)
            self.mode = "adb"
        else:
            self.shell("reboot")
        time.sleep(45)
        self._run([self.adb, "wait-for-device"], timeout=300)
        time.sleep(20)
        for _ in range(20):
            if self.getprop("sys.boot_completed") == "1":
                return
            time.sleep(15)
        self.abort("device did not finish booting in time; check screen/cable")

    def backup_partition(self, part, outdir="backups"):
        out = Path(self.cfg.get("backup_root", outdir))
        out.mkdir(parents=True, exist_ok=True)
        ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        dst = out / f"{part}_backup_{ts}.img"
        if self.dry:
            self.log(f"would pull {part} -> {dst}")
            return dst
        self.log(f"pulling {part} (minutes) -> {dst}")
        with open(dst, "wb") as f:
            r = subprocess.run(
                [self.adb, "exec-out", "su", "-c",
                 f"dd if=/dev/block/by-name/{part} bs=1M 2>/dev/null"],
                stdout=f, timeout=900)
        if r.returncode != 0:
            self.abort(f"backup pull failed for {part}")
        h = self.sha256(dst)
        (out / "BACKUP_MANIFEST.txt").open("a").write(f"{part} {dst.name} {h}\n")
        self.log(f"backup {part}: sha256={h[:12]}...")
        return dst

    def device_health(self, when):
        if self.dry:
            self.log(f"would check health ({when})")
            return
        boot = self.getprop("sys.boot_completed")
        remain = self.shell("getprop | grep remain.count")
        ee = self.shell("dmesg | grep -ciE 'modem exception|MD exception|"
                        "assert fail|reset MD|exception stage|fatal error' || true")
        sim = self.getprop("gsm.sim.state")
        bb = self.getprop("gsm.version.baseband")
        self.log(f"health[{when}]: boot={boot} sim={sim} remain?[{remain[-8:]}] "
                 f"EE={ee} baseband={bb[:44]}")


def load_profiles():
    profs = {}
    for p in sorted((HERE / "devices").glob("*.json")):
        try:
            d = json.loads(p.read_text())
        except Exception as e:  # noqa: BLE001
            print(f"[wizard] skipping {p.name}: {e}")
            continue
        if d.get("schema") != 1 or "id" not in d:
            print(f"[wizard] skipping {p.name}: bad schema")
            continue
        profs[d["id"]] = d
    return profs


def detect(ux, profs, cfg):
    one = ux.shell("devices")
    if "device" not in one and not ux.dry:
        ux.abort("no adb device")
    model = ux.getprop("ro.product.model") if not ux.dry else "kansas"
    bb = ux.getprop("gsm.version.baseband") if not ux.dry else ""
    sku = ux.getprop("ro.boot.hardware.sku") if not ux.dry else "XT2513V"
    for pid, p in profs.items():
        if (p.get("models") and model and
                any(m in model for m in p["models"]) and
                (not p.get("skus") or sku in p["skus"]) and
                (not p.get("baseband_substr") or p["baseband_substr"] in bb
                 or ux.dry)):
            ux.log(f"detected profile: {pid} ({p.get('label')})")
            return p
    ux.log(f"no profile matches model={model} sku={sku} "
           f"baseband={bb[:40]}")
    manual = (cfg.get("device_profile") or "").strip()
    if manual and manual in profs:
        ux.confirm(f"use profile {manual} for THIS device anyway?",
                   expect="OVERRIDE")
        return profs[manual]
    ux.abort("unsupported device (see devices/README.md to add one)")


def need_verified(profile, ux):
    if profile.get("status") != "verified-live":
        ux.abort(f"profile {profile['id']} is {profile.get('status')}: "
                 f"audit/status only, flashing refused")


def phase_unlock(ux, profile, cfg, workdir):
    need_verified(profile, ux)
    # Drift guard: framework profile must equal unlock.py constants.
    import unlock as U
    import patches as P
    mu = profile["modem_unlock"]
    if mu.get("slot") != "md1img_a":
        ux.abort("profile modem slot is not md1img_a")
    local = {(int(x[1]), bytes.fromhex(x[2]).hex(), bytes.fromhex(x[3]).hex())
             for x in P.PATCHES}
    prof = {(int(x["offset"], 16), x["old"], x["new"])
            for x in mu["patches"]}
    if local != prof:
        ux.abort("patches.py drifted from profile modem_unlock; refusing "
                 "(fix the profile or the table, never flash drifted bytes)")
    ux.log("patch table matches profile (8 entries, 32 bytes)")
    if ux.dry:
        ux.log("dry-run: would backup -> build -> flash slot A -> verify")
        return True
    workdir = Path(workdir)
    workdir.mkdir(parents=True, exist_ok=True)
    bdir = workdir / "backups"
    ns = argparse_Namespace(out=str(bdir))
    ux.log("== modem backup ==")
    U.cmd_backup(ns)
    stock = (cfg.get("firmware_files", {}) or {}).get("md1img_stock", "")
    if not stock:
        ux.log("set config firmware_files.md1img_stock to your factory image, "
               "then re-run (nothing flashed)")
        return False
    bfile = workdir / "patched_work.img"
    ux.log("== modem build ==")
    U.cmd_build(argparse_Namespace(stock=stock, out=str(bfile)))
    signed = bfile.with_suffix(".signed.img")
    ux.log("== modem flash (slot A only) ==")
    U.cmd_flash(argparse_Namespace(image=str(signed), backup=str(bdir)))
    ux.log("waiting out the reboot, then verifying...")
    time.sleep(75)
    ux.log("== modem verify ==")
    U.cmd_verify(argparse_Namespace())
    return True


def argparse_Namespace(**kw):
    import argparse as _a
    return _a.Namespace(**kw)


def main():
    ap = argparse.ArgumentParser(description="interactive plug-in flow")
    ap.add_argument("--config", default=str(HERE / "config.json"))
    ap.add_argument("--work", default="work-wizard")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--phases", default="detect,flash,root,unlock",
                    help="subset/order, e.g. unlock or detect,verify")
    args = ap.parse_args()
    cfg = json.loads(Path(args.config).read_text()) if Path(args.config).exists() \
        else {}
    ux = Ux(cfg, dry=args.dry_run)
    if args.dry_run:
        ux.log("note: detection values are stubbed in dry-run")
    state_p = Path(args.work) / "state.json"
    state = {"done": []}
    if Path(args.work).joinpath("state.json").exists() and not args.dry_run:
        try:
            state = json.loads(state_p.read_text())
        except Exception:  # noqa: BLE001
            pass
    if not os.environ.get("ADB_PATH") and cfg.get("adb_path"):
        os.environ["ADB_PATH"] = cfg["adb_path"]
    if not os.environ.get("FASTBOOT_PATH") and cfg.get("fastboot_path"):
        os.environ["FASTBOOT_PATH"] = cfg["fastboot_path"]
    # unlock.py resolves its tool paths at import; set env first.
    sys.path.insert(0, str(HERE))
    profs = load_profiles()
    if not profs:
        ux.abort("no device profiles found")
    profile = detect(ux, profs, cfg)
    ux.log(f"profile: {profile['id']} status={profile.get('status')}")
    import flash_mod
    import root_mod

    def save():
        if not args.dry_run:
            Path(args.work).mkdir(parents=True, exist_ok=True)
            state_p.write_text(json.dumps(state, indent=1))

    for phase in [p.strip() for p in args.phases.split(",") if p.strip()]:
        if phase in state.get("done", []) and not args.dry_run:
            ux.log(f"phase {phase}: already done (resume), skipping")
            continue
        ux.log(f"===== phase: {phase} =====")
        if phase == "detect":
            ux.device_health("detect")
        elif phase == "flash":
            need_verified(profile, ux)
            import flash_mod as _f  # noqa: F811
            if not _f.run_flash_plan(ux, profile, cfg):
                ux.log("flash phase incomplete (provide missing images, re-run)")
                save()
                return 1
        elif phase == "root":
            need_verified(profile, ux)
            if not root_mod.run_root_phase(ux, profile, cfg):
                ux.log("root phase incomplete (finish on-device steps, re-run)")
                save()
                return 1
        elif phase == "unlock":
            if not phase_unlock(ux, profile, cfg, Path(args.work)):
                ux.log("unlock phase incomplete, re-run")
                save()
                return 1
        elif phase == "verify":
            ux.device_health("verify")
        else:
            ux.abort(f"unknown phase: {phase}")
        state.setdefault("done", []).append(phase)
        save()
    ux.log("all requested phases complete")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Refuse as e:
        print(f"REFUSED: {e}")
        sys.exit(2)
