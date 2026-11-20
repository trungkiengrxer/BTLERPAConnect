"""Shared helpers for porting bluetoothd patches to a macOS build.

The same file is used by BTLEAdvFix and BTLERPAConnect. Nothing here knows
about a specific patch: it loads the x86_64 slice of bluetoothd, finds masked
signatures, assembles the routines, checks the result and writes the
per-build table that the kext compiles in.
"""
import re
import struct
import subprocess
import tempfile
from pathlib import Path

PAGE = 0x1000
CPU_X86_64 = 0x01000007
LC_SEGMENT_64 = 0x19
LC_UUID = 0x1B


class PortError(Exception):
    """The binary does not look like a build the patches apply to."""


class Binary:
    """x86_64 slice of a (fat or thin) Mach-O bluetoothd."""

    def __init__(self, path):
        raw = Path(path).read_bytes()
        self.slice_offset = 0
        if raw[:4] == b"\xca\xfe\xba\xbe":
            count = struct.unpack(">I", raw[4:8])[0]
            for i in range(count):
                cpu, _sub, off, size, _align = struct.unpack(">IIIII", raw[8 + 20 * i:28 + 20 * i])
                if cpu == CPU_X86_64:
                    self.slice_offset, raw = off, raw[off:off + size]
                    break
            else:
                raise PortError("no x86_64 slice")
        if raw[:4] != b"\xcf\xfa\xed\xfe" or struct.unpack("<I", raw[4:8])[0] != CPU_X86_64:
            raise PortError("not an x86_64 Mach-O")
        if self.slice_offset % PAGE:
            raise PortError("x86_64 slice is not page aligned")
        self.data = raw
        self.segments, self.sections = {}, {}
        self.uuid = self.uuid_offset = None
        ncmds = struct.unpack("<I", raw[16:20])[0]
        pos = 32
        for _ in range(ncmds):
            cmd, size = struct.unpack("<II", raw[pos:pos + 8])
            if cmd == LC_SEGMENT_64:
                name = raw[pos + 8:pos + 24].rstrip(b"\0").decode()
                vmaddr, vmsize, fileoff, filesize = struct.unpack("<QQQQ", raw[pos + 24:pos + 56])
                nsects = struct.unpack("<I", raw[pos + 64:pos + 68])[0]
                self.segments[name] = (vmaddr, vmsize, fileoff, filesize)
                for s in range(nsects):
                    p = pos + 72 + 80 * s
                    sect = raw[p:p + 16].rstrip(b"\0").decode()
                    addr, ssize = struct.unpack("<QQ", raw[p + 32:p + 48])
                    self.sections[(name, sect)] = (addr, ssize)
            elif cmd == LC_UUID:
                self.uuid, self.uuid_offset = raw[pos + 8:pos + 24], pos + 8
            pos += size
        if self.uuid is None or self.uuid_offset + 16 > PAGE:
            raise PortError("LC_UUID missing or outside the first page")
        vmaddr, _vmsize, fileoff, _filesize = self.segments["__TEXT"]
        if fileoff != 0:
            raise PortError("__TEXT does not start at file offset 0")
        self.vmbase = vmaddr

    def va(self, off):
        return self.vmbase + off

    def off(self, addr):
        return addr - self.vmbase

    def text_range(self):
        addr, size = self.sections[("__TEXT", "__text")]
        return self.off(addr), self.off(addr) + size

    def find(self, signature):
        """Offsets in __text where `signature` matches. `signature` is hex
        bytes separated by spaces, with ?? for a byte that may differ."""
        parts = signature.split()
        rx = re.compile(b"".join(b"." if p == "??" else re.escape(bytes([int(p, 16)])) for p in parts), re.S)
        start, end = self.text_range()
        out, pos = [], start
        while True:
            m = rx.search(self.data, pos, end)
            if not m:
                return out
            out.append(m.start())
            pos = m.start() + 1

    def find_one(self, name, signature):
        hits = self.find(signature)
        if len(hits) != 1:
            raise PortError(f"{name}: signature matches {len(hits)} times, expected 1")
        return hits[0]

    def bytes_at(self, off, size):
        return self.data[off:off + size]

    def i32(self, off):
        return struct.unpack("<i", self.data[off:off + 4])[0]

    def rip_target(self, disp_off, insn_end_off):
        """Address referenced by a rip-relative disp32 stored at disp_off."""
        return self.va(insn_end_off + self.i32(disp_off))


def rel32(from_end_off, to_off):
    return struct.pack("<i", to_off - from_end_off)


class Patch:
    def __init__(self, name, offset, original, replacement):
        if len(original) != len(replacement):
            raise PortError(f"{name}: size mismatch")
        if offset // PAGE != (offset + len(original) - 1) // PAGE:
            raise PortError(f"{name}: crosses a 4 KiB page (the kext patches one page at a time)")
        self.name, self.offset = name, offset
        self.original, self.replacement = bytes(original), bytes(replacement)


def text_section(raw):
    """(bytes, address, relocation count) of the __text section of a Mach-O."""
    ncmds = struct.unpack("<I", raw[16:20])[0]
    pos = 32
    for _ in range(ncmds):
        cmd, size = struct.unpack("<II", raw[pos:pos + 8])
        if cmd == LC_SEGMENT_64:
            nsects = struct.unpack("<I", raw[pos + 64:pos + 68])[0]
            for s in range(nsects):
                p = pos + 72 + 80 * s
                if raw[p:p + 16].rstrip(b"\0") == b"__text":
                    addr, ssize = struct.unpack("<QQ", raw[p + 32:p + 48])
                    offset, _align, _reloff, nreloc = struct.unpack("<IIII", raw[p + 48:p + 64])
                    return raw[offset:offset + ssize], addr, nreloc
        pos += size
    raise PortError("no __text section")


def assemble(source, symbols, entry, link_at=None):
    """Assemble a routine file with `symbols` predefined.
    Returns (bytes, {label: offset from `entry`}).

    Without `link_at` the code must need no relocation. With `link_at` it is
    linked at that address, so it may reach absolute addresses in bluetoothd
    through rip-relative operands."""
    with tempfile.TemporaryDirectory() as tmp:
        obj, out = Path(tmp) / "cave.o", Path(tmp) / "cave.bin"
        cmd = ["clang", "-c", "-arch", "x86_64", str(source), "-o", str(obj)]
        for key, value in symbols.items():
            cmd.append(f"-Wa,-defsym,{key}={value}")
        subprocess.run(cmd, check=True)
        if link_at is None:
            code, base, nreloc = text_section(obj.read_bytes())
            if nreloc:
                raise PortError(f"{source}: routine has relocations, it must be position independent")
            final = obj
        else:
            subprocess.run(["ld", "-arch", "x86_64", "-preload", "-e", entry, "-segalign", "0x8",
                            "-segaddr", "__TEXT", hex(link_at), str(obj), "-o", str(out)],
                           check=True, capture_output=True)
            code, base, _nreloc = text_section(out.read_bytes())
            if base != link_at:
                raise PortError(f"{source}: linked at 0x{base:x}, wanted 0x{link_at:x}")
            final = out
        labels = {}
        for line in subprocess.run(["nm", "-n", str(final)], capture_output=True, text=True, check=True).stdout.splitlines():
            parts = line.split()
            if len(parts) == 3 and parts[1] in "tT":
                labels[parts[2]] = int(parts[0], 16)
        if entry not in labels:
            raise PortError(f"{source}: label {entry} not found")
        origin = labels[entry]
        if origin != base:
            raise PortError(f"{source}: {entry} is not the first instruction")
        return code, {name: value - origin for name, value in labels.items()}


def apply(binary, patches):
    out = bytearray(binary.data)
    seen = set()
    for p in patches:
        if binary.bytes_at(p.offset, len(p.original)) != p.original:
            raise PortError(f"{p.name}: original bytes do not match the binary")
        span = set(range(p.offset, p.offset + len(p.original)))
        if span & seen:
            raise PortError(f"{p.name}: overlaps another patch")
        seen |= span
        out[p.offset:p.offset + len(p.original)] = p.replacement
    return bytes(out)


def branch_scan(binary, patches, entry_points):
    """Disassemble the patched __text and return branches or calls from
    untouched code that land inside replaced bytes, other than at the start
    of a replaced run or at one of `entry_points` (offsets)."""
    changed = set()
    for p in patches:
        changed |= {p.offset + k for k in range(len(p.original)) if p.original[k] != p.replacement[k]}
    allowed = {x for x in changed if x - 1 not in changed} | set(entry_points)
    with tempfile.NamedTemporaryFile(suffix=".bin") as tmp:
        tmp.write(apply(binary, patches))
        tmp.flush()
        asm = subprocess.run(["otool", "-tV", tmp.name], capture_output=True, text=True, check=True).stdout
    bad = []
    for m in re.finditer(r"^([0-9a-f]{16})\t(?:j\w+|callq)\t0x([0-9a-f]+)$", asm, re.M):
        src, tgt = binary.off(int(m.group(1), 16)), binary.off(int(m.group(2), 16))
        if tgt in changed and tgt not in allowed and src not in changed:
            bad.append((binary.va(src), binary.va(tgt)))
    return bad


def c_array(name, data):
    rows = []
    for i in range(0, len(data), 12):
        rows.append("    " + ", ".join(f"0x{b:02X}" for b in data[i:i + 12]) + ",")
    return f"static const uint8_t {name}[] {{\n" + "\n".join(rows) + "\n};\n"


def write_build(builds_dir, os_build, binary, patches, notes):
    """Write builds/<os_build>.inc and regenerate builds/all.inc."""
    if not re.fullmatch(r"[0-9]{2}[A-Z][0-9]{1,5}[a-z]?", os_build):
        raise PortError(f"'{os_build}' does not look like a macOS build number")
    builds_dir = Path(builds_dir)
    builds_dir.mkdir(exist_ok=True)
    tag = "b" + os_build
    out = [
        "// Generated by tools/port.py. Do not edit: rerun the tool instead.",
        f"// bluetoothd of macOS build {os_build}, x86_64 slice at file offset 0x{binary.slice_offset:x}",
        f"// LC_UUID {binary.uuid.hex().upper()}",
    ]
    out += [f"// {n}" for n in notes]
    out.append("")
    for i, p in enumerate(patches):
        out.append(f"// {p.name}: 0x{binary.va(p.offset):x}")
        out.append(c_array(f"{tag}_o{i}", p.original) + c_array(f"{tag}_r{i}", p.replacement))
    out.append(f"static const BluetoothdPatch {tag}_patches[] {{")
    for i, p in enumerate(patches):
        out.append(f'    {{ 0x{p.offset:x}, {len(p.original)}, {tag}_o{i}, {tag}_r{i}, "{p.name}" }},')
    out.append("};\n")
    uuid = ", ".join(f"0x{b:02X}" for b in binary.uuid)
    out.append(f"#define BLUETOOTHD_BUILD_{os_build} \\")
    out.append(f'    {{ "{os_build}", 0x{binary.slice_offset:x}, 0x{binary.uuid_offset:x}, {{ {uuid} }}, \\')
    out.append(f"      {tag}_patches, arrsize({tag}_patches) }},")
    (builds_dir / f"{os_build}.inc").write_text("\n".join(out) + "\n")

    names = sorted(p.stem for p in builds_dir.glob("*.inc") if p.stem != "all")
    index = ["// Generated by tools/port.py. Do not edit.", ""]
    index += [f'#include "{n}.inc"' for n in names]
    index += ["", "static const BluetoothdBuild bluetoothdBuilds[] {"]
    index += [f"    BLUETOOTHD_BUILD_{n}" for n in names]
    index += ["};", ""]
    (builds_dir / "all.inc").write_text("\n".join(index))
    return names
