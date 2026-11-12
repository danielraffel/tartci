import CryptoKit
import Darwin
import Foundation
import Security

private let usage = "usage: tartci-launcher --lane <signed-lane>\n       tartci-launcher --probe-store"
private var caughtSignal: Int32 = 0
#if TARTCI_TESTING
private let gracefulShutdownSeconds = 0.2, forcedShutdownSeconds = 1.0
#else
private let gracefulShutdownSeconds = 20.0, forcedShutdownSeconds = 5.0
#endif
struct Lane: Decodable { let environment: [String: String] }
struct SealedConfiguration: Decodable { let schema: Int; let lanes: [String: Lane] }
private func signalHandler(_ number: Int32) { caughtSignal = number }
private func fail(_ message: String, code: Int32 = 64) -> Never {
    FileHandle.standardError.write(Data(("tartci-launcher: \(message)\n").utf8)); exit(code)
}
private func appRoot() -> URL {
    URL(fileURLWithPath: CommandLine.arguments[0]).standardizedFileURL
        .deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent()
}
private func verifySealedBundle(_ root: URL) {
#if TARTCI_TESTING
    return
#else
    var code: SecStaticCode?
    let created = SecStaticCodeCreateWithPath(root as CFURL, SecCSFlags(), &code)
    guard created == errSecSuccess, let code else { fail("cannot inspect bundle signature", code: 77) }
    let flags = SecCSFlags(rawValue: kSecCSStrictValidate | kSecCSCheckAllArchitectures)
    guard SecStaticCodeCheckValidity(code, flags, nil) == errSecSuccess else {
        fail("bundle signature or sealed resources are invalid", code: 77)
    }
#endif
}
private func loadConfiguration(_ root: URL) -> SealedConfiguration {
    do {
        let url = root.appendingPathComponent("Contents/Resources/lanes.json")
        let value = try JSONDecoder().decode(SealedConfiguration.self, from: Data(contentsOf: url))
        guard value.schema == 1, !value.lanes.isEmpty else { fail("sealed lane configuration is invalid", code: 77) }
        return value
    } catch { fail("cannot read sealed lane configuration: \(error)", code: 77) }
}
private func spawn(_ executable: String, _ arguments: [String], _ environment: [String: String]) -> pid_t {
    var attr: posix_spawnattr_t?
    guard posix_spawnattr_init(&attr) == 0 else { fail("cannot initialize spawn", code: 70) }
    defer { posix_spawnattr_destroy(&attr) }
    guard posix_spawnattr_setflags(&attr, Int16(POSIX_SPAWN_SETPGROUP)) == 0,
          posix_spawnattr_setpgroup(&attr, 0) == 0 else { fail("cannot create owned process group", code: 70) }
    let args = ([executable] + arguments).map { strdup($0)! }
    let env = environment.sorted { $0.key < $1.key }.map { strdup("\($0.key)=\($0.value)")! }
    defer { args.forEach { free($0) }; env.forEach { free($0) } }
    var argv = args.map { UnsafeMutablePointer<CChar>?($0) } + [nil]
    var envp = env.map { UnsafeMutablePointer<CChar>?($0) } + [nil]
    var pid: pid_t = 0
    let result = argv.withUnsafeMutableBufferPointer { av in
        envp.withUnsafeMutableBufferPointer { ep in posix_spawn(&pid, executable, nil, &attr, av.baseAddress!, ep.baseAddress!) }
    }
    guard result == 0 else { fail("cannot launch sealed TartCI cohort: \(String(cString: strerror(result)))", code: 126) }
    return pid
}
struct SealedMetadata: Decodable { let tart_home: String }
/// The Tart store sealed into this bundle at build time (bundle.json): the
/// profile's own external-volume store, e.g. /Volumes/<data-volume>/VMs on m3 or
/// /Volumes/<data-volume>/VMs on m5studio. It is covered by the bundle signature.
private func sealedStore(_ root: URL) -> String {
    let value: SealedMetadata
    do {
        let url = root.appendingPathComponent("Contents/Resources/bundle.json")
        value = try JSONDecoder().decode(SealedMetadata.self, from: Data(contentsOf: url))
    } catch { fail("cannot read sealed bundle metadata: \(error)", code: 77) }
    guard isExternalStore(value.tart_home) else {
        fail("sealed Tart store is not an external-volume path", code: 77)
    }
    return value.tart_home
}
private func isExternalStore(_ path: String) -> Bool {
    let parts = path.split(separator: "/", omittingEmptySubsequences: false).dropFirst()
    return path.hasPrefix("/Volumes/") && parts.count >= 3
        && parts.allSatisfy({ !$0.isEmpty && $0 != "." && $0 != ".." })
}

// MARK: launcher v2: a stable app that runs a signed launch set from a generation
//
// policy.json (schema 2, inside the signed app) pins the team, the Tart store,
// the generations root and the pointer. The app itself carries no support tree,
// so its bytes, and its Removable Volumes consent, survive ordinary updates.
// What a lane runs is the generation the pointer names, accepted only when:
//   - the launch set's detached signature satisfies a requirement built from
//     policy.team_id (never from anything inside the generation);
//   - the generation, every member and every directory up to the generations
//     root is owned by this user and not group- or world-writable, with no
//     symlink anywhere;
//   - the manifest, every member and the entrypoint match their digests, and
//     the generation holds no other file: no __pycache__, no .pyc, nothing
//     the manifest does not name, so nothing unhashed can be imported;
//   - the pointer and its directories up to policy.home are trusted the same way;
//   - the set's sequence is not below the last one this host ran, except the
//     one before it (a rollback).
// Any failure exits 77 and runs nothing.
struct Policy: Decodable {
    let schema: Int; let identifier: String; let team_id: String
    let tart_home: String; let generations_root: String
    let home: String; let pointer: String; let floor: String
}
struct Pointer: Decodable { let schema: Int; let generation: String }
struct Floor: Codable { let schema: Int; let sequence: Int; let previous_sequence: Int? }
struct Entrypoint: Decodable { let path: String; let sha256: String }
struct PythonPin: Decodable { let pin: String; let shim: Entrypoint }
struct LaunchSet: Decodable {
    let schema: Int; let sequence: Int; let tart_home: String; let manifest_sha256: String
    let entrypoint: Entrypoint; let python: PythonPin; let lanes: [String: Lane]
    /// Generation files that are not manifest members but are pinned by digest:
    /// the v1 `.tartci-launch` stage-install writes into every generation.
    let files: [ManifestMember]?
}
// v2's own entrypoint and interpreter shim (scripts/launch_set.py builds the
// same bytes). Lane Python runs only through the shim: the pinned
// interpreter with -B -E -s -S, so no site-packages .pth or sitecustomize,
// user site or PYTHON* variable runs; -I is not used because its -P drops the
// script directory sibling imports need.
private let entrypointName = ".tartci-launch-v2"
private let shimName = "bin/python3"
private let shimFlags = "-B -E -s -S"
private func isSafePath(_ path: String) -> Bool {
    path.range(of: "^/[A-Za-z0-9._+@/-]+$", options: .regularExpression) != nil
}
private func shimBytes(_ pin: String) -> Data { Data("#!/bin/sh\nexec \(pin) \(shimFlags) \"$@\"\n".utf8) }
private func entrypointBytes(_ generation: String) -> Data {
    Data(("#!/bin/bash\n"
        + "\(generation)/\(shimName) \(generation)/scripts/tartci_support_manifest.py verify "
        + "\(generation)/.tartci-support-manifest.json --root \(generation) --immutable >/dev/null || exit $?\n"
        + "exec /bin/bash \(generation)/tartci \"$@\"\n").utf8)
}
/// The first python3 on PATH, or nil.
private func firstPython(onPath path: String) -> String? {
    for directory in path.split(separator: ":", omittingEmptySubsequences: false) {
        let candidate = String(directory) + "/python3"
        if !directory.isEmpty, access(candidate, X_OK) == 0 { return candidate }
    }
    return nil
}
struct ManifestMember: Decodable { let path: String; let sha256: String }
struct SupportManifest: Decodable { let members: [ManifestMember] }
private let launchSetIdentifier = "com.danielraffel.tartci.launch-set"

private func loadPolicy(_ root: URL) -> Policy? {
    let url = root.appendingPathComponent("Contents/Resources/policy.json")
    guard FileManager.default.fileExists(atPath: url.path) else { return nil }
    let policy: Policy
    do { policy = try JSONDecoder().decode(Policy.self, from: Data(contentsOf: url)) }
    catch { fail("cannot read launcher policy: \(error)", code: 77) }
    guard policy.schema == 2,
          policy.team_id.range(of: "^[A-Z0-9]{10}$", options: .regularExpression) != nil,
          isExternalStore(policy.tart_home),
          policy.generations_root.hasPrefix("/"), !policy.generations_root.hasSuffix("/"),
          policy.home.hasPrefix("/"), !policy.home.hasSuffix("/"),
          policy.pointer.hasPrefix(policy.home + "/"),
          policy.floor.hasPrefix(policy.home + "/") else { fail("launcher policy is invalid", code: 77) }
    return policy
}
private func sha256Hex(_ data: Data) -> String {
    SHA256.hash(data: data).map { String(format: "%02x", $0) }.joined()
}
/// Not a symlink, owned by this user, not group- or world-writable.
private func requireTrusted(_ path: String) {
    var info = stat()
    guard lstat(path, &info) == 0 else { fail("cannot inspect \(path)", code: 77) }
    guard (info.st_mode & S_IFMT) != S_IFLNK else { fail("\(path) is a symlink", code: 77) }
    guard info.st_uid == getuid() else { fail("\(path) is not owned by this user", code: 77) }
    guard info.st_mode & 0o022 == 0 else { fail("\(path) is group- or world-writable", code: 77) }
}
/// `path` and every directory from it up to and including `top`.
private func requireTrustedChain(_ path: String, upTo top: String) {
    guard path == top || path.hasPrefix(top + "/") else { fail("\(path) is outside \(top)", code: 77) }
    var current = path
    while true {
        requireTrusted(current)
        if current == top { return }
        current = (current as NSString).deletingLastPathComponent
    }
}
/// The bytes of a trusted regular file, read through one descriptor.
private func readTrusted(_ path: String) -> Data {
    let fd = open(path, O_RDONLY | O_NOFOLLOW | O_CLOEXEC)
    guard fd >= 0 else { fail("cannot open \(path): \(String(cString: strerror(errno)))", code: 77) }
    let handle = FileHandle(fileDescriptor: fd, closeOnDealloc: true)
    var info = stat()
    guard fstat(fd, &info) == 0, (info.st_mode & S_IFMT) == S_IFREG, info.st_uid == getuid(),
          info.st_mode & 0o022 == 0 else { fail("\(path) is not a trusted regular file", code: 77) }
    return handle.readDataToEndOfFile()
}
private func requireSafeRelative(_ relative: String) {
    let parts = relative.split(separator: "/", omittingEmptySubsequences: false)
    guard !relative.hasPrefix("/"), !parts.isEmpty,
          parts.allSatisfy({ !$0.isEmpty && $0 != "." && $0 != ".." }) else {
        fail("unsafe member path \(relative)", code: 77)
    }
}
private func launchSetRequirement(_ policy: Policy) -> String {
#if TARTCI_TESTING
    if let override = ProcessInfo.processInfo.environment["TARTCI_TEST_LAUNCH_SET_REQUIREMENT"] {
        return override
    }
#endif
    // Developer ID only: the intermediate's Developer ID CA marker and the
    // leaf's Developer ID Application marker, so the team's Apple Development
    // and other Apple-issued certificates are refused.
    return "anchor apple generic"
        + " and certificate 1[field.1.2.840.113635.100.6.2.6] exists"
        + " and certificate leaf[field.1.2.840.113635.100.6.1.13] exists"
        + " and certificate leaf[subject.OU] = \"\(policy.team_id)\""
        + " and identifier \"\(launchSetIdentifier)\""
}
/// Verify the detached signature over exactly the bytes that will be used:
/// they and the signature are copied into a private directory first, so
/// neither can change between verification and use. The check is
/// /usr/bin/codesign (SIP-protected): Security.framework has no public API
/// for a detached signature.
private func verifyLaunchSet(_ data: Data, signature: Data, _ policy: Policy) {
    var template = Array((NSTemporaryDirectory() + "tartci-launch-set.XXXXXX").utf8CString)
    guard let made = mkdtemp(&template) else { fail("cannot create a private directory", code: 70) }
    let directory = String(cString: made)
    defer { try? FileManager.default.removeItem(atPath: directory) }
    let copy = URL(fileURLWithPath: directory).appendingPathComponent("launch-set.json")
    let sig = URL(fileURLWithPath: directory).appendingPathComponent("launch-set.sig")
    do { try data.write(to: copy); try signature.write(to: sig) }
    catch { fail("cannot stage the launch set: \(error)", code: 70) }
    let check = Process()
    check.executableURL = URL(fileURLWithPath: "/usr/bin/codesign")
    check.arguments = ["--verify", "--strict", "--detached", sig.path,
                       "-R=" + launchSetRequirement(policy), copy.path]
    check.environment = ["PATH": "/usr/bin:/bin"]
    check.standardOutput = FileHandle.nullDevice
    check.standardError = FileHandle.nullDevice
    do { try check.run() } catch { fail("cannot run codesign: \(error)", code: 77) }
    check.waitUntilExit()
    guard check.terminationReason == .exit, check.terminationStatus == 0 else {
        fail("launch set signature is invalid or not from the pinned team", code: 77)
    }
}
private func requireMember(_ generation: String, _ relative: String, _ expected: String) -> String {
    requireSafeRelative(relative)
    let path = generation + "/" + relative
    requireTrustedChain(path, upTo: generation)
    guard sha256Hex(readTrusted(path)) == expected else { fail("\(relative) does not match its digest", code: 77) }
    return path
}
/// Every file in the generation is a manifest member or one of the launch
/// set's own files; there is no __pycache__, symlink or special file.
private func requireNothingUnhashed(_ generation: String, allowed: Set<String>) {
    guard let walker = FileManager.default.enumerator(atPath: generation) else {
        fail("cannot list \(generation)", code: 77)
    }
    for case let relative as String in walker {
        let path = generation + "/" + relative
        var info = stat()
        guard lstat(path, &info) == 0 else { fail("cannot inspect \(path)", code: 77) }
        let kind = info.st_mode & S_IFMT
        if (relative as NSString).lastPathComponent == "__pycache__" {
            fail("\(relative): a bytecode cache in the generation is never hashed", code: 77)
        }
        if kind == S_IFDIR { continue }
        guard kind == S_IFREG else { fail("\(relative) is not a regular file", code: 77) }
        guard allowed.contains(relative) else { fail("\(relative) is not in the signed manifest", code: 77) }
    }
}
/// The last sequence this host ran, and the one before it (a rollback).
/// A missing floor fails open: the first run on a host has nothing to compare
/// with, and records the floor it runs. A floor that exists but cannot be read
/// or decoded fails closed.
private func requireNotDowngraded(_ policy: Policy, _ sequence: Int) {
    guard sequence >= 1 else { fail("launch set has no sequence", code: 77) }
    guard access(policy.floor, F_OK) == 0 else { return }
    requireTrustedChain(policy.floor, upTo: policy.home)
    let floor: Floor
    do { floor = try JSONDecoder().decode(Floor.self, from: readTrusted(policy.floor)) }
    catch { fail("cannot read the launcher floor: \(error)", code: 77) }
    guard floor.schema == 1 else { fail("launcher floor is invalid", code: 77) }
    guard sequence >= floor.sequence || sequence == floor.previous_sequence else {
        fail("launch set \(sequence) is older than the last one run (\(floor.sequence))", code: 77)
    }
}
private func recordFloor(_ policy: Policy, _ sequence: Int) {
    var current: Floor? = nil
    if access(policy.floor, F_OK) == 0 {
        current = try? JSONDecoder().decode(Floor.self, from: readTrusted(policy.floor))
    }
    if let current, sequence <= current.sequence { return }
    let value = Floor(schema: 1, sequence: sequence, previous_sequence: current?.sequence)
    guard let data = try? JSONEncoder().encode(value) else { fail("cannot encode the launcher floor", code: 70) }
    let temporary = policy.floor + ".\(getpid()).tmp"
    let fd = open(temporary, O_WRONLY | O_CREAT | O_EXCL | O_NOFOLLOW | O_CLOEXEC, 0o600)
    guard fd >= 0 else { fail("cannot write the launcher floor", code: 70) }
    let wrote = data.withUnsafeBytes { write(fd, $0.baseAddress, data.count) }
    close(fd)
    guard wrote == data.count, rename(temporary, policy.floor) == 0 else {
        unlink(temporary); fail("cannot write the launcher floor", code: 70)
    }
}
/// The entrypoint and environment for one lane of the pointed-to generation.
private func resolveV2(_ policy: Policy, lane laneID: String) -> (String, String, [String: String]) {
    let root = policy.generations_root
    requireTrusted(root)
    requireTrustedChain(policy.pointer, upTo: policy.home)
    let pointer: Pointer
    do { pointer = try JSONDecoder().decode(Pointer.self, from: readTrusted(policy.pointer)) }
    catch { fail("cannot read the launcher pointer: \(error)", code: 77) }
    let generation = pointer.generation
    let name = generation.hasPrefix(root + "/") ? String(generation.dropFirst(root.count + 1)) : ""
    guard pointer.schema == 1, !name.isEmpty, !name.contains("/"), !name.hasPrefix("."),
          let resolved = realpath(generation, nil) else { fail("pointer names no generation under \(root)", code: 77) }
    defer { free(resolved) }
    guard String(cString: resolved) == generation else { fail("generation path is not canonical", code: 77) }
    requireTrustedChain(generation, upTo: root)
    let setData = readTrusted(generation + "/.tartci-launch-set.json")
    verifyLaunchSet(setData, signature: readTrusted(generation + "/.tartci-launch-set.sig"), policy)
    let set: LaunchSet
    do { set = try JSONDecoder().decode(LaunchSet.self, from: setData) }
    catch { fail("signed launch set is malformed: \(error)", code: 77) }
    guard set.schema == 1, set.tart_home == policy.tart_home else { fail("launch set has the wrong Tart store", code: 77) }
    requireNotDowngraded(policy, set.sequence)
    let manifestData = readTrusted(generation + "/.tartci-support-manifest.json")
    guard sha256Hex(manifestData) == set.manifest_sha256 else { fail("support manifest does not match the launch set", code: 77) }
    let manifest: SupportManifest
    do { manifest = try JSONDecoder().decode(SupportManifest.self, from: manifestData) }
    catch { fail("support manifest is malformed: \(error)", code: 77) }
    for member in manifest.members { _ = requireMember(generation, member.path, member.sha256) }
    guard set.entrypoint.path == entrypointName else { fail("unexpected entrypoint \(set.entrypoint.path)", code: 77) }
    guard isSafePath(generation), sha256Hex(entrypointBytes(generation)) == set.entrypoint.sha256 else {
        fail("entrypoint is not v2's for this generation", code: 77)
    }
    let launch = requireMember(generation, set.entrypoint.path, set.entrypoint.sha256)
    // The shim must exec exactly the signed pin.
    guard set.python.shim.path == shimName, isSafePath(set.python.pin),
          sha256Hex(shimBytes(set.python.pin)) == set.python.shim.sha256 else {
        fail("interpreter shim does not exec the signed pin", code: 77)
    }
    let shim = requireMember(generation, set.python.shim.path, set.python.shim.sha256)
    let pinned = set.files ?? []
    for file in pinned { _ = requireMember(generation, file.path, file.sha256) }
    requireNothingUnhashed(generation, allowed: Set(manifest.members.map { $0.path } + pinned.map { $0.path } + [
        set.entrypoint.path, set.python.shim.path, ".tartci-launch-set.json", ".tartci-launch-set.sig",
        ".tartci-support-manifest.json"]))
    guard let lane = set.lanes[laneID] else { fail("lane is not in the signed launch set", code: 77) }
    guard lane.environment["TART_HOME"] == policy.tart_home else { fail("signed lane has the wrong Tart store", code: 77) }
    guard lane.environment.keys.allSatisfy({ !$0.hasPrefix("DYLD_") }) else {
        fail("signed lane contains a forbidden dynamic-loader variable", code: 77)
    }
    // Python start-up is the launcher's alone: a signed lane may not set
    // PYTHONPATH, PYTHONHOME, PYTHONSTARTUP, PYTHONPYCACHEPREFIX or any other
    // PYTHON* variable (each can run or load code nothing hashed).
    if let python = lane.environment.keys.sorted().first(where: { $0.hasPrefix("PYTHON") }) {
        fail("signed lane sets \(python); Python start-up variables are the launcher's", code: 77)
    }
    for key in ["TARTCI_PYTHON", "SHIPYARD_GHAPP_PYTHON_BINARY"] {
        if let named = lane.environment[key], named != shim {
            fail("signed lane \(key) is \(named), not the generation's interpreter shim", code: 77)
        }
    }
    recordFloor(policy, set.sequence)
    // Every python3 a lane runs by name or by TARTCI_PYTHON is the shim. A
    // call that names /usr/bin/python3 explicitly is not: its site-packages is
    // root-owned, and PYTHONNOUSERSITE keeps its user site out.
    var environment = lane.environment
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    environment["PYTHONNOUSERSITE"] = "1"
    environment["TARTCI_PYTHON"] = shim
    environment["PATH"] = generation + "/bin:" + (lane.environment["PATH"] ?? "/usr/bin:/bin")
    guard let first = firstPython(onPath: environment["PATH"]!), first == shim else {
        fail("the first python3 on the lane's PATH is not the interpreter shim", code: 77)
    }
    return (launch, set.entrypoint.sha256, environment)
}
private func probeStore(_ store: String) -> Never {
    let root = URL(fileURLWithPath: store, isDirectory: true)
    let target = root.appendingPathComponent(
        ".tartci-launcher-probe-\(getpid())-\(UUID().uuidString)"
    )
    let payload = Data("tartci-launcher-store-probe\n".utf8)
    defer { try? FileManager.default.removeItem(at: target) }
    do {
        try payload.write(to: target, options: [.atomic])
        guard try Data(contentsOf: target) == payload else {
            fail("store probe readback mismatch", code: 74)
        }
        try FileManager.default.removeItem(at: target)
        exit(0)
    } catch {
        fail("store probe failed (\(store)): \(error)", code: 74)
    }
}
private func processGroupExists(_ pid: pid_t) -> Bool { kill(-pid, 0) == 0 || errno == EPERM }
private func exitLikeChild(_ status: Int32) -> Never {
    let signal = status & 0x7f
    if signal == 0 { exit((status >> 8) & 0xff) }
    if signal != 0x7f { exit(128 + signal) }
    exit(70)
}
private func finish(_ child: pid_t, _ initial: Int32?) -> Never {
    var saved = initial
    if processGroupExists(child) { _ = kill(-child, SIGTERM) }
    for (seconds, signal) in [(gracefulShutdownSeconds, SIGKILL), (forcedShutdownSeconds, 0)] {
        let deadline = Date().addingTimeInterval(seconds)
        while Date() < deadline {
            if saved == nil {
                var status: Int32 = 0; let result = waitpid(child, &status, WNOHANG)
                if result == child { saved = status }
                if result == -1 && errno != EINTR && errno != ECHILD { fail("waitpid failed", code: 70) }
            }
            if saved != nil && !processGroupExists(child) { exitLikeChild(saved!) }
            usleep(50_000)
        }
        if signal != 0 && processGroupExists(child) { _ = kill(-child, signal) }
    }
    fail("child process group did not exit after SIGKILL", code: 70)
}

let arguments = Array(CommandLine.arguments.dropFirst())
let root = appRoot()
verifySealedBundle(root)
let policy = loadPolicy(root)
let store = policy?.tart_home ?? sealedStore(root)
if arguments == ["--probe-store"] {
    probeStore(store)
}
guard arguments.count == 2, arguments[0] == "--lane",
      arguments[1].range(of: "^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$", options: .regularExpression) != nil else { fail(usage) }
let launch: String
let environment: [String: String]
var launchDigest: String? = nil
if let policy {
    (launch, launchDigest, environment) = resolveV2(policy, lane: arguments[1])
} else {
    let config = loadConfiguration(root)
    guard let lane = config.lanes[arguments[1]] else { fail("lane is not in the signed fleet enum", code: 77) }
    guard lane.environment["TART_HOME"] == store else { fail("signed lane has the wrong Tart store", code: 77) }
    guard lane.environment.keys.allSatisfy({ !$0.hasPrefix("DYLD_") }) else {
        fail("signed lane contains a forbidden dynamic-loader variable", code: 77)
    }
    launch = root.appendingPathComponent("Contents/Resources/support/.tartci-launch").path
    environment = lane.environment
}
guard access(launch, R_OK | X_OK) == 0 else { fail("sealed TartCI entrypoint is unavailable", code: 77) }
for number in [SIGTERM, SIGINT, SIGHUP] { signal(number, signalHandler) }
// macOS has no fexecve, and the entrypoint finds its generation from its own
// path, so it is spawned by path: its digest is checked again immediately
// before the spawn, through a fresh no-follow descriptor.
if let launchDigest, sha256Hex(readTrusted(launch)) != launchDigest {
    fail("entrypoint changed after verification", code: 77)
}
let child = spawn(launch, ["serve", "macos", "--loop"], environment)
var status: Int32 = 0
while true {
    let result = waitpid(child, &status, WNOHANG)
    if result == child { finish(child, status) }
    if result == -1 && errno != EINTR { fail("waitpid failed", code: 70) }
    if caughtSignal != 0 { finish(child, nil) }
    usleep(50_000)
}
