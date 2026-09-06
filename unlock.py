#!/usr/bin/env python3
"""kansas-modem-unlock — SIM-lock patch tool, Moto G 5G (2025) XT2513V.

Scope (read README.md first): this exact model + modem build ONLY. Every
gate below refuses anything else. Nothing here writes identity (no IMEI,
no NCK, no attempt counters); it patches the lock *evaluation* and re-signs
the image with the CERT2 hash-override flow the unlocked bootloader accepts.

Prerequisites the tool CANNOT do for you (it checks, guides, stops):
  1. Bootloader already unlocked (per-device LK process + owner key ceremony).
  2. Root working (KernelSU `su` path).
  3. Your own factory md1img for this build (--stock), integrity-checked.
  4. adb + fastboot on PATH, one device attached, cable steady, battery charged.

Usage:
  python unlock.py status [--stock FILE]
  python unlock.py backup --out DIR
  python unlock.py build --stock FILE --out FILE
  python unlock.py flash --image FILE --backup DIR
  python unlock.py verify
  python unlock.py revert --backup DIR
  python unlock.py full --stock FILE --work DIR     (backup+build+flash+verify)

Only stdlib is used. Tested flow: backup -> build (old-byte gates + re-sign)
-> flash slot md1img_a ONLY -> reboot -> verify (baseband alive, SIM state,
remain counter untouched, zero modem exceptions).
"""
from __future__ import annotations

import argparse
import getpass
import hashlib
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import patches
import imageaudit
from patches import EXPECT_MODEL_SUBSTR, EXPECT_SKU, EXPECT_BASEBAND_SUBSTR
from patches import EXPECT_SECURESTATE, EXPECT_STOCK_SIZE, EXPECT_STOCK_SHA256
from patches import EXPECT_VERSION_OFF, EXPECT_VERSION, PATCHES

ADB = os.environ.get("ADB_PATH") or shutil.which("adb") or "adb"
FASTBOOT = os.environ.get("FASTBOOT_PATH") or shutil.which("fastboot") or "fastboot"
SLOT = "md1img_a"          # ONLY slot ever flashed. md1img_b is never named.
FORBIDDEN = ("md1img_b", "preloader", "lk", "gpt", "pgpt",
             "efuse", "efuseBackup", "seccfg", "nvram", "nvdata",
             "protect1", "protect2")


class Refuse(Exception):
    """Gate refusal: printed plainly, exit code 2, nothing touched."""


# Injectable prompts (GUI front-ends set these to dialog-backed functions;
# CLI default is terminal input). Signatures: _ASK(prompt)->str,
# _ASK_SECRET(prompt)->str (never logged).
_ASK = None
_ASK_SECRET = None


def _ask(prompt):
    if _ASK is not None:
        return _ASK(prompt)
    return input(prompt)


def _ask_secret(prompt):
    if _ASK_SECRET is not None:
        return _ASK_SECRET(prompt)
    return getpass.getpass(prompt)


def red(text):
    return f"\033[91m{text}\033[0m"


def banner_experimental():
    print(red("=" * 70))
    print(red("EXPERIMENTAL MODE: untested device/flow. Bricks are YOUR risk."))
    print(red("Full-parse audit + byte-exact discipline still enforced;"))
    print(red("human judgment is not optional here."))
    print(red("=" * 70))


def load_table(path):
    """User patch table JSON: [{label, offset ("0x.." or int), old (hex),
    new (hex), why}]. Also accepts {"patches": [...]} with extra metadata
    keys (device, status, stock_md1img) carried alongside but unused here.
    Returns [(label, int, bytes, bytes, why)]."""
    import json as _json
    raw = _json.loads(Path(path).read_text())
    if isinstance(raw, dict):
        raw = raw.get("patches", [])
    out = []
    for e in raw:
        off = e["offset"]
        off = int(off, 16) if isinstance(off, str) else int(off)
        out.append((str(e.get("label", f"@{off:#x}")), off,
                    bytes.fromhex(e["old"]), bytes.fromhex(e["new"]),
                    str(e.get("why", ""))))
    if not out:
        raise Refuse("empty patch table")
    return out


def audit_or_refuse(path, what):
    ok, rep = imageaudit.audit_image(path)
    print(imageaudit.render(rep))
    if not ok:
        raise Refuse(f"{what} failed full-parse audit (see above)")
    return rep


def run(cmd, **kw):
    try:
        return subprocess.run(cmd, capture_output=True, text=True,
                              timeout=kw.pop("timeout", 120), **kw)
    except FileNotFoundError:
        raise Refuse(f"missing tool: {cmd[0]} (install platform-tools, add to PATH)")


def _vendor_bin(name):
    exe = name + (".exe" if os.name == "nt" else "")
    p = Path(__file__).resolve().parent / "vendor" / "platform-tools" / exe
    return str(p) if p.is_file() else ""


def need_tools():
    for label, t in (("adb", ADB), ("fastboot", FASTBOOT)):
        if shutil.which(t) is None and not Path(t).is_file():
            vendored = _vendor_bin(label)
            if vendored:
                globals()[label.upper()] = vendored
                continue
            raise Refuse(
                f"missing tool: {label} (run: python bootstrap.py --yes, "
                f"or set {label.upper()}_PATH, or add platform-tools to PATH)")


def adb(*args, timeout=60):
    r = run([ADB, *args], timeout=timeout)
    if r.returncode != 0 and "no devices" in (r.stderr + r.stdout).lower():
        raise Refuse("no device visible to adb (cable? RSA prompt accepted?)")
    return r


def adb_shell(*args, timeout=60):
    r = adb("shell", *args, timeout=timeout)
    return r.stdout.strip()


def one_device():
    out = adb("devices").stdout.splitlines()[1:]
    devs = [l.split()[0] for l in out if l.strip() and "device" in l.split()[1:2]]
    if len(devs) != 1:
        raise Refuse(f"need exactly 1 adb device, see: {devs}")
    return devs[0]


def getprop(name):
    return adb_shell(f"getprop {name}")


def check_root():
    r = adb_shell("su -c id")
    if "uid=0" not in r:
        raise Refuse(f"root not working (su -c id -> {r!r}); KernelSU root required")


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(4 << 20), b""):
            h.update(b)
    return h.hexdigest()


def cmd_status(_args):
    need_tools()
    one_device()
    print(f"model        : {getprop('ro.product.model')}")
    print(f"sku          : {getprop('ro.boot.hardware.sku')}")
    print(f"baseband     : {getprop('gsm.version.baseband')}")
    print(f"sim state    : {getprop('gsm.sim.state')}")
    pro = getprop("gsm.sim.state")
    print(f"remain       : {adb_shell('getprop | grep remain.count')}")
    print(f"slot         : {getprop('ro.boot.slot_suffix')}")
    ee = adb_shell("dmesg | grep -ciE 'modem exception|MD exception|assert fail|reset MD|exception stage|fatal error' || true")
    print(f"modem EE count (dmesg): {ee}")
    ok_model = EXPECT_MODEL_SUBSTR in getprop("ro.product.model")
    ok_sku = getprop("ro.boot.hardware.sku") == EXPECT_SKU
    ok_bb = EXPECT_BASEBAND_SUBSTR in getprop("gsm.version.baseband")
    print(f"fingerprint  : model={ok_model} sku={ok_sku} baseband={ok_bb}")
    if not (ok_model and ok_sku and ok_bb):
        print("REFUSED for patching: this tool only serves the fingerprinted build.")
        print("See README.md (why refusal is the anti-brick core).")
    try:
        check_root()
        print("root         : OK (su -> uid 0)")
    except Refuse as e:
        print(f"root         : MISSING ({e})")
    return 0


def cmd_backup(args):
    need_tools()
    one_device()
    check_root()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    dst = out / f"md1img_a_backup_{ts}.img"
    print(f"pulling live {SLOT} (200MB, minutes) -> {dst}")
    r = run([ADB, "pull", f"/dev/block/by-name/{SLOT}", str(dst)], timeout=900)
    method = "adb-pull"
    if r.returncode != 0:
        # Stock adbd cannot read block devices on most builds; same bytes
        # via the (already verified) root shell. Read-only either way.
        print("direct pull refused; retrying via su dd (read-only) ...")
        with open(dst, "wb") as f:
            r2 = subprocess.run(
                [ADB, "exec-out", "su", "-c",
                 f"dd if=/dev/block/by-name/{SLOT} bs=4M 2>/dev/null"],
                stdout=f, timeout=1800)
        if r2.returncode != 0 or not dst.is_file() or dst.stat().st_size == 0:
            raise Refuse(f"backup pull failed (pull: {r.stderr[-300:]}; "
                         f"dd rc={r2.returncode})")
        method = "su-dd"
    h = sha256(dst)
    (out / "BACKUP_MANIFEST.txt").write_text(
        f"slot={SLOT} file={dst.name} sha256={h} utc={ts} method={method}\n"
        f"DO NOT LOSE THIS FILE: it is the revert path.\n")
    print(f"backup sha256: {h}")
    return 0


def check_stock(path):
    p = Path(path)
    if not p.is_file():
        raise Refuse(f"stock file not found: {p}")
    size = p.stat().st_size
    if size != EXPECT_STOCK_SIZE:
        raise Refuse(f"stock size {size} != expected {EXPECT_STOCK_SIZE} (wrong build?)")
    h = sha256(p)
    if h != EXPECT_STOCK_SHA256:
        raise Refuse("stock sha256 mismatch (wrong build or corrupt file?)")
    data = p.read_bytes()
    v = data[EXPECT_VERSION_OFF:EXPECT_VERSION_OFF + len(EXPECT_VERSION)]
    if v != EXPECT_VERSION:
        raise Refuse("stock version literal mismatch (wrong build content?)")
    print(f"stock OK: size={size} sha256={h[:12]}... version literal OK")
    return data


def cmd_build(args):
    data = bytearray(check_stock(args.stock))
    audit_or_refuse(args.stock, "stock image")
    audit_or_refuse(args.stock, "stock image")
    applied = []
    for label, off, old_hx, new_hx, _why in PATCHES:
        old, new = bytes.fromhex(old_hx), bytes.fromhex(new_hx)
        if bytes(data[off:off + len(old)]) != old:
            raise Refuse(
                f"patch {label}: expected {old_hx} at {off:#x}, "
                f"found {bytes(data[off:off+len(old)]).hex()} — refusing "
                f"(image is not the fingerprinted build)")
        data[off:off + len(new)] = new
        applied.append(label)
    tmp = Path(args.out)
    tmp.parent.mkdir(parents=True, exist_ok=True)
    tmp.write_bytes(bytes(data))
    here = Path(__file__).resolve().parent
    signer = here / "sign_mtk_cert.py"
    if not signer.is_file():
        raise Refuse("sign_mtk_cert.py missing next to unlock.py")
    signed = tmp.with_suffix(".signed.img")
    r = run([sys.executable, str(signer), "-w", str(tmp), "-o", str(signed)],
            timeout=300)
    tail = (r.stdout + r.stderr)[-800:]
    if r.returncode != 0 or "Write complete" not in tail:
        raise Refuse(f"re-sign failed:\n{tail}")
    print(f"signed image: {signed} sha256={sha256(signed)[:12]}...")
    print(f"applied[{len(applied)}]: {', '.join(applied)}")
    print("verify on device after flashing (unlock.py verify).")
    return 0


def fastboot(args, timeout=120):
    r = run([FASTBOOT, *args], timeout=timeout)
    out = r.stdout + r.stderr
    return r.returncode, out


def cmd_flash(args):
    img = Path(args.image)
    if not img.is_file():
        raise Refuse(f"image not found: {img}")
    bdir = Path(args.backup)
    if not any(bdir.glob("md1img_a_backup_*.img")):
        raise Refuse(f"no backup found in {bdir} (run: unlock.py backup first)")
    if img.name == "md1img_b" or SLOT != "md1img_a":
        raise Refuse("internal guard tripped")  # can never happen; belt+suspenders
    for bad in FORBIDDEN[1:]:
        if bad in img.name:
            raise Refuse(f"refusing: filename looks like {bad}")
    print("rebooting to bootloader for preflight (nothing flashed yet)...")
    adb("reboot", "bootloader")
    time.sleep(12)
    rc, out = fastboot(["devices"])
    if "fastboot" not in out:
        raise Refuse("device not in fastboot (cable? Vol-Down+Power, cable-insert trick)")
    rc, out = fastboot(["getvar", "securestate"])
    if EXPECT_SECURESTATE not in out:
        raise Refuse(f"bootloader not unlocked (securestate != {EXPECT_SECURESTATE}). "
                     f"Unlock first (per-device LK process); refusing to flash.")
    rc, out = fastboot(["getvar", "current-slot"])
    print(f"preflight: unlocked, {out.strip().splitlines()[-1] if out.strip() else '?'}")
    print(f"ABOUT TO FLASH {SLOT} with {img.name} ({img.stat().st_size} bytes).")
    print("Slot B is never touched. Revert = unlock.py revert --backup DIR.")
    ans = _ask("type YES in capitals to flash: ").strip()
    if ans != "YES":
        raise Refuse("aborted by user (nothing flashed)")
    rc, out = fastboot(["flash", SLOT, str(img)], timeout=300)
    print(out[-400:])
    if rc != 0 or "OKAY" not in out:
        raise Refuse("fastboot flash failed (see above); try again or revert")
    fastboot(["reboot"])
    print("rebooting; run: unlock.py verify (after ~90s)")
    return 0


def cmd_verify(_args):
    need_tools()
    one_device()
    remain = adb_shell("getprop | grep remain.count")
    print(f"remain: {remain}")
    if "[5]" not in remain:
        print("WARNING: remain counter moved. Report, do not proceed.")
    ee = adb_shell("dmesg | grep -ciE 'modem exception|MD exception|assert fail|reset MD|exception stage|fatal error' || true")
    print(f"modem EE count: {ee}")
    print(f"baseband: {getprop('gsm.version.baseband')}")
    print(f"sim state: {getprop('gsm.sim.state')}")
    state = getprop("gsm.sim.state")
    if "NETWORK_LOCKED" in state:
        print("SIM still network-locked on the evaluated path.")
        print("Read README.md (scope: which doors this build opens).")
    elif "LOADED" in state:
        print("SIM state LOADED: lock evaluation passes for the inserted SIM.")
    else:
        print(f"SIM state {state}: compare with README.md expectations.")
    return 0


def cmd_revert(args):
    bdir = Path(args.backup)
    cands = sorted(bdir.glob("md1img_a_backup_*.img"))
    if not cands:
        raise Refuse(f"no backup in {bdir}")
    img = cands[-1]
    print(f"reverting {SLOT} to {img.name} (stock backup).")
    adb("reboot", "bootloader")
    time.sleep(12)
    rc, out = fastboot(["devices"])
    if "fastboot" not in out:
        raise Refuse("device not in fastboot")
    ans = _ask("type YES in capitals to revert: ").strip()
    if ans != "YES":
        raise Refuse("aborted by user (nothing flashed)")
    rc, out = fastboot(["flash", SLOT, str(img)], timeout=300)
    print(out[-400:])
    if rc != 0 or "OKAY" not in out:
        raise Refuse("revert flash failed; retry, check cable")
    fastboot(["reboot"])
    print("reverted; run: unlock.py verify")
    return 0


def cmd_custom(args):
    if not args.experimental:
        raise Refuse("custom firmware flow requires --experimental "
                     "(untested device/table)")
    ans = _ask("type EXPERIMENTAL in capitals to proceed: ").strip()
    if ans != "EXPERIMENTAL":
        raise Refuse("aborted by user (nothing touched)")
    sp = Path(args.stock)
    if not sp.is_file():
        raise Refuse(f"stock file not found: {sp}")
    audit_or_refuse(sp, "stock image (intact-factory proof lives here: "
                    "stored-vs-recomputed digests must match pre-patch)")
    table = load_table(args.patches)
    data = bytearray(sp.read_bytes())
    for label, off, old, new, _why in table:
        if off < 0 or off + len(old) > len(data) or len(old) != len(new):
            raise Refuse(f"table entry {label}: bad range/size")
        if bytes(data[off:off + len(old)]) != old:
            raise Refuse(f"table entry {label}: expected {old.hex()} at "
                         f"{off:#x}, found "
                         f"{bytes(data[off:off+len(old)]).hex()} — refusing")
        data[off:off + len(new)] = new
    tmp = Path(args.out)
    tmp.parent.mkdir(parents=True, exist_ok=True)
    tmp.write_bytes(bytes(data))
    ok, rep = imageaudit.compare_built(sp.read_bytes(), bytes(data),
                                       [(l, o, ol, nw) for l, o, ol, nw, _w
                                        in table])
    if not ok:
        raise Refuse(f"byte-exact discipline failed: {rep['unexplained']}")
    print(f"diff audit: {len(rep['runs'])} runs, all declared. OK")
    here = Path(__file__).resolve().parent
    r = run([sys.executable, str(here / "sign_mtk_cert.py"), "-w",
             str(tmp), "-o", str(tmp.with_suffix(".signed.img"))],
            timeout=300)
    tail = (r.stdout + r.stderr)[-800:]
    if r.returncode != 0 or "Write complete" not in tail:
        raise Refuse(f"re-sign failed:\n{tail}")
    signed = tmp.with_suffix(".signed.img")
    ok2, rep2 = imageaudit.audit_image(signed)
    print(imageaudit.render(rep2))
    if not ok2:
        raise Refuse("signed output failed re-parse audit")
    (tmp.parent / "MANIFEST-custom.txt").write_text(
        f"mode=EXPERIMENTAL-custom stock={sp.name} "
        f"out={signed.name} sha256={sha256(signed)} "
        f"table={[t[0] for t in table]}\n"
        f"key material: none used, none stored (user key never touches "
        f"this flow)\n")
    print(f"custom build complete: {signed}")
    print("flash only via: unlock.py flash --image ... --backup ... "
          "(same gates: backup present, unlocked BL, typed YES)")
    return 0


def cmd_bootloader(_args):
    print("Bootloader unlock helper (official-style flow only).");
    print("WARNING: unlocking typically FACTORY-RESETS userdata (vendor "
          "behavior). Back up the phone first. This tool performs no "
          "LK/bootloader patching of its own.")
    adb("reboot", "bootloader")
    time.sleep(12)
    rc, out = fastboot(["devices"])
    if "fastboot" not in out:
        raise Refuse("device not in fastboot")
    rc, out = fastboot(["getvar", "securestate"])
    if EXPECT_SECURESTATE in out:
        print("already unlocked; nothing to do")
        return 0
    print("If your vendor offers an official key flow (e.g. Motorola unlock "
          "portal):")
    rc, out = fastboot(["oem", "get_unlock_data"])
    print(out[-600:])
    print("Take that unlock data to the VENDOR portal, retrieve YOUR key, "
          "then continue. (Some devices instead need 'fastboot flashing "
          "unlock' + on-screen confirm — follow vendor docs.)")
    ans = _ask("have YOUR vendor-issued key ready? type YES to enter it "
               "(or anything else to stop): ").strip()
    if ans != "YES":
        raise Refuse("stopped (nothing changed)")
    key = _ask_secret("paste unlock key (hidden, never stored/logged): "
                      ).strip()
    try:
        if not key:
            raise Refuse("empty key (nothing sent)")
        rc, out = fastboot(["oem", "unlock", key], timeout=180)
        print(out[-400:])
    finally:
        key = "0" * 64
        del key
    rc, out = fastboot(["getvar", "securestate"])
    if EXPECT_SECURESTATE in out or "unlocked" in out.lower():
        print("bootloader reports unlocked")
        return 0
    raise Refuse("still locked (see output above); follow vendor guidance")


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0],
                                 formatter_class=argparse.RawDescriptionHelpFormatter,
                                 epilog=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)
    ap.add_argument("--experimental", action="store_true",
                    help="enable EXPERIMENTAL flows (red banner; custom "
                    "firmware tables; untested devices at your own risk)")
    sub.add_parser("status")
    p = sub.add_parser("backup")
    p.add_argument("--out", required=True)
    p = sub.add_parser("build")
    p.add_argument("--stock", required=True)
    p.add_argument("--out", required=True)
    p = sub.add_parser("flash")
    p.add_argument("--image", required=True)
    p.add_argument("--backup", required=True)
    sub.add_parser("verify")
    p = sub.add_parser("revert")
    p.add_argument("--backup", required=True)
    p = sub.add_parser("custom")
    p.add_argument("--stock", required=True)
    p.add_argument("--patches", required=True,
                   help="your patch table JSON [{label,offset,old,new,why}]")
    p.add_argument("--out", required=True)
    sub.add_parser("bootloader")
    p = sub.add_parser("full")
    p.add_argument("--stock", required=True)
    p.add_argument("--work", required=True)
    args = ap.parse_args()
    if args.experimental:
        banner_experimental()
    try:
        if args.cmd == "status":
            return cmd_status(args)
        if args.cmd == "backup":
            return cmd_backup(args)
        if args.cmd == "build":
            return cmd_build(args)
        if args.cmd == "flash":
            return cmd_flash(args)
        if args.cmd == "verify":
            return cmd_verify(args)
        if args.cmd == "revert":
            return cmd_revert(args)
        if args.cmd == "custom":
            return cmd_custom(args)
        if args.cmd == "bootloader":
            return cmd_bootloader(args)
        if args.cmd == "full":
            work = Path(args.work)
            work.mkdir(parents=True, exist_ok=True)
            bdir = work / "backups"
            bfile = work / "patched_work.img"
            steps = [
                ("backup", lambda: cmd_backup(argparse.Namespace(out=str(bdir)))),
                ("build", lambda: cmd_build(argparse.Namespace(
                    stock=args.stock, out=str(bfile)))),
            ]
            for name, fn in steps:
                print(f"=== {name} ===")
                rc = fn()
                if rc:
                    return rc
            signed = bfile.with_suffix(".signed.img")
            print("=== flash ===")
            print("Review the build output above, then flash explicitly:")
            print(f"  python unlock.py flash --image {signed} --backup {bdir}")
            print("Then: python unlock.py verify")
            return 0
    except Refuse as e:
        print(f"REFUSED: {e}")
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
