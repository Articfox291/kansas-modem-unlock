# Building the root image (KernelSU Next, init_boot method)

End-to-end, from zero to `su` prompt. Items marked **[lab-proven]** were
done live on the kansas reference unit (v3.3.0, both slots, verified su);
**[generic]** items are standard practice — follow them exactly as written.

> Why KernelSU, why init_boot: **[lab-proven]** Magisk in boot *or*
> init_boot bootloops this device on every version tried (v27, v30.7) — do
> not substitute. KernelSU Next in GKI mode, patching **init_boot** (the
> manager itself recommends init_boot here), boots clean with working root.

## 0. Prerequisites

- Bootloader already unlocked (`fastboot getvar securestate` →
  `flashing_unlocked`). Rooting starts after unlocking, never before.
- Disabled-verification vbmeta in both slots. Stock vbmeta **will** reject a
  modified init_boot at boot (validation failure, no root, possible boot
  rejection). If you used the wizard flash phase with the disabled set,
  you're covered — otherwise fix vbmeta first.
- `adb`/`fastboot` on PATH, one device, steady cable, charged battery.
- 30–60 unhurried minutes. Rushing flashes is how mistakes happen.

## 1. Collect materials (PC side)

1. **Stock init_boot for YOUR build.** Source: your factory firmware package
   (RSA/service firmware for your exact model+build). **[lab-proven]**
   reference shape: 8,388,608 bytes. Record its sha256 now — this file is
   your revert material (live backup is impossible before root exists):
   `sha256sum init_boot_stock.img` → save the string somewhere safe.
2. **KernelSU Next manager APK.** Source: the official project releases
   (rifsxd/KernelSU-Next on GitHub). **[lab-proven]** pin: **v3.3.0**
   (`versionCode 33214`, package `com.rifsxd.ksunext`). Newer releases may
   work; they are untested by this guide — prefer the pin unless you have a
   reason. Verify the download against the release page checksums/signature
   before installing.
3. Put paths into `config.json`: `firmware_files.init_boot_stock` now;
   `init_boot_ksu` after step 3.

## 2. Install the manager (no root needed for this)

```bash
adb install KernelSU-Next.apk
adb shell "pm list packages | grep rifsxd"   # expect com.rifsxd.ksunext
```

## 3. Patch on-device (the actual build — taps, in order)

1. Open the KernelSU Next app → **Install** → **Select and Patch a File**.
2. Pick your **stock** `init_boot.img` (transfer it to the phone first,
   e.g. `adb push init_boot_stock.img /sdcard/Download/`).
3. Mode: **GKI** (the manager recommends init_boot for this device; accept).
4. Wait for success. Output lands in Download named like
   `kernelsu_next_patched_20260905_020341.img` (**[lab-proven]** shape:
   8,388,608 bytes — same size as stock).
5. If the manager offers boot vs init_boot: choose **init_boot**. If it
   errors, stop and re-read step 0 (vbmeta/unlock state) — do not improvise.

## 4. Pull + verify (PC side — nothing flashes yet)

```bash
adb pull /sdcard/Download/kernelsu_next_patched_*.img ./init_boot_ksu.img
ls -l init_boot_ksu.img init_boot_stock.img   # sizes must match (8,388,608 here)
sha256sum init_boot_ksu.img                   # record it; must differ from stock
```

The wizard checks `ANDROID!` magic on this file and refuses anything else.
Set `config.json → firmware_files.init_boot_ksu` to its path.

## 5. Flash via the wizard (both slots, verified after)

```bash
python wizard.py --work work-wizard --phases root
```

What it does: confirms unlock + stock recorded → confirms per slot →
`fastboot flash init_boot_a` + `init_boot_b` → reboot → probes
`su -c id` for `uid=0` → on success pulls both live slots as new baselines.
**[lab-proven]** both slots is load-bearing (OTA/slot-rotation safety).

## 6. Confirm root

```bash
adb shell "su -c id"            # expect uid=0(...)
```

Open the manager: status should read Working. Grant root to apps
individually as needed. Root shell without app prompts:
Magisk-path `/debug_ramdisk/su` also works under KSU.

## 7. Troubleshooting

| Symptom | Cause (usual) | Fix |
|---|---|---|
| Bootloop after flash | Magisk-patched image, or wrong-slot/wrong-build file | Fastboot: flash **stock** init_boot to **both** slots, reboot. Never `-w`. |
| Boots, but no su | vbmeta re-enabled by a later flash, or manager built for boot not init_boot | Re-check vbmeta state; rebuild per §3 choosing init_boot. |
| Manager won't patch | Locked bootloader or stock file unreadable | Fix §0 first; re-push a clean stock file. |
| Lost root after update | OTAs replace init_boot | Re-run §3–§5 with the new build's stock file. |
| `su` works, modules don't mount | Known GSI limitation (no systemless mounts here) | Expected on this stack; root shell itself is unaffected. |

Revert (exact, any time):

```bash
fastboot flash init_boot_a <your-recorded-stock.img>
fastboot flash init_boot_b <your-recorded-stock.img>
fastboot reboot
```

## 8. What this does NOT do

- No bootloader unlocking (prerequisite, separate ceremony + owner key).
- No vbmeta decisions (must already be disabled; §0).
- No Magisk path (proven dead end on this hardware — documented, not supported).
- No personal data anywhere in the process (no IMEI/NCK/identity touched).
