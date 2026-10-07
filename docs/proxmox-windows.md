# Proxmox Windows build lane

This is the x64 Windows lane on `macpro` (Proxmox). It is a non-blocking
headless validation lane: it must never replace or reserve the Apple Silicon
macOS runners. The interactive user path remains the ARM64 QEMU/UTM golden on
M5/M5S.

## VM contract

The current host uses VMID `300` (`pulp-win-ci`) with a `toolchain-vs2022`
snapshot, 4 vCPUs, 10 GiB RAM, VirtIO storage, and the QEMU guest agent. Keep
the VM stopped when it is not serving a diagnostic build. Start it with:

```bash
ssh macpro 'qm start 300'
ssh macpro 'qm guest cmd 300 ping'
```

The guest agent is the control channel. Do not depend on a mutable guest SSH
password or copy a macOS checkout into the VM. A macOS copy can carry
`._*` AppleDouble files and invalid Git pack indexes, which caused the original
Proxmox checkout to report thousands of modified files and fail closed.

## Clean checkout and build

Materialize a native Windows checkout directly from GitHub. Use a fresh path for
each proof or reset a checkout only after `git fsck` succeeds:

```text
git clone --branch <ref> --depth 1 https://github.com/Generous-Corp/pulp.git C:\src\pulp-win-proof
```

Inside the guest, configure through the VS 2022 Build Tools environment and keep
parallelism bounded. For the GPU path, Pulp selects the immutable Windows x64
Skia archive from its dependency manifest; `-DPULP_ENABLE_GPU=ON` must not be
silently replaced with a CPU fallback.

```text
call "C:\Program Files (x86)\Microsoft Visual Studio\2022\BuildTools\Common7\Tools\VsDevCmd.bat" -arch=amd64 -host_arch=amd64
cd /d C:\src\pulp-win-proof
"C:\\Program Files\\Git\\bin\\bash.exe" -c "./setup.sh --ci --deps-only"
cmake -S . -B build -DCMAKE_BUILD_TYPE=Release -DPULP_BUILD_EXAMPLES=OFF -DPULP_ENABLE_GPU=ON -DFETCHCONTENT_BASE_DIR=C:/fc
cmake --build build --config Release --parallel 3
```

The default full CTest inventory is not a Windows acceptance claim. Use the
Windows CI selection and preserve the complete log:

```text
ctest --test-dir build -C Release --parallel 3 -LE "validation|slow" --output-on-failure > C:\tmp\pulp-ctest.log 2>&1
```

The command must return zero before a lane can report green. A partial run,
registration-only test, or a skipped test is evidence of an incomplete lane.

## Plugin proof

For a checkout that produces the sample plugin, copy both formats into the
standard Windows locations and scan them with the built worker:

```text
mkdir "C:\Program Files\Common Files\VST3"
mkdir "C:\Program Files\Common Files\CLAP"
robocopy "build\VST3\Sample Region Allpass.vst3" "C:\Program Files\Common Files\VST3\Sample Region Allpass.vst3" /E
copy "build\CLAP\Release\Sample Region Allpass.clap" "C:\Program Files\Common Files\CLAP\Sample Region Allpass.clap"
build\tools\scan-worker\Release\pulp-scan-worker.exe "C:\Program Files\Common Files\CLAP\Sample Region Allpass.clap"
build\tools\scan-worker\Release\pulp-scan-worker.exe "C:\Program Files\Common Files\VST3\Sample Region Allpass.vst3\Contents\x86_64-win\Sample Region Allpass.vst3"
```

Record the JSON scan output, file sizes, SHA-256 values, and the exact source
SHA. Discovery by the scan worker proves the binaries can be loaded and
identified; it does not prove DAW audio or UI behavior. REAPER/Ableton testing
belongs on the persistent UTM bench where a headed Windows desktop is available.

## Failure handling

- A clean compile with CTest failures is `BUILD_PASS_TEST_FAIL`, never green.
- Missing Skia, a failed SHA check, or a CPU fallback while GPU is requested is
  `GPU_PROOF_FAIL`.
- Missing plugin scan output is `PLUGIN_PROOF_FAIL` when a plugin target was
  requested.
- Keep the original logs under a named proof directory. Do not overwrite the
  golden or the UTM bench disk. Nightly automation should use a disposable
  overlay/clone and power the VM down after the receipt is written.
