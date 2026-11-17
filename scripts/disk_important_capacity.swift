// Print the bytes macOS reports as available "for important usage" on the
// volume holding the given path: free space plus what the system can purge on
// demand (local snapshots, caches). `df` and statfs omit the purgeable part.
// Exit 1 when the value is unavailable (e.g. a non-APFS or network volume).
import Foundation

let path = CommandLine.arguments.count > 1 ? CommandLine.arguments[1] : "/"
let url = URL(fileURLWithPath: path)
if let values = try? url.resourceValues(forKeys: [.volumeAvailableCapacityForImportantUsageKey]),
   let bytes = values.volumeAvailableCapacityForImportantUsage {
    print(bytes)
} else {
    exit(1)
}
