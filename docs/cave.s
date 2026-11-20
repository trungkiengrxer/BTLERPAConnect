# BTLERPAConnect routines for /usr/sbin/bluetoothd (x86_64), build independent.
#
# Placed in the zero padding after __eh_frame, at __eh_frame + 0x180 (the
# range before it belongs to BTLEAdvFix's routine). tools/port.py finds the
# build-specific addresses by signature and passes them in:
#
#   CAVE              where this code is placed
#   ADV_TYPE          legacy advertising type requested by bluetoothd
#   CONN_IN_PROGRESS  lConnectionInProgress (LE_CreateConn pending)
#   SILENT_RESTART    stop + resend params + enable, if advertising
#   TABLE             start of the state below
#
# The stack slots used by connectStart (-0x40) and connComplete (-0xa8) are
# part of the signatures in port.py: a build that moves them is not ported.
#
# State in the unused, zero-filled tail of __DATA (after the last section):
#   TABLE     16 entries x 24 bytes, identity -> RPA
#     +0  identity address (type byte + 6 bytes, as stored by bluetoothd)
#     +7  last RPA seen for it (type 1 + 6 bytes), written by LE_ResolveAddress
#     +14 RPA put in the controller accept list (used again for the removal)
#   LINKS     number of LE links up
#   CUR_TYPE  advertising type last sent to the controller

# The addresses in the comments below are those of 25G72, kept as a guide.

.set ENTRIES,           16
.set ENTRY,             24
.set LINKS,             TABLE + ENTRIES * ENTRY
.set CUR_TYPE,          LINKS + 1

.text
.globl base
base:

# 0x1001f5c66 / 0x1001f6693, replaces: movzbl ADV_TYPE(%rip), %r8d
# (edx = interval min, ecx = interval max, already loaded)
# BCM20702 (BT 4.0) cannot advertise connectable while initiating or while
# an LE link is up. Only then use ADV_SCAN_IND (2) instead of ADV_IND (0).
# BT 4.0 requires an interval of at least 0xA0 (100 ms) for ADV_SCAN_IND,
# so fast advertising (e.g. 0x30) is slowed down to 0xA0 in that case.
advType:
    movzbl  base + (ADV_TYPE - CAVE)(%rip), %r8d
    testl   %r8d, %r8d
    jnz     1f
    cmpb    $0, base + (CONN_IN_PROGRESS - CAVE)(%rip)
    jne     2f
    cmpb    $0, base + (LINKS - CAVE)(%rip)
    je      1f
2:  movl    $2, %r8d
    cmpl    $0xA0, %edx
    jae     3f
    movl    $0xA0, %edx
3:  cmpl    $0xA0, %ecx
    jae     1f
    movl    $0xA0, %ecx
1:  movb    %r8b, base + (CUR_TYPE - CAVE)(%rip)
    ret

# LE connect, before LE_CreateConn is built (two functions build it):
#   0x10020537f (fn 0x100204cb3), replaces movw %r14w, -0x40(%rbp)
#   0x100205ea4 (fn 0x100205623), replaces movw %r13w, -0x40(%rbp)
# If the controller advertises connectable, restart advertising: the disable
# is queued ahead of LE_CreateConn, and the parameters are resent after it,
# when lConnectionInProgress is set, so advType picks ADV_SCAN_IND.
connectStart:
    movw    %r14w, -0x40(%rbp)
    jmp     connectCheck
connectStart2:
    movw    %r13w, -0x40(%rbp)
connectCheck:
    cmpb    $0, base + (CUR_TYPE - CAVE)(%rip)
    jne     1f
    call    silentRestart
1:  ret

# 0x100214c06, GAP_ConnectionCompleteCallback; replaces:
#   cmpl $0x0, -0xa8(%rbp)        (status; the je after it is kept)
connComplete:
    cmpl    $0, -0xa8(%rbp)
    jne     1f
    incb    base + (LINKS - CAVE)(%rip)
    cmpl    $0, -0xa8(%rbp)
1:  ret

# 0x100209514, LE disconnection complete; replaces:
#   movl %edx, %r14d ; testq %rsi, %rsi   (the je after it is kept)
# When the last link goes and nothing is connecting, go back to connectable.
disconnect:
    movl    %edx, %r14d
    testq   %rsi, %rsi
    je      1f
    cmpb    $0, base + (LINKS - CAVE)(%rip)
    je      2f
    decb    base + (LINKS - CAVE)(%rip)
    jne     2f
    cmpb    $0, base + (CONN_IN_PROGRESS - CAVE)(%rip)
    jne     2f
    cmpb    $2, base + (CUR_TYPE - CAVE)(%rip)
    jne     2f
    call    silentRestart
2:  testq   %rsi, %rsi
1:  ret

# Calls SILENT_RESTART(0) keeping every caller-saved register.
# Both patched sites run with rsp = 0 mod 16; two return addresses
# (site -> routine -> here), 9 pushes and 8 bytes of padding keep rsp
# 0 mod 16 at the call.
silentRestart:
    subq    $8, %rsp
    pushq   %rax
    pushq   %rcx
    pushq   %rdx
    pushq   %rsi
    pushq   %rdi
    pushq   %r8
    pushq   %r9
    pushq   %r10
    pushq   %r11
    xorl    %edi, %edi
    call    base + (SILENT_RESTART - CAVE)
    popq    %r11
    popq    %r10
    popq    %r9
    popq    %r8
    popq    %rdi
    popq    %rsi
    popq    %rdx
    popq    %rcx
    popq    %rax
    addq    $8, %rsp
    ret

# 0x100227190, replaces the identity stores and "found = 1" in
# LE_ResolveAddress, then records identity -> RPA (r14 = identity out,
# r15 = RPA, rbx = found flag; rax is dead afterwards).
resolveRecord:
    movb    %al, 6(%r14)
    movw    %dx, 4(%r14)
    movl    %ecx, (%r14)
    movb    $1, (%rbx)
    pushq   %rcx
    pushq   %rsi
    pushq   %rdi
    pushq   %r8
    movq    %r14, %rdi
    call    findEntry
    testq   %rax, %rax
    jnz     3f
    leaq    base + (TABLE - CAVE)(%rip), %rax
    leaq    base + (TABLE + ENTRIES * ENTRY - CAVE)(%rip), %rsi
1:  movl    (%rax), %r8d
    orl     3(%rax), %r8d
    jz      2f
    addq    $ENTRY, %rax
    cmpq    %rsi, %rax
    jb      1b
    # ponytail: table full, reuse entry 0; grow ENTRIES if more devices matter
    leaq    base + (TABLE - CAVE)(%rip), %rax
2:  movl    (%r14), %r8d
    movl    %r8d, (%rax)
    movl    3(%r14), %r8d
    movl    %r8d, 3(%rax)
    movl    $0, 14(%rax)
    movl    $0, 17(%rax)
3:  movl    (%r15), %ecx
    movl    %ecx, 7(%rax)
    movl    3(%r15), %ecx
    movl    %ecx, 10(%rax)
    popq    %r8
    popq    %rdi
    popq    %rsi
    popq    %rcx
    ret

# 0x1002350ec (add to accept list), replaces:
#   movzbl (%r15), %edx ; leaq 1(%r15), %rcx
# When an RPA is known for the identity in r15, send that RPA instead.
acceptAdd:
    pushq   %r8
    movq    %r15, %rdi
    call    findEntry
    testq   %rax, %rax
    jz      acceptOriginal
    cmpb    $0, 7(%rax)
    je      acceptOriginal
    movl    7(%rax), %ecx
    movl    %ecx, 14(%rax)
    movl    10(%rax), %ecx
    movl    %ecx, 17(%rax)
    jmp     acceptAdded

# 0x10023540c (remove from accept list), same replaced instructions.
# Remove the RPA that was added, not the current one.
acceptRemove:
    pushq   %r8
    movq    %r15, %rdi
    call    findEntry
    testq   %rax, %rax
    jz      acceptOriginal
    cmpb    $0, 14(%rax)
    je      acceptOriginal
acceptAdded:
    movzbl  14(%rax), %edx
    leaq    15(%rax), %rcx
    popq    %r8
    ret
acceptOriginal:
    movzbl  (%r15), %edx
    leaq    1(%r15), %rcx
    popq    %r8
    ret

# rdi = 7-byte address; returns rax = table entry with that identity or 0.
# Clobbers rsi, r8.
findEntry:
    leaq    base + (TABLE - CAVE)(%rip), %rax
    leaq    base + (TABLE + ENTRIES * ENTRY - CAVE)(%rip), %rsi
1:  movl    (%rax), %r8d
    cmpl    (%rdi), %r8d
    jne     2f
    movl    3(%rax), %r8d
    cmpl    3(%rdi), %r8d
    je      3f
2:  addq    $ENTRY, %rax
    cmpq    %rsi, %rax
    jb      1b
    xorl    %eax, %eax
3:  ret
