#!/usr/bin/env python3
"""postroot.py — non-interactive post-root flow: detect -> backup -> build -> verify.

Starts PAST rooting/flashing setup: it assumes the bootloader is unlocked
and root is present, verifies both with bounded machine checks, then runs
the modem backup + patch build + verify with ZERO prompts.

Interaction contract (there is exactly one place a human may be needed):
  - Magisk/KernelSU "grant root to Shell" tap, once, on the phone. The su
    probe waits a bounded --timeout then fails fast telling you to tap and
    re-run. Nothing hangs forever.
  - The single destructive step (fastboot flash) NEVER runs implicitly:
    without --flash this script stops after the build and prints the exact
    flash command. With --flash, the flag itself is the confirmation
    (logged). Low battery (<30%) still refuses to flash unless
    --charge-anyway is passed.

Usage:
  python postroot.py [--config mylab-nevada.json] [--work work-nevada]
  python postroot.py --config mylab-nevada.json --experimental \\
      --patches nevada_table.DRAFT.json
  python postroot.py --config mylab-nevada.json --experimental \\
      --patches nevada_table.DRAFT.json --flash   # the one destructive step

Exit codes: 0 ok, 2 refused/blocked (nothing destructive touched), 1 error.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))


def say(msg):
    print(f"[postroot] {msg}")


class Blocked(Exception):
    """Precondition failed: printed plainly, exit 2, nothing flashed."""


def run(cmd, timeout):
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def resolve_tool(name):
    p = os.environ.get(name.upper() + "_PATH", "")
    if p and Path(p).is_file():
        return p
    vendored = HERE / "vendor" / "platform-tools" / name
    if vendored.is_file():
        return str(vendored)
    found = shutil.which(name)
    if found:
        return found
    raise Blocked(f"missing tool: {name} (run: python bootstrap.py --yes, "
                  f"or set {name.upper()}_PATH)")


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(4 << 20), b""):
            h.update(b)
    return h.hexdigest()


def step_env(args):
    if sys.version_info < (3, 10):
        raise Blocked(f"python {sys.version.split()[0]} < 3.10")
    say(f"python {sys.version.split()[0]} OK")
    adb = resolve_tool("adb")
    fastboot = resolve_tool("fastboot")
    os.environ["ADB_PATH"] = adb
    os.environ["FASTBOOT_PATH"] = fastboot
    say(f"tools: adb={adb} fastboot={fastboot}")
    return adb, fastboot


def adb_devices(adb):
    r = run([adb, "devices"], 30)
    return [l.split()[0] for l in r.stdout.splitlines()[1:]
            if l.strip().split()[-1:] == ["device"]]


def step_device(args, adb):
    run([adb, "wait-for-device"], 90)
    devs = adb_devices(adb)
    if len(devs) != 1:
        raise Blocked(f"need exactly 1 authorized adb device, see: {devs} "
                      f"(enable USB debugging, accept the RSA prompt)")
    say(f"device: {devs[0]} (authorized)")
    return devs[0]


def shell(adb, *args, timeout=30):
    r = run([adb, "shell", *args], timeout)
    if "no devices" in (r.stderr + r.stdout).lower():
        raise Blocked("adb device lost (cable? RSA prompt accepted?)")
    return r.stdout.strip()


def step_fingerprint(args, adb):
    props = {}
    for k in ("ro.product.model", "ro.boot.hardware.sku",
              "gsm.version.baseband", "gsm.sim.state",
              "ro.boot.slot_suffix", "sys.boot_completed",
              "ro.build.display.id", "ro.boot.flash.locked",
              "ro.boot.verifiedbootstate"):
        try:
            props[k] = shell(adb, "getprop", k)
        except Exception as e:  # noqa: BLE001
            props[k] = f"<error {e}>"
    try:
        props["remain"] = shell(adb, "getprop | grep remain.count")
    except Exception:  # noqa: BLE001
        props["remain"] = "<unknown>"
    for k, v in props.items():
        say(f"  {k} = {v[:90]}")
    locked = props.get("ro.boot.flash.locked", "")
    if locked not in ("0", ""):
        # "" = property absent on some builds; 0 = unlocked, 1 = locked.
        if locked == "1":
            raise Blocked("bootloader reports locked (flash.locked=1); "
                          "unlock first, then re-run")
    say("bootloader: unlocked (flash.locked=0, orange state)" if locked == "0"
        else "bootloader: lock state unreadable, flash preflight will verify")
    return props


def step_root(args, adb):
    try:
        r = run([adb, "shell", "su", "-c", "id"], args.timeout)
    except subprocess.TimeoutExpired:
        raise Blocked(
            "su probe timed out: root daemon is present but Shell has no "
            "grant yet. ON THE PHONE: open the Magisk/KernelSU manager -> "
            "Superuser -> allow Shell (or set auto-allow), then re-run. "
            "This one tap cannot be scripted.")
    out = (r.stdout + r.stderr).strip()
    if r.returncode != 0 or "uid=0" not in out:
        raise Blocked(f"root not granted to adb shell ({out[:120]!r}); tap "
                      f"Allow on the phone's superuser prompt, then re-run")
    say("root: OK (su -> uid 0)")


def battery_level(adb):
    try:
        out = shell(adb, "dumpsys", "battery")
    except Exception:  # noqa: BLE001
        return -1
    for line in out.splitlines():
        if "level:" in line:
            try:
                return int(line.split(":")[1])
            except ValueError:
                pass
    return -1


def step_battery(args, adb):
    lvl = battery_level(adb)
    if lvl < 0:
        say("battery: unreadable, continuing (flash still gated on charge)")
        return
    say(f"battery: {lvl}%")
    if lvl < 30 and args.flash and not args.charge_anyway:
        raise Blocked(f"battery {lvl}% < 30%: refusing to flash (a mid-flash "
                      f"power loss bricks modems). Charge, then re-run "
                      f"(or pass --charge-anyway to own the risk)")


def step_backup(args, adb):
    import unlock as U
    out = Path(args.work) / "backups"
    if sorted(out.glob("md1img_a_backup_*.img")):
        say(f"backup already present in {out}, skipping re-pull")
        return
    say("pulling live modem backup (~200MB, minutes) -- mandatory")
    t0 = time.time()
    try:
        rc = U.cmd_backup(argparse.Namespace(out=str(out)))
    except U.Refuse as e:
        raise Blocked(f"backup refused: {e}")
    if rc:
        raise Blocked("backup pull failed (see above)")
    say(f"backup done in {int(time.time() - t0)}s")


def step_build(args, cfg):
    import unlock as U
    stock = (cfg.get("firmware_files", {}) or {}).get("md1img_stock", "")
    if not stock or not Path(stock).is_file():
        raise Blocked("config firmware_files.md1img_stock is not set to a "
                      "file (point it at YOUR factory md1img, then re-run)")
    out = Path(args.work) / ("custom_work.img" if args.patches
                             else "patched_work.img")
    try:
        if args.patches:
            if not args.experimental:
                raise Blocked("custom table needs --experimental")
            say("building from YOUR table (experimental custom flow: "
                "full-parse audit + old-byte gates + byte-exact diff "
                "discipline)...")
            rc = U.cmd_custom(argparse.Namespace(
                stock=stock, patches=args.patches, out=str(out),
                experimental=True))
        else:
            say("build-testing the shipped kansas table against your stock "
                "(expect REFUSE on non-kansas firmware)...")
            rc = U.cmd_build(argparse.Namespace(stock=stock, out=str(out)))
    except U.Refuse as e:
        raise Blocked(f"build refused: {e}")
    if rc:
        raise Blocked("build step failed (see above)")
    say(f"build OK -> {out.with_suffix('.signed.img')}")
    return out.with_suffix(".signed.img")


def step_flash(args, signed):
    import unlock as U
    bdir = str(Path(args.work) / "backups")
    if not args.flash:
        say("NOT flashing (no --flash). When ready, run:")
        say(f"  python unlock.py flash --image {signed} --backup {bdir}")
        say("  python unlock.py verify")
        return
    say(f"FLASHING {signed.name} to md1img_a (explicit --flash, logged)")
    try:
        rc = U.cmd_flash(argparse.Namespace(image=str(signed), backup=bdir))
    except U.Refuse as e:
        raise Blocked(f"flash refused: {e}")
    if rc:
        raise Blocked("flash step failed (see above)")
    say("flash sent; device rebooting. Waiting 90s before verify...")
    time.sleep(90)


def step_verify(args):
    import unlock as U
    say("post-step verify:")
    try:
        rc = U.cmd_verify(argparse.Namespace())
    except U.Refuse as e:
        raise Blocked(f"verify refused: {e}")
    if rc:
        raise Blocked("verify reported an issue (see above)")


def save_state(args, phases):
    try:
        p = Path(args.work) / "state.json"
        p.parent.mkdir(parents=True, exist_ok=True)
        done = []
        if p.is_file():
            done = json.loads(p.read_text()).get("done", [])
        for ph in phases:
            if ph not in done:
                done.append(ph)
        p.write_text(json.dumps({"done": done,
                                 "utc": datetime.now(timezone.utc).isoformat(),
                                 "config": args.config}, indent=1))
    except Exception as e:  # noqa: BLE001
        say(f"(state save skipped: {e})")


def main():
    ap = argparse.ArgumentParser(description="non-interactive post-root flow")
    ap.add_argument("--config", default="mylab-nevada.json")
    ap.add_argument("--work", default="work-nevada")
    ap.add_argument("--flash", action="store_true",
                    help="perform the ONE destructive step (else build-only)")
    ap.add_argument("--charge-anyway", action="store_true",
                    help="flash even under 30%% battery (your risk)")
    ap.add_argument("--timeout", type=int, default=25,
                    help="seconds to wait for the su grant (default 25)")
    ap.add_argument("--experimental", action="store_true",
                    help="allow custom-table flow (red-banner rules)")
    ap.add_argument("--patches", default="",
                    help="your patch-table JSON (experimental custom flow)")
    ap.add_argument("--skip-backup", action="store_true",
                    help="reuse existing backup dir without pulling")
    args = ap.parse_args()

    if args.experimental:
        print("\033[91m" + "=" * 70 + "\033[0m")
        print("\033[91mEXPERIMENTAL MODE: untested device/table. "
              "Bricks are YOUR risk.\033[0m")

    cfg_path = Path(args.config)
    cfg = json.loads(cfg_path.read_text()) if cfg_path.is_file() else {}
    if not cfg_path.is_file():
        say(f"WARNING: {args.config} not found, using empty config")

    # unlock.py resolves tool paths at import; set env first, then import.
    adb, fastboot = step_env(args)
    import unlock as U
    if args.flash or args.experimental:
        # Non-interactive answers for unlock.py's typed prompts. The flags
        # themselves are the confirmation; everything is logged.
        def auto_ask(prompt):
            ans = "EXPERIMENTAL" if "EXPERIMENTAL" in prompt else "YES"
            say(f"auto-confirm (--flash/--experimental): {prompt.strip()[:70]}")
            return ans
        U._ASK = auto_ask
    else:
        U._ASK = lambda prompt: (_ for _ in ()).throw(
            Blocked(f"would prompt {prompt!r}: re-run with --flash "
                    f"(or --experimental) to authorize non-interactively"))

    step_device(args, adb)
    step_fingerprint(args, adb)
    step_battery(args, adb)
    # Build is fully local (no device writes): validate firmware gates even
    # while the one-time root grant is still pending.
    signed = step_build(args, cfg)
    save_state(args, ["setup", "detect", "build"])
    step_root(args, adb)
    save_state(args, ["root-check"])
    if not args.skip_backup:
        step_backup(args, adb)
    save_state(args, ["backup"])
    step_flash(args, signed)
    if args.flash:
        save_state(args, ["flash"])
        step_verify(args)
        save_state(args, ["verify"])
    say("done. Nothing flashed" if not args.flash else "done.")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Blocked as e:
        print(f"BLOCKED: {e}")
        sys.exit(2)
    except subprocess.TimeoutExpired as e:
        print(f"BLOCKED: timed out waiting ({e}); check cable/device, re-run")
        sys.exit(2)
    except KeyboardInterrupt:
        print("interrupted (nothing flashed by this script unless --flash "
              "was passed and the flash step had started)")
        sys.exit(130)
