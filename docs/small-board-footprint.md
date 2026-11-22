# Small-board footprint (measured, no code restructured)

Method: `mpy-cross` (MicroPython 8a56be6660, mpy v6.3, built at `/Users/milen/micropython/mpy-cross/build/mpy-cross`)
run on every `firmware/urns/**/*.py`, once per tree: git HEAD (`git archive HEAD firmware/urns`) and the working tree
(branch `rns-161-stamps`, uncommitted). "Eager" = the modules in `sys.modules` after `import urns` (traced on the host with
the MicroPython modules shimmed; `resource`, the stamp modules and the interfaces are NOT in it). Sizes are the `.mpy` file
bytes (`-march=xtensawin` and `-march=armv6m` give byte-identical sizes for these modules: no `@native`/`@viper` code
in `urns/`, so the architecture flag only changes the header). Resident RAM is more than file size (qstr table, constants,
and on RP2040 the filesystem bytecode is loaded into the heap); these numbers are a relative guide, not a heap measurement.
No on-device `gc.mem_free()` comparison was made. The uniform -9 B on unchanged modules is an artifact: the source path (shorter in the working-tree copy) is stored in the bytecode, so deltas under ~10 B per module are noise (about 216 B over the whole table).

## Loaded by `import urns` (bytes of bytecode)

| module | HEAD | working tree | delta |
|---|---:|---:|---:|
| `lxmf` | 12,000 | 23,966 | +11966 |
| `transport` | 22,847 | 28,381 | +5534 |
| `destination` | 5,026 | 7,758 | +2732 |
| `link` | 17,572 | 20,234 | +2662 |
| `reticulum` | 4,859 | 5,949 | +1090 |
| `identity` | 7,470 | 8,342 | +872 |
| `packet` | 5,563 | 6,076 | +513 |
| `const` | 3,048 | 3,458 | +410 |
| `crypto.ed25519` | 1,370 | 1,649 | +279 |
| `buffer` | 3,655 | 3,646 | -9 |
| `bz2dec` | 3,168 | 3,159 | -9 |
| `channel` | 6,116 | 6,107 | -9 |
| `crypto` | 562 | 553 | -9 |
| `crypto.aes` | 672 | 663 | -9 |
| `crypto.hashes` | 418 | 409 | -9 |
| `crypto.hkdf` | 577 | 568 | -9 |
| `crypto.hmac` | 1,352 | 1,343 | -9 |
| `crypto.pkcs7` | 409 | 400 | -9 |
| `crypto.sha512` | 3,436 | 3,427 | -9 |
| `crypto.token` | 1,380 | 1,371 | -9 |
| `crypto.x25519` | 2,208 | 2,199 | -9 |
| `interfaces` | 1,964 | 1,955 | -9 |
| `log` | 804 | 795 | -9 |
| `umsgpack` | 3,111 | 3,102 | -9 |
| **total (24 modules)** | **109,587** | **135,510** | **+25,923 (23%)** |

## Loaded later, on demand

| module | HEAD | working tree | loaded when |
|---|---:|---:|---|
| `resource` | 9,687 | 9,869 | first Resource (LXMF over ~415 B, link response, NomadNet page) |
| `stamp_check` | - | 2,234 | first stamped announce received (`identity._split_ext`), first inbound link request to a destination with `link_cost` > 0, first stored-stamp check |
| `stamps` | - | 7,570 | first stamped path request, link key search (announced link cost), PoW message stamp, or announce stamp generation; imports `stamp_check` |
| `announce_stamps` | - | 4,197 | board generates stamps (native hashcash present) or a stored `.dstamp` exists, at the first announce / destination |
| `interfaces.udp` / `tcp` / `lora` / `serial` / `e32` | 3,441 / 3,958 / 7,079 / 2,735 / 3,049 | 3,432 / 4,655 / 7,070 / 2,726 / 3,040 | only the configured ones, in `setup_interfaces` |

First stamped announce received: +2,234 B (`stamp_check`). First LXMF send to a peer that announces a stamp cost,
or first path request with stamping on: +9,804 B (`stamps` + `stamp_check`). A board that generates its
own announce stamp: additionally +4,197 B (`announce_stamps`). First link to a destination with a link cost: `stamps` too (the
key search); first large message: `resource` (+9,869 B, same as before the branch).
Worst case on a stamping board: eager total + `resource` + `stamps` + `stamp_check` + `announce_stamps` =
159,380 B (HEAD: 119,274 B).

## Update: simplification round (measured with the same method, `-march=xtensawin`, same relative paths)

Before = commit 5870adc, after = working tree with the reviewed simplifications. Eager set unchanged (same 25 files).

| module | before | after | delta |
|---|---:|---:|---:|
| `lxmf` | 23,821 | 21,341 | -2,480 |
| `transport` | 28,236 | 27,451 | -785 |
| `destination` | 7,613 | 7,487 | -126 |
| `link` | 20,089 | 20,034 | -55 |
| `crypto.ed25519` | 1,504 | 1,409 | -95 |
| `packet` | 5,931 | 5,894 | -37 |
| **eager total** | **132,936** | **129,358** | **-3,578** |
| `resource` (on demand) | 9,724 | 9,613 | -111 |
| `stamps` (on demand) | 7,425 | 5,463 | -1,962 |
| `stamp_hw` (new, only with `stamps_hw_sha`) | - | 1,929 | +1,929 |
| `stamp_check` | 2,089 | 2,122 | +33 |
| `announce_stamps` | 4,052 | 3,890 | -162 |
| `interfaces.tcp` | 4,510 | 4,281 | -229 |

The hardware-SHA policy moved out of `stamps.py` into `stamp_hw.py`, which only loads when `stamps_hw_sha` is on: a board
without the accelerator never loads those 1,929 B (`stamps` itself is 1,962 B smaller). Worst case on a stamping board
without the accelerator: eager + `resource` + `stamps` + `stamp_check` + `announce_stamps` = 129,358 + 9,613 + 5,463 + 2,122 + 3,890 = 150,446 B.

## Where the growth is, and what could be lazy or left out of a leaf profile (not done)

Of the +25,923 B eager growth, `lxmf` is +11,966 B, `transport` +5,534 B, `link` +2,662 B and `destination` +2,732 B.

* `lxmf.py` (+11,966 B): the outbound scheduler (queue, `process_outbound`, retry/slow-path logic,
  adaptive wait, path-request debounce, ticket and PoW stamp handling) could move to a module imported by `LXMRouter.send_message` only
  (a receive-only node, or the e-ink status node, never sends LXMF). PoW stamping (`_pow_stamp*`, `pow_stamp_cap`) is separable and needs `stamps` anyway.
* `transport.py` (+5,534 B): the path-request stamp task (`_pr_stamp_*`), `ensure_path` waiters, in-use route protection and the
  ingress-queue handling are leaf-useful, but the stamp task is only needed when `stamp_path_requests` is on.
* `link.py` (+2,662 B): the `OutgoingLink` stamp search (`_stamp_*`, `_start_stamp_search`) and the inbound stamp check are small; `OutgoingLink` as a whole is
  not needed by a node that never opens links (Link/Channel/Buffer are already eager here and could be lazy: 9,753 B for `channel`+`buffer`).
* `destination.py` (+2,732 B): the emission-time/RTC code (`_emission_time`, `_rtc_*`, `_flash_*`) could live in its own module loaded on the first announce.
* `identity.py` (+872 B), `reticulum.py` (+1,090 B), `packet.py`, `const.py`, `tcp.py`: small; not worth splitting.
* The stamp modules themselves (`stamp_check`, `stamps`, `announce_stamps`: 14,001 B) are already lazy and
  would simply never load on a leaf with `stamp_announces` off, no hashcash, and no stamp-requiring peers; the receive path then still loads `stamp_check` (2 KB) on the first stamped announce.
* Not present in this tree (so nothing to compare): the relay split (`transport_relay.py`) and lazy Link/Channel/Buffer that earlier notes describe for the RP2040 leaf profile.

Sizing consequence (inference, not measured on a device): the working tree's eager set is about a quarter larger than HEAD's, which
matters for RP2040 (bytecode from filesystem is loaded into a ~200 KB heap) and for the no-PSRAM Wireless Paper (95 KB heap); on ESP32-S3 with PSRAM it is irrelevant.

## Measured earlier (not re-measured for this document)

* A board without native crypto needs 3-12 s per announce validation (pure-Python Ed25519 verify), so it cannot hold a connection
  to a hub that pushes ~8 announces/s (the ingress queue stays full, other tasks starve), but it works on a quiet link.
* With the native module loaded under its IRAM name the same operations take ~12-20 ms.
