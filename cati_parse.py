#!/usr/bin/env python3
"""Parse CATI symbol table from nevada md1_dbginfo (LZMA) region."""
import lzma
import struct
import sys

IMG = "/home/cameron/Downloads/RETUS/md1img.img"


def load_cati_blob():
    data = open(IMG, "rb").read()
    return lzma.decompress(data[0x37358A0:0x37358A0 + 1348890])


def parse(blob):
    recs = {}
    pos = 16
    n = len(blob)
    # Each record: u32 prev, u32 start, name+NUL, u32 start2, u32 next.
    # The name is found by scanning; validate start2==start.
    count = 0
    while pos + 8 < n:
        prev, start = struct.unpack_from("<II", blob, pos)
        # plausible VA range check (modem 0x90xxxxxx-0x92xxxxxx-ish)
        if not (0x90000000 <= start < 0x94000000):
            pos += 1
            continue
        epos = blob.find(b"\x00", pos + 8)
        if epos < 0 or epos - (pos + 8) > 256 or epos - (pos + 8) < 1:
            pos += 1
            continue
        try:
            name = blob[pos + 8:epos].decode("ascii")
        except UnicodeDecodeError:
            pos += 1
            continue
        if not all(32 <= ord(c) < 127 for c in name):
            pos += 1
            continue
        if epos + 9 > n:
            break
        start2, nxt = struct.unpack_from("<II", blob, epos + 1)
        if start2 != start:
            pos += 1
            continue
        if not (0x90000000 <= nxt < 0x94000000) and nxt != 0:
            pos += 1
            continue
        recs[name] = (start, nxt)
        pos = epos + 1 + 8
        count += 1
    return recs


if __name__ == "__main__":
    blob = load_cati_blob()
    recs = parse(blob)
    print(f"records: {len(recs)}")
    for want in sys.argv[1:]:
        if want in recs:
            s, e = recs[want]
            print(f"{want}: VA [{s:#x}, {e:#x}) len={e - s}")
        else:
            cands = sorted(x for x in recs if want.lower() in x.lower())[:10]
            print(f"{want}: NOT FOUND; similar: {cands}")
