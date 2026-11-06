#!/usr/bin/env python3
"""Check BTLERPAConnect patches against the installed bluetoothd.

Usage: python3 tools/check_patterns.py [/usr/sbin/bluetoothd]

Checks:
  1. every in-place pattern matches exactly once (or is already patched in
     the page cache), the __eh_frame anchor is unique and the cave is free
  2. the cave bytes equal docs/cave.s linked at 0x100aa2440
  3. every inserted call lands on the routine it is meant for
  4. no branch/call elsewhere in __text lands inside replaced bytes
Exit code 0 = all checks passed.
"""
import re
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "BTLERPAConnect" / "kern_start.cpp"
CAVE_SRC = ROOT / "docs" / "cave.s"
BIN = Path(sys.argv[1] if len(sys.argv) > 1 else "/usr/sbin/bluetoothd")
SLICE = 16384          # x86_64 slice offset in the fat bluetoothd (25G72)
VMBASE = 0x100000000
CAVE = 0x100aa2440
CAVE_OFFSET = 0xF0     # from the __eh_frame anchor
IN_PLACE = ("uheMode", "advType1", "advType2", "connectStart", "connectStart2", "connComplete",
            "disconnect", "resolve", "acceptAdd", "acceptRemove")
# call site (start of the replaced instruction) -> routine label in docs/cave.s
CALLS = {
    0x1001f5c66: "advType",
    0x1001f6693: "advType",
    0x10020537f: "connectStart",
    0x100205ea4: "connectStart2",
    0x100214c06: "connComplete",
    0x100209514: "disconnect",
    0x100227190: "resolveRecord",
    0x1002350ec: "acceptAdd",
    0x10023540c: "acceptRemove",
}
SILENT_RESTART = 0x1001f6d42


def arrays(text):
    out = {}
    for name, body in re.findall(r"static const uint8_t (\w+)\[\] \{(.*?)\};", text, re.S):
        out[name] = bytes(int(x, 16) for x in re.findall(r"0x([0-9A-Fa-f]{2})", body))
    return out


def va(off):
    return off - SLICE + VMBASE


def off(addr):
    return addr - VMBASE + SLICE


def linked_cave():
    """Returns (bytes, {label: address}) of docs/cave.s linked at CAVE."""
    with tempfile.TemporaryDirectory() as t:
        obj, out = Path(t) / "cave.o", Path(t) / "cave.bin"
        subprocess.run(["clang", "-c", "-arch", "x86_64", str(CAVE_SRC), "-o", str(obj)], check=True)
        subprocess.run(["ld", "-arch", "x86_64", "-preload", "-e", "base", "-segaddr", "__TEXT",
                        hex(CAVE), str(obj), "-o", str(out)], check=True, capture_output=True)
        sect = subprocess.run(["otool", "-l", str(out)], capture_output=True, text=True).stdout
        m = re.search(r"sectname __text.*?size (0x[0-9a-f]+)\s+offset (\d+)", sect, re.S)
        size, start = int(m.group(1), 16), int(m.group(2))
        syms = {}
        for line in subprocess.run(["nm", "-n", str(out)], capture_output=True, text=True).stdout.splitlines():
            parts = line.split()
            if len(parts) == 3 and parts[1] in "tTa":
                syms[parts[2]] = int(parts[0], 16)
        calls = []
        for line in subprocess.run(["otool", "-tV", str(out)], capture_output=True, text=True).stdout.splitlines():
            m = re.match(r"[0-9a-f]{16}\t(?:callq|jmp)\t(\S+)$", line)
            if m:
                t = m.group(1)
                calls.append(int(t, 16) if t.startswith("0x") else syms[t])
        return out.read_bytes()[start:start + size], syms, calls


def main():
    a = arrays(re.sub(r"//[^\n]*", "", SRC.read_text()))
    data = BIN.read_bytes()
    patched = bytearray(data)
    ok, changed = True, []

    for base in IN_PLACE:
        orig, repl = a[base + "Original"], a[base + "Replacement"]
        count = data.count(orig)
        # A running BTLERPAConnect patches the shared page cache, so reads of
        # the file can already return the replacement bytes.
        if count == 0 and data.count(repl) == 1:
            print(f"{base:14s} len={len(orig):3d} already patched in page cache")
            i = data.find(repl)
        else:
            print(f"{base:14s} len={len(orig):3d} occurrences={count}")
            if count != 1 or len(orig) != len(repl) or i_page(data.find(orig), len(orig)):
                ok = False
                continue
            i = data.find(orig)
            patched[i:i + len(orig)] = repl
        changed += [va(i + k) for k in range(len(orig)) if orig[k] != repl[k]]

    anchor, cave = a["ehFrameAnchor"], a["caveReplacement"]
    print(f"{'ehFrameAnchor':14s} occurrences={data.count(anchor)}")
    c = data.find(anchor) + CAVE_OFFSET
    current = data[c:c + len(cave)]
    print(f"{'cave':14s} len={len(cave):3d} at 0x{va(c):x}",
          "already patched" if current == cave else "free" if not any(current) else "OCCUPIED")
    ok = ok and data.count(anchor) == 1 and va(c) == CAVE and current in (cave, bytes(len(cave)))
    ok = ok and not i_page(c, len(cave))
    patched[c:c + len(cave)] = cave
    changed += [CAVE + k for k in range(len(cave)) if cave[k]]

    linked, syms, cave_calls = linked_cave()
    same = linked == cave
    print("cave bytes match docs/cave.s:", same)
    ok = ok and same

    for site, label in CALLS.items():
        o, routine = off(site), syms[label]
        tgt = site + 5 + int.from_bytes(patched[o + 1:o + 5], "little", signed=True)
        good = patched[o] == 0xE8 and tgt == routine
        print(f"call 0x{site:x} -> 0x{tgt:x} {label} {'ok' if good else 'WRONG (want 0x%x)' % routine}")
        ok = ok and good

    # The cave lies outside __text, so otool below does not see it: its only
    # call/jmp out into bluetoothd must be silentRestart -> SILENT_RESTART.
    outside = [t for t in cave_calls if not CAVE <= t < CAVE + len(cave)]
    print("cave calls out to:", [hex(t) for t in outside])
    ok = ok and outside == [SILENT_RESTART]

    with tempfile.NamedTemporaryFile(suffix=".bluetoothd") as tmp:
        tmp.write(patched)
        tmp.flush()
        asm = subprocess.run(["otool", "-arch", "x86_64", "-tV", tmp.name],
                             capture_output=True, text=True).stdout
    changed_set = set(changed)
    allowed = {x for x in changed if x - 1 not in changed_set} | set(CALLS.values())
    bad = []
    for line in asm.splitlines():
        m = re.match(r"([0-9a-f]{16})\t(j\w+|callq)\t0x([0-9a-f]+)$", line)
        if m:
            src, tgt = int(m.group(1), 16), int(m.group(3), 16)
            if tgt in changed_set and tgt not in allowed and src not in changed_set:
                bad.append((src, tgt))
    for s, t in bad:
        print(f"branch 0x{s:x} -> 0x{t:x} lands inside replaced bytes")
    ok = ok and not bad

    print("OK" if ok else "MISMATCH: do not use this kext with this bluetoothd")
    return 0 if ok else 1


def i_page(i, n):
    """True when [i, i+n) crosses a 4 KiB page (the kext patches one page at a time)."""
    return (i // 4096) != ((i + n - 1) // 4096)


if __name__ == "__main__":
    sys.exit(main())
