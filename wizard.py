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
        if dry:
            self.adb, self.fastboot = "adb", "fastboot"
        else:
            try:
                self.adb = self._resolve_tool("adb",
                                              os.environ.get("ADB_PATH"))
                self.fastboot = self._resolve_tool(
                    "fastboot", os.environ.get("FASTBOOT_PATH"))
            except Refuse:
                self.adb, self.fastboot = "", ""
            if not self.adb or not self.fastboot:
                self.log("tools unresolved yet "
                         "(setup phase will offer the official download)")

    @staticmethod
    def _resolve_tool(name, configured):
        import shutil as _sh
        if configured and Path(configured).is_file():
            return configured
        if configured:
            raise Refuse(f"{name} not found: {configured}")
        vendored = Path(__file__).resolve().parent / "vendor" / "platform-tools" / (name + (".exe" if os.name == "nt" else ""))
        if vendored.is_file():
            return str(vendored)
        found = _sh.which(name)
        if found:
            return found
        raise Refuse(
            f"{name} not found. Run: python bootstrap.py --yes (official "
            f"download, verified), or set {name.upper()}_PATH, or add "
            f"platform-tools to PATH")

    def ensure_tools(self):
        if self.dry:
            self.log("would ensure platform-tools (download if missing)")
            return
        if self.adb and self.fastboot:
            self.log(f"tools: adb={self.adb} fastboot={self.fastboot}")
            return
        tools_cfg = self.cfg.get("tools", {}) \
            if isinstance(self.cfg.get("tools"), dict) else {}
        if not tools_cfg.get("auto_download", True):
            self.abort("tools missing and tools.auto_download is off "
                       "(install platform-tools, set ADB_PATH/FASTBOOT_PATH, "
                       "or run bootstrap.py)")
        self.human("download official platform-tools",
                   ["~15-50MB from dl.google.com over HTTPS",
                    "verified after download: zip structure + both binaries "
                    "present + version output runs",
                    "binaries land in vendor/ (git-ignored, never committed)",
                    "strict setups: pin url+sha256, see README production section"])
        import bootstrap
        paths = bootstrap.ensure()
        self.adb = self.adb or paths.get("adb", "")
        self.fastboot = self.fastboot or paths.get("fastboot", "")
        if not self.adb or not self.fastboot:
            self.abort("bootstrap did not yield tools; install manually")
        self.log(f"tools ready: adb={self.adb}")

    def log(self, msg):
        print(f"[wizard] {msg}")

    def abort(self, msg):
        raise Refuse(msg)

    def confirm(self, prompt, expect="YES"):
        if self.dry:
            self.log(f"would confirm: {prompt} (expect {expect})")
            return
        if getattr(self, "auto_yes", False):
            self.log(f"auto-yes: proceeding past: {prompt}")
            return
        try:
            ans = input(f"[confirm] {prompt} [type {expect}]: ").strip()
        except EOFError:
            self.abort("no terminal input available (run interactively)")
        if ans != expect:
            self.abort("aborted by user (nothing flashed)")

    def _run(self, cmd, timeout=120):
        return subprocess.run(cmd, capture_output=True, text=True,
                              timeout=timeout)

    # ---- automation: environment, waiting, human handoffs ----
    def ask_path(self, prompt, must_exist=True):
        while True:
            if self.dry:
                self.log(f"would ask: {prompt}")
                return ""
            p = input(f"[input] {prompt} (empty aborts): ").strip()
            p = p.strip("'\"")
            if not p:
                self.abort("aborted by user (nothing changed)")
            if must_exist and not Path(p).is_file():
                print(f"not a file: {p} -- try again")
                continue
            return p

    def poll(self, fn, timeout, idle_msg, ok_msg="ready"):
        if self.dry:
            self.log(f"would poll: {idle_msg}")
            return True
        end = time.time() + timeout
        while time.time() < end:
            try:
                if fn():
                    self.log(ok_msg)
                    return True
            except Exception:  # noqa: BLE001
                pass
            time.sleep(5)
        self.abort(f"timed out waiting: {idle_msg}")

    def ensure_device(self):
        if self.dry:
            self.log("would wait for device + RSA authorization")
            return
        self._run([self.adb, "wait-for-device"], timeout=180)
        def authorized():
            r = self._run([self.adb, "devices"], timeout=30)
            return "\tdevice" in r.stdout
        if authorized():
            self.log("device authorized")
            return
        print("ON THE PHONE: enable Developer options (tap Build number 7x), "
              "enable USB debugging, plug the cable, then tap Allow + "
              "Always-allow on the RSA prompt.")
        self.poll(authorized, 300, "RSA authorization", "device authorized")

    def human(self, title, steps, verify=None):
        print(f"\n===== YOUR TURN: {title} =====")
        for i, s in enumerate(steps, 1):
            print(f"  {i}. {s}")
        if self.dry:
            self.log(f"would wait for: {title}")
            return True
        while True:
            input("[input] press Enter when done (or type ABORT): ")
            if verify is None:
                return True
            try:
                if verify():
                    self.log(f"verified: {title}")
                    return True
            except Exception as e:  # noqa: BLE001
                print(f"not yet: {e}")
            ans = input("[input] retry, SKIP, or ABORT? ").strip().lower()
            if ans == "abort":
                self.abort("aborted by user (nothing flashed)")
            if ans == "skip":
                self.log(f"skipped by user: {title}")
                return False

    def open_app(self, pkg):
        if self.dry:
            self.log(f"would open app: {pkg}")
            return
        self.shell("monkey", "-p", pkg, "-c",
                   "android.intent.category.LAUNCHER", "1")

    def open_settings(self, action):
        if self.dry:
            self.log(f"would open Settings: {action}")
            return
        self.shell("am", "start", "-a", action)

    def install_apk(self, path):
        if self.dry:
            self.log(f"would adb install: {path}")
            return
        r = self._run([self.adb, "install", str(path)], timeout=300)
        print((r.stdout + r.stderr)[-400:])
        if r.returncode != 0 and "ALREADY_EXISTS" not in (r.stdout + r.stderr):
            self.abort(f"adb install failed for {path}")

    def latest_download(self, pattern):
        out = self.shell("ls", "-t", "/sdcard/Download")
        import fnmatch as _fn
        for line in out.splitlines():
            name = line.strip().split()[-1]
            if _fn.fnmatch(name.lower(), pattern.lower()):
                return "/sdcard/Download/" + name
        return ""

    def battery_ok(self, minimum=30):
        if self.dry:
            self.log("would check battery")
            return True
        out = self.shell("dumpsys", "battery")
        level = 100
        for line in out.splitlines():
            if "level:" in line:
                try:
                    level = int(line.split(":")[1])
                except ValueError:
                    pass
        self.log(f"battery: {level}%")
        if level < minimum:
            self.confirm(f"battery {level}% under {minimum}%", expect="CHARGE_ANYWAY")
        return True

    def disk_ok(self, path, need_bytes):
        import shutil as _sh
        tgt = Path(path)
        base = tgt.parent if tgt.suffix else tgt
        free = _sh.disk_usage(base).free if not self.dry else need_bytes + 1
        self.log("disk free MB: %d need MB: %d" % (free // (1 << 20), need_bytes // (1 << 20)))
        if free < need_bytes:
            self.abort("not enough local disk (free space first)")
        return True

    def pull(self, remote, local):
        if self.dry:
            self.log(f"would pull: {remote} -> {local}")
            return local
        Path(local).parent.mkdir(parents=True, exist_ok=True)
        r = self._run([self.adb, "pull", remote, str(local)], timeout=600)
        if r.returncode != 0:
            self.abort(f"adb pull failed: {remote}")
        return local

    def photo(self, what):
        self.human("photograph the phone screen (screenshots are broken "
                   "on some builds)",
                   [f"make the phone show: {what}",
                    "take a clear photo with another camera",
                    "confirm the exact on-screen text back to the operator"])

    def adb_devices(self):
        if self.dry:
            return ["dry-device"]
        r = self._run([self.adb, "devices"], timeout=30)
        return [l.split()[0] for l in r.stdout.splitlines()[1:]
                if l.strip().split()[-1:] == ["device"]]

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
        if self.dry:
            self.log(f"would pull backup of {part}")
            return Path(outdir) / f"{part}_dry.img"
        out = Path(self.cfg.get("backup_root", outdir))
        out.mkdir(parents=True, exist_ok=True)
        ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        dst = out / f"{part}_backup_{ts}.img"
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


RED_ON = "\033[91m"
RED_OFF = "\033[0m"


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
    devs = ux.adb_devices()
    if not devs and not ux.dry:
        ux.abort("no adb device (cable? RSA prompt accepted? try setup phase)")
    one = " ".join(devs)
    model = ux.getprop("ro.product.model") if not ux.dry else "kansas"
    bb = ux.getprop("gsm.version.baseband") if not ux.dry else ""
    sku = ux.getprop("ro.boot.hardware.sku") if not ux.dry else "XT2513V"
    for pid, p in profs.items():
        bb_ok = (not p.get("baseband_substr") or p["baseband_substr"] in bb
                 or ux.dry)
        bb_variant = ""
        if not bb_ok:
            for alt in p.get("baseband_alt", []):
                if alt and alt in bb:
                    bb_ok, bb_variant = True, alt
                    break
        if (p.get("models") and model and
                any(m in model for m in p["models"]) and
                (not p.get("skus") or sku in p["skus"]) and bb_ok):
            ux.log(f"detected profile: {pid} ({p.get('label')})")
            if bb_variant:
                ux.log("baseband shows our own lab marker variant "
                       f"({bb_variant[:32]}...) — same build, recognized")
            return p
    ux.log(f"no profile matches model={model} sku={sku} "
           f"baseband={bb[:40]}")
    manual = (cfg.get("device_profile") or "").strip()
    if manual and manual in profs:
        ux.confirm(f"use profile {manual} for THIS device anyway?",
                   expect="OVERRIDE")
        return profs[manual]
    ux.abort("unsupported device (see devices/README.md to add one)")


def need_verified(profile, ux, experimental=False):
    if profile.get("status") != "verified-live":
        if experimental:
            ux.log(RED_ON + "UNTESTED PROFILE: proceeding under EXPERIMENTAL "
                   "rules (your offsets, full audit, your risk)" + RED_OFF)
            ux.confirm("accept EXPERIMENTAL on an untested profile?",
                       expect="EXPERIMENTAL")
            return
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


def phase_unlock_custom(ux, profile, cfg, workdir, args):
    """Experimental custom-table flow for untested profiles/devices.

    No shipped offsets are used here at all: the human brings their own
    RE-proven table (--patches), the tool contributes only machinery
    (full audit, old-byte gates, byte-exact diff discipline, re-sign,
    backup/flash/verify gates)."""
    need_verified(profile, ux, experimental=True)
    if ux.dry:
        ux.log("dry-run: would backup -> custom build (your table) -> "
               "flash slot A -> verify")
        return True
    if not args.experimental:
        ux.abort("custom unlock flow requires --experimental")
    if not args.patches:
        ux.log("custom flow needs --patches TABLE.json (your offsets, "
               "proven by your own hardware work like ours was)")
        return False
    import unlock as U
    workdir = Path(workdir)
    workdir.mkdir(parents=True, exist_ok=True)
    bdir = workdir / "backups"
    ux.log("== modem backup (EXPERIMENTAL run) ==")
    U.cmd_backup(argparse_Namespace(out=str(bdir)))
    stock = (cfg.get("firmware_files", {}) or {}).get("md1img_stock", "")
    if not stock:
        ux.log("set config firmware_files.md1img_stock to the factory image")
        return False
    bfile = workdir / "custom_work.img"
    ux.log("== custom build (audit + your table + diff discipline) ==")
    U.cmd_custom(argparse_Namespace(stock=stock, patches=args.patches,
                                    out=str(bfile), experimental=True))
    signed = bfile.with_suffix(".signed.img")
    ux.log("== modem flash (slot A only, EXPERIMENTAL) ==")
    U.cmd_flash(argparse_Namespace(image=str(signed), backup=str(bdir)))
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
    ap.add_argument("--experimental", action="store_true",
                    help="red-banner mode: untested profiles + custom tables")
    ap.add_argument("--patches",
                    help="user patch-table JSON (experimental custom flow)")
    ap.add_argument("--phases", default="setup,detect,flash,root,unlock",
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
    if args.experimental:
        print(RED_ON + "=" * 70)
        print(RED_ON + "EXPERIMENTAL MODE: untested devices/flows. "
              "Bricks are YOUR risk." + RED_OFF)
        print(RED_ON + "Full-parse audit + byte-exact discipline still "
              "enforced." + RED_OFF)
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
        if phase == "setup":
            ux.log("python %s" % sys.version.split()[0])
            ux.ensure_tools()
            ux.log("adb=%s fastboot=%s" % (ux.adb, ux.fastboot))
            ux.ensure_device()
            ux.battery_ok()
            ux.disk_ok(Path(args.work), 1 << 30)
        elif phase == "detect":
            ux.device_health("detect")
        elif phase == "bootloader":
            if ux.dry:
                ux.log("dry-run: would guide bootloader unlock (key prompt)")
            else:
                import unlock as _U
                _U.cmd_bootloader(argparse_Namespace())
        elif phase == "flash":
            need_verified(profile, ux, args.experimental)
            import flash_mod as _f  # noqa: F811
            if not _f.run_flash_plan(ux, profile, cfg):
                ux.log("flash phase incomplete (provide missing images, re-run)")
                save()
                return 1
        elif phase == "root":
            need_verified(profile, ux, args.experimental)
            if not root_mod.run_root_phase(ux, profile, cfg):
                ux.log("root phase incomplete (finish on-device steps, re-run)")
                save()
                return 1
        elif phase == "unlock":
            if profile.get("status") == "verified-live" and not args.patches:
                ok = phase_unlock(ux, profile, cfg, Path(args.work))
            else:
                ok = phase_unlock_custom(ux, profile, cfg, Path(args.work),
                                         args)
            if not ok:
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
