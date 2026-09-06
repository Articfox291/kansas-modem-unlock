#!/usr/bin/env python3
"""Nevada stock-vs-patch execution differential (lab interp engine, overlay, no file writes)."""
import sys

sys.path.insert(0, "/tmp/opencode/kansas-modem-lab/sim")
import interp as I

IMG = "/home/cameron/Downloads/RETUS/md1img.img"
ROM_OFF, ROM_LEN = 0x200, 46549072  # md1rom payload (CERT layout parsed)

NEV = {
    "legal": {"va": 0x90728A68, "size": 106,
              "overlay": {0x90728A68: bytes.fromhex("01d2e0db")}},
    "sl_Check": {"va": 0x907398EE, "size": 142,
                 "overlay": {0x907398EE: bytes.fromhex("01d2e0db")}},
    "smu": {"va": 0x91A1789E, "size": 842,
            "overlay": {v: bytes.fromhex("08900890") for v in (
                0x91A17A30, 0x91A17A58, 0x91A17A5E,
                0x91A17A66, 0x91A17A6A, 0x91A17A6E)}},
}


def main():
    rom = open(IMG, "rb").read()[ROM_OFF:ROM_OFF + ROM_LEN]
    assert len(rom) == ROM_LEN, len(rom)
    I._load_image_bytes = lambda: rom  # noqa: E731

    base_regs = {"a0": I.CTX_INIT, "s0": I.CTX_INIT, "ra": I.RA_INIT,
                 "sp": I.STACK_INIT, "_ctx_image": b"\x00" * I.CTX_SIZE}
    ok = True
    for name, cfg in NEV.items():
        for mode in ("stock", "patch"):
            regs = dict(base_regs)
            if mode == "patch":
                regs["_overlay"] = cfg["overlay"]
            try:
                a0, steps, trace = I._run_fn_impl(
                    cfg["va"], cfg["size"], regs, None, None, 20000)
                print(f"{name}[{mode}]: HIT-RET a0={a0:#x} steps={steps}")
                for t in trace[-2:]:
                    print(f"    {t}")
                if mode == "patch" and name in ("legal", "sl_Check"):
                    good = (a0 == 1 and steps == 1)
                    print(f"    force-pair differential: {'PASS' if good else 'FAIL'}")
                    ok &= good
            except Exception as e:  # noqa: BLE001
                print(f"{name}[{mode}]: {type(e).__name__}: {e}")
                if mode == "patch" and name in ("legal", "sl_Check"):
                    ok = False
    print("ENTRY-FORCE RESULT:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
