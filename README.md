# BTLERPAConnect

A [Lilu](https://github.com/acidanthera/Lilu) plugin that makes **Instant Hotspot** work on Hackintoshes with a Broadcom **BCM20702** Bluetooth 4.0 controller (for example the Dell **DW1560**) under **macOS Tahoe 26.6**.

It patches `/usr/sbin/bluetoothd` at runtime so that the Mac can open a Bluetooth LE connection to the iPhone, which Instant Hotspot needs before it can ask the iPhone to turn on Personal Hotspot.

It is a companion to [BTLEAdvFix](https://github.com/trungkiengrxer/BTLEAdvFix), which fixes BLE advertising and AirDrop to older iPhones. The two plugins patch different code and can be used together.

> Unofficial project. Not affiliated with Apple, Dell or Broadcom. The patches are tied to one exact `bluetoothd` build and are ignored on any other build.

## Tested hardware and software

| Item | Value |
|------|-------|
| Bluetooth card | Dell DW1560 (BCM94352Z) |
| Bluetooth chipset | `BCM_20702A3`, USB `0489:E07A`, firmware `v14 c5747` |
| SMBIOS | `MacBookPro16,1` |
| macOS | Tahoe 26.6 (`25G72`), Intel x86_64 |
| Bluetooth kexts | Lilu 1.7.2, BlueToolFixup / BrcmPatchRAM3 2.7.2, BTLEAdvFix 0.2.1 |
| iPhone | iPhone 7 Plus on iOS 15.8.3 |

## Problem

Clicking the iPhone under **Personal Hotspot** in the Mac's Wi-Fi menu fails after a few seconds. The iPhone never sees a request. Logs on the Mac show:

```text
rapportd  [CLinkCnx] ### Connect failed: kBluetoothResetErr (Bluetooth reset)
sharingd  [TetheringAgent] ### Request complete: failed with error: ... "kNetworkErr (Connect failed)"
```

Joining the iPhone's hotspot manually from the Wi-Fi list works. Only the Instant Hotspot flow fails.

## How Instant Hotspot works

1. `sharingd` asks `rapportd` to send `com.sharing.hotspot.request` to the iPhone.
2. `rapportd` uses an on-demand Bluetooth LE link (`ForceL2CAP`, L2CAP PSM `0x81`). The **Mac** must open it.
3. `bluetoothd` adds the iPhone to the controller's filter accept list and sends `LE_Create_Connection`.
4. Over that link the iPhone turns on Personal Hotspot and returns the SSID, channel and password. The Mac then joins the Wi-Fi network.

On this hardware step 3 fails, for three separate reasons.

## Root cause 1: bluetoothd restarts itself on a false "firmware crash"

When any HCI command is rejected with `0x0C Command Disallowed`, bluetoothd checks the controller firmware with Apple's vendor command `BD_VSC_READ_FW_INFO` (`fcfe 04`). The BCM20702 does not implement it and answers `0x01 Unknown Command` (`STATUS 701`).

`LEHardwareModel::readCurrentUHEModeCB` then stores mode `0`, and the check treats every mode other than `2` as "Ultra High Efficiency" mode:

```text
bluetoothd  BD_VSC_READ_FW_INFO completed with status 701
bluetoothd  Bluetooth error - restarting { reason=654, context="FW is in UHE Mode. Firmware crashed..." }
```

bluetoothd restarts, which resets Bluetooth and drops every LE link. On the tested machine this happened dozens of times a day, and is likely behind the recurring `bluetoothd-*.ips` reports (`abort_with_payload`).

The same fix was found independently in [acidanthera/BrcmPatchRAM#37](https://github.com/acidanthera/BrcmPatchRAM/pull/37).

## Root cause 2: Bluetooth 4.0 cannot advertise connectable while connecting

An HCI trace shows that `LE_Create_Connection` is rejected with `0x0C` exactly when advertising is enabled, and accepted when it is not:

```text
CreateConn  adv=1 scan=1  -> 0x0c
CreateConn  adv=0 scan=1  -> 0x00
AdvEnable(1) while a connection is pending -> 0x0c
```

The Bluetooth 4.0 specification lists the allowed combinations of link layer states. "Connectable advertising + initiating" and "connectable advertising + connection" are missing; they were added in 4.1. "Scannable advertising + initiating/connection" are allowed. The BCM20702 follows the 4.0 list.

Apple's controllers are all newer, so bluetoothd never pauses or changes connectable advertising before it connects. The Mac advertises `ADV_IND` (connectable) almost all the time, so every outgoing connection is rejected.

The same limit breaks connections that the iPhone opens: after the link is up, bluetoothd re-enables connectable advertising, gets `0x0C`, and (because of root cause 1) restarts.

## Root cause 3: the accept list holds the identity address, the iPhone uses an RPA

bluetoothd adds the iPhone's identity address to the accept list:

```text
Adding address "Public 98:10:XX:XX:XX:XX" to connection FilterAcceptList
```

The iPhone advertises with a resolvable private address (RPA) that changes about every 15 minutes. A Bluetooth 4.2 controller with LL Privacy, or Apple's firmware with a vendor IRK command, resolves the RPA in the controller. The BCM20702 can do neither, so a connection with the identity address in the accept list never matches. With advertising off, `LE_Create_Connection` was accepted but no connection completed in 16 seconds.

bluetoothd itself can resolve RPAs in software (it does so for every advertisement it receives), so the information is there. It is just not used for the accept list.

## How it works

All patches are applied in `cs_validate_page` to pages of `/usr/sbin/bluetoothd`, as BTLEAdvFix does. A page that does not contain the exact expected bytes is left alone. The added routines are in [`docs/cave.s`](docs/cave.s). They live in the zero padding after `__eh_frame` (after BTLEAdvFix's routine), and keep their state (386 bytes) in unused space at the end of `__DATA`.

| # | Address (25G72) | Change |
|---|-----------------|--------|
| 1 | `0x10033cbfd` | `readCurrentUHEModeCB`: on failure store mode `2` (normal) instead of `0`. No more false restarts. |
| 2 | `0x1001f5c66`, `0x1001f6693` | Where `LE_Set_Advertising_Parameters` is built: if bluetoothd asks for `ADV_IND` while an `LE_Create_Connection` is pending (`lConnectionInProgress`) or an LE link is up, send `ADV_SCAN_IND` instead. The interval is raised to at least `0xA0`, which Bluetooth 4.0 requires for `ADV_SCAN_IND`. Otherwise advertising stays connectable. |
| 3 | `0x10020537f`, `0x100205ea4` | Both functions that build `LE_Create_Connection`: if the controller is advertising connectable, call bluetoothd's own "silent restart" of advertising (`0x1001f6d42`). Its disable command is queued ahead of `LE_Create_Connection`, and the parameters are sent again after it, when patch 2 picks `ADV_SCAN_IND`. |
| 4 | `0x100214c06`, `0x100209514` | `GAP_ConnectionCompleteCallback` and the LE disconnection handler: count LE links. When the last link goes and nothing is connecting, silently restart advertising again so it goes back to `ADV_IND`. |
| 5 | `0x100227190` | `LE_ResolveAddress`: whenever an RPA resolves to a paired device, record identity -> RPA in a 16-entry table. |
| 6 | `0x1002350ec`, `0x10023540c` | Accept list add / remove: if an RPA is known for the identity address, send the RPA (random address type) to the controller instead, and remove the same RPA later. bluetoothd's own bookkeeping still uses the identity address. |

The connection complete event then reports the RPA, which bluetoothd already resolves to the paired iPhone in software.

## Result

HCI trace of one Instant Hotspot request with 0.2.1:

```text
05:03:59.398  LE_Add_Device_To_Filter_Accept_List  01 61:1C:02:8E:AD:C1   (iPhone's current RPA)
05:03:59.403  LE_Set_Advertising_Enable 00                                 (patch 3)
05:03:59.405  LE_Create_Connection -> status 00
05:03:59.411  LE_Set_Advertising_Parameters type 02, LE_Set_Advertising_Enable 01
05:03:59.654  LE Connection Complete, status 00, central, peer 61:1C:02:8E:AD:C1
05:04:02.233  sharingd: Request complete: success, name: iPhone, channel: 6, hasPassword: yes
05:04:02.278  link closed, advertising back to type 00
05:04:05.369  DHCP on en0: 172.20.10.3 from 172.20.10.1
```

From click to network: about 6 seconds. No `0x0C` rejections and no bluetoothd restarts during the test. AirDrop from the Mac to the iPhone (BTLEAdvFix) kept working: 4 of 4 transfers.

## Installation

1. Download `BTLERPAConnect-<version>-RELEASE.zip` from the latest release and extract `BTLERPAConnect.kext`.
2. Copy it to `EFI/OC/Kexts`, replacing any older version. Delete the old folder first; a Finder merge can leave the old binary in place.
3. Add it to `Kernel -> Add` after `Lilu.kext`. Keep your existing Bluetooth kexts.
4. Reboot.

```bash
rm -rf /Volumes/EFI/EFI/OC/Kexts/BTLERPAConnect.kext
ditto BTLERPAConnect.kext /Volumes/EFI/EFI/OC/Kexts/BTLERPAConnect.kext
plutil -extract CFBundleVersion raw /Volumes/EFI/EFI/OC/Kexts/BTLERPAConnect.kext/Contents/Info.plist
```

Recommended load order:

```text
Lilu.kext
BlueToolFixup.kext
BrcmFirmwareData.kext / BrcmFirmwareStore.kext
BrcmPatchRAM3.kext
BTLEAdvFix.kext
BTLERPAConnect.kext
```

BTLERPAConnect does not depend on BTLEAdvFix, but without BTLEAdvFix the BCM20702 does not advertise at all under Tahoe, and the iPhone will not offer the Mac a hotspot in the first place.

Boot arguments:

| Boot argument | Effect |
|---------------|--------|
| `-blrpaoff` | Disable the plugin |
| `-blrpadbg` | Enable debug logging (DEBUG build of the plugin and Lilu) |
| `-blrpabeta` | Allow loading on unsupported macOS versions |

## Verification

Use `/usr/bin/log` with the full path. In zsh, plain `log` is a shell builtin.

```bash
kmutil showloaded | grep -iE 'Lilu|BTLE'
```

No more false restarts:

```bash
/usr/bin/log show --last boot --style compact \
  --predicate 'process == "bluetoothd" AND eventMessage CONTAINS "restarting"'
```

Instant Hotspot results:

```bash
/usr/bin/log show --last 1h --style compact \
  --predicate 'process == "sharingd" AND category == "TetheringAgent" AND eventMessage CONTAINS "Request complete"'
```

Look for `Request complete: success with info: <SFRemoteHotspotInfo ...>`.

To look at the HCI commands themselves, record a trace as described in the [BTLEAdvFix README](https://github.com/trungkiengrxer/BTLEAdvFix#debugging-with-an-hci-trace). In a working attempt `LE_Set_Advertising_Enable 00` comes right before `LE_Create_Connection`, and the accept list entry has address type `01`.

## Porting to another macOS build

The patches match the exact bytes of `bluetoothd` in macOS 26.6 (25G72). After a macOS update, run:

```bash
python3 tools/check_patterns.py
```

It checks that:

- every in-place pattern matches exactly once and stays within one 4 KiB page;
- the `__eh_frame` anchor is unique and the cave after it is free;
- the routine bytes in `kern_start.cpp` equal `docs/cave.s` assembled and linked at `0x100aa2440`;
- every inserted `call` lands on its routine, and the only call out of the cave goes to the silent restart function;
- no branch anywhere in `__text` lands inside replaced bytes.

A running BTLERPAConnect patches the page cache, so `/usr/sbin/bluetoothd` then reads back patched. Run the check after booting with `-blrpaoff`, or pass a copy of the original binary as the first argument.

To port, find the same places in the new build. Useful strings:

| Patch | Search for |
|-------|------------|
| 1 | `readCurrentUHEModeCB`, `FW is in UHE Mode` |
| 2 | command index `0xa6` (`LE_Set_Advertising_Parameters`) passed to the HCI command helper |
| 3 | `Connecting with interval` (two functions) |
| 3, 4 | `Advertising stopped due to %s`, `silentRestartSequence`, `lConnectionInProgress` |
| 4 | `GAP_ConnectionCompleteCallback`, `OI_HCIEventHandler_DisconnectionComplete` |
| 5 | callers of `LE_ResolveUsingIRK` |
| 6 | `Could not add device to filter accept list`, `Could not remove device from filter accept list` (command indices `0xb1`, `0xb2`) |

Then update the addresses in [`docs/cave.s`](docs/cave.s), rebuild its bytes, and update `kern_start.cpp` and `tools/check_patterns.py`.

## Building

Dependencies:

- Xcode
- [Lilu](https://github.com/acidanthera/Lilu) DEBUG build (contains the plugin SDK)
- [MacKernelSDK](https://github.com/acidanthera/MacKernelSDK)

Place them in the repository root:

```text
Lilu.kext
MacKernelSDK
```

```bash
xcodebuild \
  -project BTLERPAConnect.xcodeproj \
  -target BTLERPAConnect \
  -configuration Release \
  SYMROOT="$PWD/build" \
  build
```

The kext and a release zip are written to `build/Release`. To rebuild the routine bytes:

```bash
clang -c -arch x86_64 docs/cave.s -o /tmp/cave.o
ld -arch x86_64 -preload -e base -segaddr __TEXT 0x100aa2440 /tmp/cave.o -o /tmp/cave.bin
otool -tV /tmp/cave.bin
```

## Compatibility

Currently verified only on:

- Dell DW1560 / BCM20702A3 Bluetooth
- macOS Tahoe 26.6 (25G72)
- Intel x86_64
- iPhone on iOS 15.8.3

Other BCM20702 cards have the same Bluetooth 4.0 limits and are likely affected in the same way, but have not been tested. Cards with an Apple Bluetooth module (for example BCM94360NG/CS2) do not need this plugin.

## Known limitations

- **While the Mac connects or has an LE link up, it advertises as scannable, not connectable.** Other devices cannot open a new LE connection to the Mac during that time. On a Bluetooth 4.0 controller connectable advertising is impossible in these states anyway; before this plugin those connections ended in a bluetoothd restart.
- **The link counter is maintained by the plugin.** If bluetoothd reports a connection or disconnection in a way not seen during testing, advertising can stay in the wrong mode until bluetoothd restarts advertising for another reason (for example the next address rotation).
- **The RPA table is empty after bluetoothd starts.** Until bluetoothd has resolved one of the iPhone's advertisements, the identity address is used and a connection attempt is expected to fail. Wake the iPhone near the Mac for a few seconds and try again.
- **Turning off the hotspot is up to the iPhone.** When the Mac switches to another network it does not send a stop request; the iPhone turns Personal Hotspot off on its own after a while.
- **Other Continuity features are not fixed.** Other Apple vendor commands are still rejected by the BCM20702.

## Safety warning

This project patches executable code of a system daemon at runtime and adds routines that call into it.

A mismatched macOS build is ignored by design: patterns simply do not match. Even so, any patch to `bluetoothd` can cause Bluetooth instability.

Keep a working EFI backup, and keep a reliable way to disable the plugin: the `-blrpaoff` boot argument or removing the kext.

## Changelog

- **0.2.1**: first release. False UHE restart fix, dynamic connectable/scannable advertising (both connection paths, `ADV_SCAN_IND` interval raised to `0xA0`), connection to the iPhone's current RPA. Instant Hotspot works.
- **0.2.0** (not released): only one of the two `LE_Create_Connection` paths was hooked, and fast scannable advertising was rejected with `0x12`.
- **0.1.0** (not released): always advertised `ADV_SCAN_IND`. Replaced by the dynamic switch so the Mac stays connectable when idle.

## Credits

- Acidanthera for Lilu, MacKernelSDK, BrcmPatchRAM and BlueToolFixup
- [CommandMaker](https://github.com/acidanthera/BrcmPatchRAM/pull/37) for independently identifying the UHE check on non-Apple Broadcom cards
- FeatureUnlock and RealtekUVCFix as reference implementations for runtime page patching

## License

See `LICENSE`.
