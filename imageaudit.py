#!/usr/bin/env python3
"""Full-parse firmware audit for modem images (stdlib only).

Two gates, both must pass before anything is built or flashed:

1. audit_image(path) — structural walk + per-triple hash consistency:
   every CERT2-stored header/image digest must equal the recomputed digest
   over the actual bytes. A fully matching image is either an intact factory
   image or a consistently re-signed one (distinguished by compare step).
   RSA signature checks run best-effort where the vendored verifier supports
   them; hash consistency is the load-bearing gate either way.

2. compare_built(stock, built, table) — byte-for-byte discipline: EVERY
   differing byte between the two images must fall inside one declared
   (offset, old, new) table entry with exact old->new content. Anything else
   fails closed. This is what "only continue if byte-for-byte the same
   except the declared set" means in this repo.
"""
from __future__ import annotations

import hashlib
from pathlib import Path

import sign_mtk_cert as SMC
import verify_mtk_image as VM


def _triples(data):
    entries = VM.parse_part_entries(data)
    return entries, VM.find_targets(entries, None)


def audit_image(path):
    """Full parse + integrity. Returns (ok: bool, report: dict)."""
    data = Path(path).read_bytes()
    rep = {"path": str(path), "size": len(data), "entries": [],
           "layout_ok": False, "triples": [], "integrity": False,
           "errors": []}
    try:
        parts = SMC.parse_part_headers(data)
    except Exception as e:  # noqa: BLE001
        rep["errors"].append(f"entry walk failed: {e}")
        return False, rep
    if not parts:
        rep["errors"].append("no parseable entries (not an md1img?)")
        return False, rep
    # layout: contiguous, ordered, ends exactly at EOF
    ok = True
    for i, (idx, hoff, doff, noff, hdr) in enumerate(parts):
        rep["entries"].append({"idx": idx, "name": hdr.name,
                               "hdr_off": hoff, "data_off": doff,
                               "next_off": noff, "dsize": hdr.dsize})
        if i and hoff != parts[i - 1][3]:
            ok = False
            rep["errors"].append(f"gap/overlap before entry {idx} ({hdr.name})")
    if parts[-1][3] != len(data):
        ok = False
        rep["errors"].append("trailing bytes past last entry")
    rep["layout_ok"] = ok
    # per-triple hash consistency
    try:
        _entries, triples = _triples(data)
    except Exception as e:  # noqa: BLE001
        rep["errors"].append(f"triple grouping failed: {e}")
        return False, rep
    if not triples:
        rep["errors"].append("no (target, CERT1, CERT2) triples found")
        return False, rep
    all_ok = bool(ok)
    for t, c1, c2 in triples:
        tr = {"target": t.hdr.name or f"#{t.index}", "hash_ok": None,
              "rsa_ok": None, "cert2_dsize": c2.hdr.dsize, "notes": []}
        try:
            n2 = VM.parse_der_nodes(data[c2.data_off:c2.data_off + c2.hdr.dsize])
            stored_hh = VM.find_bit_string_by_oid(n2, VM.OID_IMG_HDR_HASH)
            stored_ih = VM.find_bit_string_by_oid(n2, VM.OID_IMG_HASH)
            cert1 = VM.parse_cert(
                data[c1.data_off:c1.data_off + c1.hdr.dsize])
            calc_hh = VM.hash_data(data[t.off:t.off + t.hdr.hdr_sz],
                                   cert1.sec_level)
            calc_ih = VM.hash_data(VM.padded_image_data(data, t),
                                   cert1.sec_level)
            tr["hash_ok"] = (stored_hh == calc_hh and stored_ih == calc_ih)
            if not tr["hash_ok"]:
                tr["notes"].append("stored-vs-recomputed digest mismatch")
            try:
                cert2 = VM.parse_cert(
                    data[c2.data_off:c2.data_off + c2.hdr.dsize])
                tr["rsa_ok"] = bool(VM.rsa_pss_verify(
                    cert2.tbs.full, cert2.signature,
                    cert2.public_key, cert2.hash_name))
            except Exception as e:  # noqa: BLE001
                tr["notes"].append(f"rsa check unavailable: {e}")
        except Exception as e:  # noqa: BLE001
            tr["notes"].append(f"triple evaluation failed: {e}")
        rep["triples"].append(tr)
        if tr["hash_ok"] is not True:
            all_ok = False
    rep["integrity"] = all_ok
    if not all_ok:
        rep["errors"].append("integrity gate failed (see triple notes)")
    return all_ok, rep


def diff_runs(a: bytes, b: bytes):
    """Yield (start, end_exclusive) runs where a != b. Sizes must match."""
    if len(a) != len(b):
        raise ValueError(f"size changed {len(a)} -> {len(b)} (refusing)")
    runs, s = [], None
    for i, (x, y) in enumerate(zip(a, b)):
        if x != y and s is None:
            s = i
        elif x == y and s is not None:
            runs.append((s, i))
            s = None
    if s is not None:
        runs.append((s, len(a)))
    return runs


def compare_built(stock: bytes, built: bytes, table):
    """Every differing byte must sit inside exactly one declared entry with
    exact old->new content. table: iterable of (label, offset:int,
    old:bytes, new:bytes). Returns (ok, report)."""
    rep = {"runs": [], "unexplained": [], "ok": False}
    try:
        runs = diff_runs(stock, built)
    except ValueError as e:
        rep["unexplained"].append(str(e))
        return False, rep
    decl = [(lab, int(off), bytes(old), bytes(new)) for lab, off, old, new
            in table]
    ok = True
    covered_idx = set()
    seen_labels = set()
    for s, e in runs:
        rep["runs"].append({"start": hex(s), "end": hex(e), "size": e - s})
    for lab, off, old, new in decl:
        for i in range(len(old)):
            if (off + i) in covered_idx:
                ok = False
                rep["unexplained"].append(
                    f"index {off + i:#x} covered twice (table overlap: {lab})")
            covered_idx.add(off + i)
    diffset = set()
    for s, e in runs:
        diffset.update(range(s, e))
    if diffset != covered_idx:
        ok = False
        only_diff = sorted(diffset - covered_idx)[:8]
        only_decl = sorted(covered_idx - diffset)[:8]
        if only_diff:
            rep["unexplained"].append(
                "diff bytes outside table: " +
                ", ".join(hex(i) for i in only_diff))
        if only_decl:
            rep["unexplained"].append(
                "declared bytes with no diff (wrong base image?): " +
                ", ".join(hex(i) for i in only_decl))
    if ok:
        for s, e in runs:
            for i in range(s, e):
                cand = [(lab, off, old, new) for lab, off, old, new in decl
                        if off <= i < off + len(old)]
                lab, off, old, new = cand[0]
                if stock[i] != old[i - off] or built[i] != new[i - off]:
                    ok = False
                    rep["unexplained"].append(
                        f"byte {i:#x} != declared {lab} old->new")
                    break
            if not ok:
                break
    rep["ok"] = ok and bool(runs)
    if not runs:
        rep["unexplained"].append("zero differences (nothing to do?)")
        rep["ok"] = False
    return rep["ok"], rep


def render(rep):
    lines = [f"entries={len(rep['entries'])} layout_ok={rep['layout_ok']} "
             f"integrity={rep['integrity']}"]
    for t in rep["triples"]:
        lines.append(f"  {t['target']}: hash_ok={t['hash_ok']} "
                     f"rsa_ok={t['rsa_ok']} cert2_dsize={t['cert2_dsize']} "
                     f"{'; '.join(t['notes'])}")
    lines += [f"ERROR: {e}" for e in rep["errors"]]
    return "\n".join(lines)
