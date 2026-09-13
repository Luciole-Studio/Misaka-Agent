# libghostty-vt builds

The panel's terminal emulator is ghostty's VT library, the copy herdr vendors
(github.com/herdrdev/herdr, tag v0.8.2, `vendor/libghostty-vt`, ghostty commit
c5a21edfc with herdr's grapheme-clustering patch). `ghostty.py` loads
`libghostty-vt-<os>-<arch>.<ext>` for the running platform:

| file | platform |
|---|---|
| `libghostty-vt-darwin-arm64.dylib` | macOS, Apple silicon |
| `libghostty-vt-darwin-x86_64.dylib` | macOS, Intel |
| `libghostty-vt-linux-x86_64.so` | Linux x86_64 (glibc ≥ 2.28) |
| `libghostty-vt-linux-arm64.so` | Linux aarch64 (glibc ≥ 2.28) |

`MISAKA_GHOSTTY_VT=<path>` overrides the lookup with any build.

## Rebuilding

zig 0.15.2 (https://ziglang.org/download/), herdr's flags:

```sh
git clone --depth 1 --branch v0.8.2 https://github.com/herdrdev/herdr
cd herdr/vendor/libghostty-vt
for target in aarch64-macos x86_64-macos x86_64-linux-gnu.2.28 aarch64-linux-gnu.2.28; do
  zig build -Demit-lib-vt -Doptimize=ReleaseFast -Dsimd=true -Dstrip=true -Demit-xcframework=false \
      "-Dtarget=$target" "-Dversion-string=$(cat VERSION)" --prefix "out/$target"
done
```

On macOS 26 zig 0.15.2 cannot read the current SDK's libSystem stubs: put an `xcrun`
shim first on `PATH` that answers `--show-sdk-path` with
`/Library/Developer/CommandLineTools/SDKs/MacOSX15.4.sdk` (and set `SDKROOT` to the same),
passing every other call through to `/usr/bin/xcrun`. The Linux builds are cross-compiled
from macOS; zig ships the glibc stubs it needs.
