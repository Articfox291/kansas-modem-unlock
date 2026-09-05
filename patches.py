#!/usr/bin/env python3
"""Kansas modem SIM-lock patch table (XT2513V, build P247.01.339R ONLY).

Every entry is (name, md1img file offset, expected old bytes, new bytes,
purpose). The tool verifies old bytes match before writing a single byte;
ANY mismatch aborts (that is the anti-brick core: offsets are build-specific).

No personal data lives here: offsets + stock bytes + patch bytes are identical
on every unit of this build. No IMEI, serial, key, certificate, or identity.
"""

# --- device fingerprint: ALL must match or the tool refuses -----------------
EXPECT_MODEL_SUBSTR = "kansas"          # ro.product.model
EXPECT_SKU = "XT2513V"                  # ro.boot.hardware.sku
EXPECT_BASEBAND_SUBSTR = "MT6835_NR17.RC.MP.V40.2.P247.01.339R"
EXPECT_SECURESTATE = "flashing_unlocked"  # fastboot getvar securestate
EXPECT_STOCK_SIZE = 75697504            # factory md1img bytes
EXPECT_STOCK_SHA256 = (
    "371671d30fa8c257985570223cc8abb32ff6f08cc93eba8b950a142585ab3272"
)
# Version literal inside the image proving right build content
# (md1img file offset 0x2B7E75C, 36 ASCII bytes + NUL).
EXPECT_VERSION_OFF = 0x2B7E75C
EXPECT_VERSION = b"MT6835_NR17.RC.MP.V40.2.P247.01.339R"

# --- patch table: 32 bytes total --------------------------------------------
# Each: (label, md1img file offset, old hex, new hex, why)
PATCHES = [
    ("force-legal",
     0x5DF4FA, "141e2412", "01d2e0db",
     "custom_check_link_sml_legal_sim_rule entry -> return LEGAL(1); "
     "covers the Network-link (cat0) evaluation path."),
    ("sl-check-force",
     0x5EFF10, "a3349110", "01d2e0db",
     "sml_sl_Check entry -> return pass(1); covers the per-category "
     "Service-Provider-family check dispatch."),
    # smu_check_sml verdict gauntlet: neutralize fail-arms so every arrival
    # falls through toward the allow arm. Each is a 4B branch -> NOP NOP.
    ("gate-e6e2", 0x198E8E2, "70ca5b0f", "08900890",
     "BNEIC s3,1 (per-cat verdict) -> fall through to allow arm."),
    ("gate-e6ba", 0x198E8BA, "90c89c08", "08900890",
     "BNEIC a0,1 early gate (loop entry) -> fall through into loop."),
    ("gate-e6e8", 0x198E8E8, "f0c87009", "08900890",
     "BNEIC a3,1 (status-byte gate) -> fall through."),
    ("gate-e6f0", 0x198E8F0, "90c87008", "08900890",
     "BNEIC a0,1 (cat-choice gate) -> fall through."),
    ("gate-e6f4", 0x198E8F4, "a0ca6c38", "08900890",
     "BEQIC s5,7 (cat-accounting gate) -> fall through."),
    ("gate-e6f8", 0x198E8F8, "608ae000", "08900890",
     "BEQC zero,s3 (verdict-zero gate) -> fall through to allow return."),
]

TOTAL_PATCH_BYTES = sum(len(bytes.fromhex(p[3])) for p in PATCHES)
assert TOTAL_PATCH_BYTES == 32, TOTAL_PATCH_BYTES
