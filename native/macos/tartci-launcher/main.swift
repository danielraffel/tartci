import Darwin
import Foundation
import Security

private let usage = "usage: tartci-launcher --lane <signed-lane>\n       tartci-launcher --probe-store"
private let m3Store = "/Volumes/Workshop/VMs"
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
private func probeStore() -> Never {
    let root = URL(fileURLWithPath: m3Store, isDirectory: true)
    let target = root.appendingPathComponent(
        ".tartci-launcher-probe-\(getpid())-\(UUID().uuidString)"
    )
    let payload = Data("tartci-launcher-store-probe\n".utf8)
    defer { try? FileManager.default.removeItem(at: target) }
    do {
        try payload.write(to: target, options: [.atomic])
        guard try Data(contentsOf: target) == payload else {
            fail("M3 store probe readback mismatch", code: 74)
        }
        try FileManager.default.removeItem(at: target)
        exit(0)
    } catch {
        fail("M3 store probe failed: \(error)", code: 74)
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
if arguments == ["--probe-store"] {
    probeStore()
}
guard arguments.count == 2, arguments[0] == "--lane",
      arguments[1].range(of: "^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$", options: .regularExpression) != nil else { fail(usage) }
let config = loadConfiguration(root)
guard let lane = config.lanes[arguments[1]] else { fail("lane is not in the signed fleet enum", code: 77) }
guard lane.environment["TART_HOME"] == m3Store else { fail("signed lane has the wrong M3 Tart store", code: 77) }
guard lane.environment.keys.allSatisfy({ !$0.hasPrefix("DYLD_") }) else {
    fail("signed lane contains a forbidden dynamic-loader variable", code: 77)
}
let launch = root.appendingPathComponent("Contents/Resources/support/.tartci-launch").path
guard access(launch, R_OK | X_OK) == 0 else { fail("sealed TartCI entrypoint is unavailable", code: 77) }
for number in [SIGTERM, SIGINT, SIGHUP] { signal(number, signalHandler) }
let child = spawn(launch, ["serve", "macos", "--loop"], lane.environment)
var status: Int32 = 0
while true {
    let result = waitpid(child, &status, WNOHANG)
    if result == child { finish(child, status) }
    if result == -1 && errno != EINTR { fail("waitpid failed", code: 70) }
    if caughtSignal != 0 { finish(child, nil) }
    usleep(50_000)
}
