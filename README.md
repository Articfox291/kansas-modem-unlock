# kansas-modem-unlock

Plug-in device framework (flash → root → carrier unlock) with interactive
guidance. Ships with **one verified device** (below); other devices plug in
as data files only after hardware-proven verification (see
`devices/README.md`). Nothing here works on a device it wasn't proven on —
refusal is a feature.

## The 5-minute path

```bash
cp config.json mylab.json   # point firmware_files at YOUR images
python wizard.py --dry-run --work work-wizard     # walk everything, touch nothing
python wizard.py --work work-wizard               # detect -> flash -> root -> unlock
python wizard.py --work work-wizard --phases verify  # health re-check anytime
```

State resumes from `work-wizard/state.json`; `--phases unlock` (or any
subset) runs just that slice. Every destructive step needs typed
confirmation; `--dry-run` performs the whole ceremony against logs only.

## Architecture

- `wizard.py` — interactive runner + device/session layer (Ux),
  state/resume, profile detection and gating. No patch bytes inside.
- `devices/<id>.json` — the entire device definition: fingerprint gates,
  never-flash list, flash plan (user-supplied images + magic checks),
  root recipe, modem patch table, verify acceptance values. Schema v1.
- `flash_mod.py` — denylist (profile + global sacred list), magic checks,
  sha pinning, per-partition typed confirms, post-flash health check.
- `root_mod.py` — guided root with machine-verified gates around a human
  middle (manager app + on-device patch); re-baselines once su passes.
- `unlock.py` + `patches.py` — the proven modem flow, reused by the wizard
  behind a drift guard (profile table must equal patches.py or refuse).
- `sign_mtk_cert.py`, `parse_mtk_certs.py` — vendored re-sign helpers.
- `config.json` — local paths + strictness (never committed with contents).

## Supported devices

| Profile | Status | Notes |
|---|---|---|
| `kansas` — Moto G 5G (2025) XT2513V, MT6835 P247.01.339R | verified-live | SIM NETWORK_LOCKED→LOADED proven, remain 5, EE 0, revert tested |

Adding one: `devices/README.md` (schema + 6-item hardware-proof checklist).
Untested drafts run audit/status only — flashing refuses.

---

## Standalone modem tool (same guarantees, no wizard)

SIM-lock evaluation patch tool for **one exact device + modem build**:

- Phone: Motorola Moto G 5G (2025) **XT2513V** (`kansas`, Tracfone/Visible)
- Modem: MT6835, baseband `MT6835_NR17.RC.MP.V40.2.P247.01.339R`
- Stock md1img: 75,697,504 bytes, sha256 `371671d3…ab3272`

Provenance: developed against a live lab unit of exactly this build
(SIM state `NETWORK_LOCKED` → `LOADED` for a foreign SIM, remain counter 5/5,
zero modem exceptions, fully reversible). No personal data in this repo:
patch offsets/bytes are identical on every unit of the build; no IMEI,
serial, key, certificate, QR, ICCID, IMSI, or matching ID anywhere
(verified by audit before release).

## What this is NOT (read before anything)

- **Not universal.** It refuses any model, SKU, baseband, image size/hash, or
  bootloader state that isn't the fingerprinted build above. Offsets are
  build-specific; applying them elsewhere is how modems get bricked, so the
  tool says no instead. "Any phone" support does not exist here on purpose.
- **Not a bootloader unlock or root tool.** It *requires* both and checks:
  `securestate: flashing_unlocked` in fastboot + `su → uid 0` over adb.
  Those are per-device processes with their own key ceremonies — out of scope.
- **Never touches identity.** No IMEI read or write, no NCK entry or trials,
  no attempt-counter interaction, no protect/nvram/nvdata writes, never slot B,
  never preloader/lk/gpt/efuse. The patch changes the lock *evaluation*
  (verdict logic), never credentials.
- **Not persistent across modem OTAs.** A carrier/modem update rewriting slot A
  restores stock lock silently. Re-check SIM state after any update.

## What it does (32 bytes)

Eight same-footprint patches to the md1img modem image, then CERT2 re-sign
(accepted by the unlocked bootloader), flashed to **slot A only**:

| # | Site | Change | Effect |
|---|------|--------|--------|
| 1 | SML legal-rule entry | `LI a0,1; JRC ra` | Network-link (cat0) evaluation returns LEGAL |
| 2 | SP check entry | `LI a0,1; JRC ra` | Per-category SP-family check returns pass |
| 3–8 | `smu_check_sml` verdict gauntlet (6×) | branch → `NOP; NOP` | Every arrival falls through toward the allow arm |

Total delta vs stock: 32 bytes, image size unchanged. Old bytes are verified
before each write; any mismatch aborts (wrong build = instant refuse).

## Prerequisites

1. The exact device + build above (the tool verifies; don't argue with it).
2. Bootloader unlocked + root (see above).
3. Your own factory `md1img` for this build (integrity-checked by sha256).
4. `adb` + `fastboot` (platform-tools) and `python` on PATH.
5. One device attached, steady cable, charged battery, ~30 minutes.
6. A full backup first (the tool enforces: no backup, no flash).

## Usage

```bash
python unlock.py status                                  # read-only audit
python unlock.py backup --out backups/                  # pull live slot A (200MB)
python unlock.py build --stock FACTORY_MD1IMG --out work/patched.img
python unlock.py flash --image work/patched.signed.img --backup backups/
python unlock.py verify                                 # baseband/SIM/remain/EE
python unlock.py revert --backup backups/               # back to stock backup
python unlock.py full --stock FACTORY_MD1IMG --work work/  # backup+build, then flash explicitly
```

`flash` and `revert` require typing YES. `verify` after every flash is not
optional in spirit: baseband alive, SIM state as expected, remain counter
unchanged at 5, zero modem exceptions in dmesg.

## If something looks wrong

- Flash rejected / boot odd / unexpected SIM state: `unlock.py revert`,
  then `unlock.py verify`. Revert path is why backups are mandatory.
- Fastboot always reachable via Vol-Down+Power (cable-insert trick if looping).
- Never `fastboot -w`, never erase partitions, never flash slot B.

## Files

- `unlock.py` — the tool (stdlib only).
- `patches.py` — fingerprint + 32-byte patch table.
- `sign_mtk_cert.py` — CERT2 re-sign helper (vendored, unmodified).
- `LICENSE` — MIT.

## Legal / safety notes (short)

Unlocking a phone you own for interoperability (other carriers, private/test
networks) is lawful in many places (e.g. US Unlocking Consumer Choice Act),
but rules differ by country and carrier contract — your responsibility to
check yours. This tool changes lock evaluation only; it does not alter device
identity, bypass stolen-device blacklists, or touch network authentication.
Radio operation stays subject to your national regulator (power, bands,
equipment authorization); a lab belongs on cables/attenuators or licensed
spectrum, not on hope. No warranty; you flash at your own risk.
