//
//  kern_start.cpp
//  BTLERPAConnect.kext
//
//  Runtime patches for /usr/sbin/bluetoothd (macOS Tahoe, x86_64) on
//  non-Apple Broadcom BCM20702 controllers (e.g. DW1560, Fenvi T919), so
//  LE links (Instant Hotspot, AirDrop, Auto Unlock) are not torn down.
//  Independent of BTLEAdvFix.
//
//  What is patched and why is described in README.md, docs/cave.s and
//  tools/port.py. This file holds no build-specific bytes: it only applies
//  the tables in builds/<macOS build>.inc, which tools/port.py generates
//  from a clean bluetoothd by signature search.
//
//  Safety rules:
//    * The table is chosen by the running macOS build (kern.osversion).
//      An unknown build is never patched, there is no pattern search here.
//    * The LC_UUID in the Mach-O header page of bluetoothd must be the one
//      the table was generated from, otherwise patching stops for good.
//    * A patch is written only at its exact file offset and only over the
//      exact original bytes.
//

#include <Headers/plugin_start.hpp>
#include <Headers/kern_api.hpp>
#include <Headers/kern_user.hpp>

#include <sys/vnode.h>

#define MODULE_SHORT "blrpa"

#include "page_patcher.hpp"
#include "builds/all.inc"

static const char bluetoothdPath[] = "/usr/sbin/bluetoothd";

static mach_vm_address_t originalCsValidatePage {};

// kern.osversion. launchd fills it in early in boot, long before bluetoothd
// starts, so it is read when the first bluetoothd page arrives.
static const char *kernelOsVersion {};

static const BluetoothdBuild *currentBuild {};
static bool buildResolved {};
static bool binaryRejected {};

static const BluetoothdBuild *resolveBuild() {
    if (buildResolved)
        return currentBuild;

    if (kernelOsVersion == nullptr || kernelOsVersion[0] == '\0')
        return nullptr;

    currentBuild = bluetoothdFindBuild(bluetoothdBuilds, arrsize(bluetoothdBuilds), kernelOsVersion);
    buildResolved = true;

    if (currentBuild)
        SYSLOG(MODULE_SHORT, "macOS build %s is supported", kernelOsVersion);
    else
        SYSLOG(MODULE_SHORT, "macOS build %s is not supported, bluetoothd is left untouched (see tools/port.py)", kernelOsVersion);

    return currentBuild;
}

static void patchedCsValidatePage(
    vnode_t vnode,
    memory_object_t pager,
    memory_object_offset_t pageOffset,
    const void *data,
    int *validated,
    int *tainted,
    int *nx
) {
    FunctionCast(
        patchedCsValidatePage,
        originalCsValidatePage
    )(vnode, pager, pageOffset, data, validated, tainted, nx);

    if (data == nullptr || binaryRejected)
        return;

    char path[PATH_MAX] {};
    int pathLength = sizeof(path);

    if (vn_getpath(vnode, path, &pathLength) != 0)
        return;

    if (strcmp(path, bluetoothdPath) != 0)
        return;

    auto build = resolveBuild();

    if (build == nullptr)
        return;

    auto page = static_cast<uint8_t *>(const_cast<void *>(data));

    // The header page is the first one dyld touches. A different LC_UUID
    // means this is not the binary the table was made from.
    if (!bluetoothdIsExpectedBinary(*build, pageOffset, page)) {
        binaryRejected = true;
        SYSLOG(MODULE_SHORT, "bluetoothd is not the one shipped with %s, it is left untouched", build->osBuild);
        return;
    }

    // Patch on every page-in: the page cache can drop and reload a page.
    // Routines and call sites sit on different pages; each page is patched
    // when it faults in, before any code on it runs.
    bluetoothdPatchPage(*build, pageOffset, page, [](const BluetoothdPatch &patch, bool applied) {
        if (applied)
            DBGLOG(MODULE_SHORT, "applied %s at slice offset 0x%x", patch.name, patch.offset);
        else
            SYSLOG(MODULE_SHORT, "unexpected bytes for %s at slice offset 0x%x, not patched", patch.name, patch.offset);
    });
}

static void pluginStart() {
    DBGLOG(MODULE_SHORT, "BTLERPAConnect starting");

    lilu.onPatcherLoadForce([](void *, KernelPatcher &patcher) {
        kernelOsVersion = reinterpret_cast<const char *>(patcher.solveSymbol(KernelPatcher::KernelID, "_osversion"));

        if (kernelOsVersion == nullptr) {
            patcher.clearError();
            SYSLOG(MODULE_SHORT, "failed to find kern.osversion, bluetoothd is left untouched");
            return;
        }

        KernelPatcher::RouteRequest request(
            "_cs_validate_page",
            patchedCsValidatePage,
            originalCsValidatePage
        );

        if (!patcher.routeMultipleLong(KernelPatcher::KernelID, &request, 1))
            SYSLOG(MODULE_SHORT, "failed to route _cs_validate_page");
    });
}

static const char *bootargOff[] {
    "-blrpaoff"
};

static const char *bootargDebug[] {
    "-blrpadbg"
};

static const char *bootargBeta[] {
    "-blrpabeta"
};

PluginConfiguration ADDPR(config) {
    xStringify(PRODUCT_NAME),
    parseModuleVersion(xStringify(MODULE_VERSION)),
    LiluAPI::AllowNormal,
    bootargOff,
    arrsize(bootargOff),
    bootargDebug,
    arrsize(bootargDebug),
    bootargBeta,
    arrsize(bootargBeta),
    KernelVersion::Tahoe,
    KernelVersion::Tahoe,
    pluginStart
};
