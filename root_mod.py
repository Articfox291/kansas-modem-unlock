#!/usr/bin/env python3
"""Guided root phase from device profiles (stdlib only).

Rooting needs on-device actions (manager app) the tool cannot take, so this
phase is a checklist with machine-verified gates: it proves each step before
and after, instructs the human middle, and never advances on assumption.

Kansas flow (KernelSU Next, init_boot method): backup-what-exists ->
install manager APK (user's own copy) -> patch init_boot ON DEVICE ->
flash produced image to both init_boot slots -> reboot -> su probe.
"""
from __future__ import annotations

from pathlib import Path


def run_root_phase(ux, profile, cfg):
    """Returns True when su probe passes after the phase."""
    root = profile.get("root", {})
    method = root.get("method", "unknown")
    ux.log(f"root method: {method}")
    if ux.shell_su_ok(root.get("su_probe", "su -c id"),
                       root.get("su_ok_marker", "uid=0")):
        ux.log("root ALREADY active; skipping root phase")
        return True
    fw = dict(cfg.get("firmware_files", {}))
    stock = fw.get("init_boot_stock", "")
    if not stock or not Path(stock).is_file():
        ux.log("need stock init_boot path first: config firmware_files."
               "init_boot_stock (revert material; live backup is impossible "
               "pre-root, so the factory file IS the safety net)")
        return False
    ux.log(f"stock init_boot: {Path(stock).name} "
           f"sha256={ux.sha256(stock)[:12]}... (revert material, pinned)")
    ux.confirm("bootloader unlocked + stock init_boot recorded?", expect="YES")
    pkgs = [root.get("manager_package", "")] + root.get("manager_package_alt", [])
    pkgs = [p for p in pkgs if p]
    installed = ux.installed_packages()
    if not any(p in installed for p in pkgs):
        ux.log("ON DEVICE, install your KernelSU manager APK, then open it.")
        ux.log("In the manager: patch the STOCK init_boot file (manager "
               "recommends init_boot on this device). NEVER Magisk-in-boot "
               "(bootloops here).")
        ux.confirm("manager installed and init_boot patched on-device?",
                   expect="YES")
    ux.log("ON DEVICE: pull the manager-produced image to the PC "
           "(adb pull) and set config firmware_files.init_boot_ksu to it.")
    ksu = fw.get("init_boot_ksu", "")
    if not ksu or not Path(ksu).is_file():
        ux.log("init_boot_ksu path not set yet; re-run when staged")
        return False
    ok, got = __import__("flash_mod").check_magic(ksu, "android-boot")
    if not ok:
        ux.abort(f"ksu image magic {got} != android-boot ({ksu})")
    for slot in ("init_boot_a", "init_boot_b"):
        ux.confirm(f"flash {slot} from {Path(ksu).name}?", expect="INIT_BOOT")
        ux.fastboot_flash(slot, ksu)
    ux.log("rebooting to rooted stack...")
    ux.reboot(wait=True)
    if ux.shell_su_ok(root.get("su_probe", "su -c id"),
                       root.get("su_ok_marker", "uid=0")):
        ux.log("root ACTIVE (su probe passed)")
        ux.log("now pulling live init_boot slots as new baselines "
               "(possible now that root exists)")
        ux.backup_partition("init_boot_a")
        ux.backup_partition("init_boot_b")
        return True
    ux.log("root probe FAILED after flash: likely wrong image or disabled "
           "vbmeta missing. Revert path: flash stock init_boot to both "
           "slots (unlock.py-style revert), then re-check the manager build.")
    return False
