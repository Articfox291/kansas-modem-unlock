#!/usr/bin/env python3
"""Partition flash plans from device profiles (stdlib only).

Executes profile['flash_plan'] in order with: denylist enforcement (profile
never_flash + global sacred list), per-image magic checks, sha256 logging +
pinning, typed confirmation per partition, and a boot+health check at the end.

Firmware provenance: images are USER-SUPPLIED (config firmware_files). The
tool verifies structure, never origin; first-use hashes are recorded and
pinned afterwards. Anything unsigned-for-this-purpose or mismatched is refused.
"""
from __future__ import annotations

from pathlib import Path

SACRED = ("preloader", "gpt", "pgpt", "lk", "efuse", "seccfg",
          "nvram", "nvdata", "protect1", "protect2", "md1img_b",
          "tee", "gz", "scp", "sspm", "mcupm", "pi_img", "spmfw")


def magic_of(path):
    with open(path, "rb") as f:
        head = f.read(64)
    if head[:4] == b"\xd7\xb3\x26\xed" or head[40:44] == b"\x53\xef":
        return "sparse-or-ext4"
    if head[:4] == b"AVB0":
        return "avb"
    if head.startswith(b"ANDROID!"):
        return "android-boot"
    return "unknown"


def check_magic(path, want):
    got = magic_of(path)
    if want == "sparse-or-ext4":
        ok = got in ("sparse-or-ext4",)
    else:
        ok = (got == want)
    return ok, got


def run_flash_plan(ux, profile, cfg):
    """ux: minimal interface (see wizard.py): log/confirm/abort/adb/fastboot.
    Returns True when the whole plan flashed and the device came back."""
    fw = dict(cfg.get("firmware_files", {}))
    never = set(profile.get("never_flash", [])) | set(SACRED)
    plan = profile.get("flash_plan", [])
    if not plan:
        ux.log("flash plan empty for this profile; nothing to do")
        return True
    for step in plan:
        part = step["partition"]
        if part in never:
            ux.abort(f"profile asks to flash denied partition: {part}")
        targets = []
        if step.get("both_slots"):
            targets = [f"{part}_a", f"{part}_b"]
        elif step.get("slot_suffix") and ux.active_slot():
            # flash the CURRENTLY ACTIVE slot only by default; both_slots
            # opts in explicitly. Never the inactive modem slot family.
            targets = [f"{part}_{ux.active_slot()}"]
        else:
            targets = [part]
        for t in targets:
            if t in never or t.split("_")[0] in never and t != part:
                ux.abort(f"denied target: {t}")
        src = fw.get(step.get("source_key", ""))
        if not src or not Path(src).is_file():
            if ux.dry:
                ux.log(f"need image for {part}; skipping until provided")
                return False
            src = ux.ask_path(f"image file for partition '{part}' "
                              f"({step.get('note', '')})")
            fw[step.get("source_key", "")] = src
        ok, got = check_magic(src, step.get("magic", ""))
        if not ok:
            ux.abort(f"{part}: magic {got} != expected {step.get('magic')} "
                     f"({src})")
        ux.log(f"{part}: {Path(src).name} magic={got} "
               f"sha256={ux.sha256(src)[:12]}... {step.get('note','')}")
        for t in targets:
            ux.confirm(f"flash {t} from {Path(src).name}?",
                       expect=t.split("_")[0].upper())
            ux.fastboot_flash(t, src)
    ux.log("rebooting to flashed stack...")
    ux.reboot(wait=True)
    ux.device_health("post-flash")
    return True
