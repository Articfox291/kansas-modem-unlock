# Adding a device profile

A profile is a claim that **this exact build** was unlocked on real hardware
with receipts (before/after SIM state, remain counter intact, zero modem
exceptions, revert tested). Do not add untested offsets — that is how other
people's modems get bricked.

## Schema (`devices/<id>.json`, `"schema": 1`)

- `id`, `label`, `status` (`verified-live` | `untested-draft` — drafts refuse
  to flash), `models[]`, `skus[]`, `baseband_substr`, `securestate`,
  `stock_md1img {size, sha256}` — all become preflight gates.
- `never_flash[]` — hard deny list, no override path. Must at minimum cover
  preloader/gpt/efuse/nvram-family and the non-target modem slot.
- `flash_plan[]` — ordered `{partition, slot_suffix, source_key, magic,
  both_slots?, note}`. `source_key` resolves via `config.json
  firmware_files` (user-supplied paths; nothing bundled). `magic` is one of
  `sparse-or-ext4 | avb | android-boot` (checked from magic bytes; mismatch
  refuses). Expected hashes are recorded on first use and pinned after —
  provenance of user firmware is the owner's responsibility, stated in the log.
- `root {method, manager_package, stock_partition, su_probe, su_ok_marker,
  notes[]}` — guided flow with before/after checks, never silent.
- `modem_unlock {slot, patches[]}` — `{label, offset ("0x…" file offset),
  old, new, why}`. Every old-bytes check must pass or the build aborts.
- `verify {remain_must_stay, ee_must_stay, sim_state_unlocked,
  sim_state_locked}` — post-flash acceptance values.

## Verification checklist (all required for `verified-live`)

1. `unlock.py status` fingerprint green on the target unit.
2. Full backup pulled and manifest-logged (revert tested at least once).
3. `build` reproduces the exact documented byte delta, re-signs, parses.
4. Flash → reboot → SIM state moves locked→LOADED (or documented equivalent),
   remain counter untouched, zero modem exceptions.
5. `revert` returns the unit to stock behavior (document the round trip).
6. No personal data in the profile: no IMEI/serial/keys/QRs/EIDs/IMSIs.
   Run the repo's leak-grep pattern before committing.

## Loader rules (wizard.py)

- Unknown `schema` → refuse. `status != verified-live` → refuse flashing
  (audit/status commands still work).
- Auto-detect from `ro.product.model` + baseband substring; manual override
  requires typing the profile id plus an explicit untested-device warning
  unless the profile is verified-live for the detected fingerprint.
