#!/usr/bin/env python3
"""Port BTLERPAConnect to the bluetoothd of one macOS build.

Usage: python3 tools/port.py <bluetoothd> <macOS build>      e.g. 25G332
       python3 tools/port.py --check <bluetoothd> <macOS build>

<bluetoothd> must be a clean copy. While the kext is active, reads of
/usr/sbin/bluetoothd return patched pages: boot with -blrpaoff first, or
take the copy from another volume.

The patch sites and the addresses the routines need are found by signature
(?? = byte that differs between builds), never by address. Every signature
must match exactly once and all checks below must pass, otherwise nothing
is written:

  - both advertising-parameter sites read the same ADV_TYPE variable
  - the routine area after __eh_frame and the state area after the last
    __DATA section are free
  - no other branch or call lands inside replaced bytes

The result is BTLERPAConnect/builds/<build>.inc, which the kext compiles in.
With --check nothing is written; the existing file is compared instead.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from portlib import Binary, Patch, PortError, assemble, branch_scan, rel32, write_build  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
BUILDS = ROOT / "BTLERPAConnect" / "builds"
CAVE_SRC = ROOT / "docs" / "cave.s"

# Shared layout of the zero padding after __eh_frame (both kexts rely on it):
#   __eh_frame + 0x060 .. + 0x180   BTLEAdvFix routine
#   __eh_frame + 0x180 .. + 0x360   BTLERPAConnect routines
CAVE_OFFSET = 0x180
CAVE_LIMIT = 0x360
STATE_SIZE = 16 * 24 + 2

# LEHardwareModel::readCurrentUHEModeCB, failure path:
#   jmp +2; xorl %eax, %eax; movb %al, mode(%rip)     xorl -> movb $0x2, %al
#   cmpq $-0x1, ...(%rip); jne ...; movl %ebx, %edi; movl $0x92, %esi
UHE_MODE = "EB 02 31 C0 88 05 ?? ?? ?? ?? 48 83 3D ?? ?? ?? ?? FF 75 ?? 89 DF BE 92 00 00 00"

# Call sites: name, signature, offset of the replaced bytes, their length,
# routine label in docs/cave.s. The replaced bytes become "call routine"
# padded with nops; the routine redoes what they did.
SITES = [
    # Arguments of HCI command index 0xa6 (LE_Set_Advertising_Parameters), two places:
    #   movzwl ...(%rip), %ecx; movzbl ADV_TYPE(%rip), %r8d; ...; movl $0xa6, %edi
    ("advertising type", "0F B7 0D ?? ?? ?? ?? 44 0F B6 05 ?? ?? ?? ??", 7, 8, "advType", 2),
    # call ...; movw %r14w, -0x40(%rbp); movw %r14w, -0x3e(%rbp); movw %r14w, -0x3c(%rbp)
    ("restart advertising before connect",
     "E8 ?? ?? ?? ?? 66 44 89 75 C0 66 44 89 75 C2 66 44 89 75 C4", 5, 5, "connectStart", 1),
    # same in the second LE connect path, with %r13w
    ("restart advertising before connect 2",
     "E8 ?? ?? ?? ?? 66 44 89 6D C0 66 44 89 6D C2 66 44 89 6D C4", 5, 5, "connectStart2", 1),
    # GAP_ConnectionCompleteCallback: movq %r12, %r15; cmpl $0x0, -0xa8(%rbp); je ...; cmpl $..., -0xa8(%rbp)
    ("count LE links up", "4D 89 E7 83 BD 58 FF FF FF 00 74 ?? 81 BD 58 FF FF", 3, 7, "connComplete", 1),
    # LE disconnection complete: pushes; movl %edx, %r14d; testq %rsi, %rsi; je ...; movq %r8, %r15
    ("count LE links down", "41 55 41 54 53 50 41 89 D6 48 85 F6 74 ?? 4D 89 C7", 6, 6, "disconnect", 1),
    # LE_ResolveAddress: store the identity address, mark as resolved
    ("record identity -> RPA",
     "42 0F B7 54 20 FD 42 8A 44 20 FF 41 88 46 06 66 41 89 56 04 41 89 0E C6 03 01", 11, 15, "resolveRecord", 1),
    # accept list add: movq %rax, %r14; movzbl (%r15), %edx; leaq 0x1(%r15), %rcx; leaq ...(%rip), %rsi
    ("accept list add RPA", "49 89 C6 41 0F B6 17 49 8D 4F 01 48 8D 35 ?? ?? ?? ??", 3, 8, "acceptAdd", 1),
    # accept list remove: same with movq %rax, %rbx
    ("accept list remove RPA", "48 89 C3 41 0F B6 17 49 8D 4F 01 48 8D 35 ?? ?? ?? ??", 3, 8, "acceptRemove", 1),
]

ADV_PARAMS_COMMAND = bytes.fromhex("BF A6 00 00 00")     # movl $0xa6, %edi

# bluetoothd's own silent restart of advertising (function entry):
#   xorl %eax,%eax; cmpb $0x2, ...; jne; testb $0x1, ...; jne; testb %dil,%dil; je;
#   cmpb $0x3, ...; je; retq; pushq %rbp; movq %rsp,%rbp; call ...
SILENT_RESTART = ("31 C0 80 3D ?? ?? ?? ?? 02 75 17 F6 05 ?? ?? ?? ?? 01 75 0E 40 84 FF 74 0A "
                  "80 3D ?? ?? ?? ?? 03 74 01 C3 55 48 89 E5 E8 ?? ?? ?? ??")

# A place that tests and sets lConnectionInProgress:
#   cmpb $0x0, flag(%rip); three loads from the stack; jne +7; movb $0x1, flag(%rip)
CONN_IN_PROGRESS = "80 3D ?? ?? ?? ?? 00 8B 55 ?? 8B 75 ?? 8B 7D ?? 75 07 C6 05 ?? ?? ?? ?? 01"


def port(binary):
    notes = []

    uhe = binary.find_one("no false UHE restart", UHE_MODE)
    original = binary.bytes_at(uhe, len(UHE_MODE.split()))
    patches = [Patch("no false UHE restart", uhe, original, original[:2] + b"\xB0\x02" + original[4:])]

    found = []
    for name, signature, at, length, label, count in SITES:
        hits = binary.find(signature)
        if label == "advType":
            hits = [h for h in hits if ADV_PARAMS_COMMAND in binary.bytes_at(h, 0x60)]
        if len(hits) != count:
            raise PortError(f"{name}: signature matches {len(hits)} times, expected {count}")
        size = len(signature.split())
        for n, hit in enumerate(hits):
            found.append((name if count == 1 else f"{name} {n + 1}", hit, size, at, length, label))

    adv = [binary.rip_target(hit + 11, hit + 15) for name, hit, *_ in found if name.startswith("advertising type")]
    if len(set(adv)) != 1:
        raise PortError("the two advertising type sites read different variables")
    adv_type = adv[0]

    restart = binary.va(binary.find_one("silent restart", SILENT_RESTART))

    cip = binary.find_one("lConnectionInProgress", CONN_IN_PROGRESS)
    conn = binary.rip_target(cip + 2, cip + 7)
    if conn != binary.rip_target(cip + 20, cip + 25):
        raise PortError("lConnectionInProgress: test and set use different variables")

    eh_addr, eh_size = binary.sections[("__TEXT", "__eh_frame")]
    eh = binary.off(eh_addr)
    cave = eh + CAVE_OFFSET
    if eh + CAVE_LIMIT > binary.segments["__TEXT"][3]:
        raise PortError("not enough padding after __eh_frame")
    if any(binary.bytes_at(cave, CAVE_LIMIT - CAVE_OFFSET)):
        raise PortError("routine area after __eh_frame is not free")

    vmaddr, vmsize, _fileoff, filesize = binary.segments["__DATA"]
    last = max(addr + size for (seg, _s), (addr, size) in binary.sections.items() if seg == "__DATA")
    table = (last + 15) // 16 * 16 + 16
    if table < vmaddr + filesize or table + STATE_SIZE > vmaddr + vmsize:
        raise PortError("no zero-filled room after the last __DATA section")

    symbols = {"CAVE": binary.va(cave), "ADV_TYPE": adv_type, "CONN_IN_PROGRESS": conn,
               "SILENT_RESTART": restart, "TABLE": table}
    for key, value in symbols.items():
        notes.append(f"{key} = 0x{value:x}")
    code, labels = assemble(CAVE_SRC, symbols, "base", link_at=binary.va(cave))
    if len(code) > CAVE_LIMIT - CAVE_OFFSET:
        raise PortError(f"routines are {len(code)} bytes, limit is {CAVE_LIMIT - CAVE_OFFSET}")
    patches.append(Patch("RPA routines", cave, bytes(len(code)), code))

    for name, hit, size, at, length, label in found:
        original = binary.bytes_at(hit, size)
        call = b"\xE8" + rel32(hit + at + 5, cave + labels[label]) + b"\x90" * (length - 5)
        patches.append(Patch(name, hit, original, original[:at] + call + original[at + length:]))

    bad = branch_scan(binary, patches, [])
    for src, tgt in bad:
        print(f"branch 0x{src:x} -> 0x{tgt:x} lands inside replaced bytes")
    if bad:
        raise PortError("other code jumps into the replaced instructions")
    return patches, notes


def main():
    args = sys.argv[1:]
    check = "--check" in args
    args = [a for a in args if a != "--check"]
    if len(args) != 2:
        sys.exit(__doc__)
    path, os_build = args
    try:
        binary = Binary(path)
        patches, notes = port(binary)
        for n in notes:
            print(n)
        for p in patches:
            print(f"{p.name:38s} 0x{binary.va(p.offset):x}  {len(p.original):3d} bytes")
        target = BUILDS / f"{os_build}.inc"
        before = target.read_text() if target.exists() else None
        if check and before is None:
            raise PortError(f"{target.name} does not exist")
        names = write_build(BUILDS, os_build, binary, patches, notes)
        if check:
            same = target.read_text() == before
            target.write_text(before)
            print("OK: builds/%s.inc is up to date" % os_build if same else "MISMATCH with builds/%s.inc" % os_build)
            sys.exit(0 if same else 1)
        print(f"wrote builds/{os_build}.inc; builds compiled in: {', '.join(names)}")
    except PortError as e:
        sys.exit(f"NOT PORTED: {e}")


if __name__ == "__main__":
    main()
