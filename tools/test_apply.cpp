// User-space test of the page patcher against a real bluetoothd.
//
//   clang++ -std=c++14 -I BTLERPAConnect tools/test_apply.cpp -o /tmp/test_apply
//   /tmp/test_apply <clean bluetoothd> <macOS build> <patched output>
//
// Feeds the file to the same code the kext runs, one 4 KiB page at a time,
// and writes the result. Exit code 0 = every patch of the build was applied.
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <vector>

#define arrsize(x) (sizeof(x) / sizeof((x)[0]))

#include "page_patcher.hpp"
#include "builds/all.inc"

int main(int argc, char **argv) {
    if (argc != 4)
        return 2;

    auto build = bluetoothdFindBuild(bluetoothdBuilds, arrsize(bluetoothdBuilds), argv[2]);
    if (!build) {
        printf("build %s is not supported, nothing patched\n", argv[2]);
        return 1;
    }

    FILE *in = fopen(argv[1], "rb");
    if (!in)
        return 2;
    std::vector<uint8_t> data;
    uint8_t buffer[4096];
    for (size_t n; (n = fread(buffer, 1, sizeof(buffer), in)) > 0;)
        data.insert(data.end(), buffer, buffer + n);
    fclose(in);
    data.resize((data.size() + 4095) / 4096 * 4096);

    size_t applied = 0, unexpected = 0;
    for (uint64_t offset = 0; offset < data.size(); offset += 4096) {
        if (!bluetoothdIsExpectedBinary(*build, offset, &data[offset])) {
            printf("LC_UUID mismatch, nothing patched\n");
            return 1;
        }
        bluetoothdPatchPage(*build, offset, &data[offset], [&](const BluetoothdPatch &patch, bool ok) {
            ok ? applied++ : unexpected++;
            printf("%s %s\n", ok ? "applied   " : "UNEXPECTED", patch.name);
        });
    }

    FILE *out = fopen(argv[3], "wb");
    fwrite(data.data(), 1, data.size(), out);
    fclose(out);
    printf("%zu of %zu applied, %zu unexpected\n", applied, build->patchCount, unexpected);
    return applied == build->patchCount && unexpected == 0 ? 0 : 1;
}
