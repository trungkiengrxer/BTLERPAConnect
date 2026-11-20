//
//  page_patcher.hpp
//
//  Applies the generated per-build tables (builds/<macOS build>.inc) to one
//  4 KiB page of /usr/sbin/bluetoothd. No kernel dependencies, so the same
//  code runs in the kext and in tools/test_apply.cpp.
//

#ifndef page_patcher_hpp
#define page_patcher_hpp

struct BluetoothdPatch {
    uint32_t offset;                // file offset inside the x86_64 slice
    uint32_t size;
    const uint8_t *original;
    const uint8_t *replacement;
    const char *name;
};

struct BluetoothdBuild {
    const char *osBuild;            // macOS build number, e.g. "25G332"
    uint32_t sliceOffset;           // x86_64 slice offset in the fat file
    uint32_t uuidOffset;            // LC_UUID bytes, offset inside the slice
    uint8_t uuid[16];
    const BluetoothdPatch *patches;
    size_t patchCount;
};

static const uint32_t bluetoothdPageSize = 0x1000;
static const uint32_t bluetoothdMachMagic64 = 0xFEEDFACF;

static inline const BluetoothdBuild *bluetoothdFindBuild(const BluetoothdBuild *builds, size_t count, const char *osBuild) {
    for (size_t i = 0; i < count; i++)
        if (strcmp(osBuild, builds[i].osBuild) == 0)
            return &builds[i];
    return nullptr;
}

// False when `page` is the Mach-O header page of the x86_64 slice and its
// LC_UUID is not the one the table was generated from.
static inline bool bluetoothdIsExpectedBinary(const BluetoothdBuild &build, uint64_t pageOffset, const uint8_t *page) {
    if (pageOffset != build.sliceOffset || memcmp(page, &bluetoothdMachMagic64, sizeof(bluetoothdMachMagic64)) != 0)
        return true;
    return memcmp(page + build.uuidOffset, build.uuid, sizeof(build.uuid)) == 0;
}

// Writes every patch that lies on this page, only over its exact original
// bytes. `report(patch, applied)` is called for each write and for each
// patch whose bytes are neither the original nor the replacement.
template <typename Report>
static inline void bluetoothdPatchPage(const BluetoothdBuild &build, uint64_t pageOffset, uint8_t *page, Report report) {
    if (pageOffset < build.sliceOffset)
        return;

    uint64_t sliceOffset = pageOffset - build.sliceOffset;

    for (size_t i = 0; i < build.patchCount; i++) {
        const auto &patch = build.patches[i];

        if (patch.offset < sliceOffset || patch.offset + patch.size > sliceOffset + bluetoothdPageSize)
            continue;

        auto at = page + (patch.offset - sliceOffset);

        if (memcmp(at, patch.original, patch.size) == 0) {
            memcpy(at, patch.replacement, patch.size);
            report(patch, true);
        } else if (memcmp(at, patch.replacement, patch.size) != 0) {
            report(patch, false);
        }
    }
}

#endif /* page_patcher_hpp */
