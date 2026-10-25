import Foundation

/// Mediaforce's background service on this Mac: the `com.mediaforce.web` login item that `mediaforce service install`
/// writes. The app starts or restarts it through launchctl, as the service command does, and never installs it.
enum BackgroundService {
    static let label = "com.mediaforce.web"

    static var plist: URL {
        FileManager.default.homeDirectoryForCurrentUser.appending(path: "Library/LaunchAgents/\(label).plist")
    }

    /// Whether this Mac was ever set up to run Mediaforce in the background.
    static var isInstalled: Bool {
        FileManager.default.fileExists(atPath: plist.path)
    }

    private static var domain: String { "gui/\(getuid())" }

    /// Starts the service: loads it when it isn't loaded, otherwise asks launchd to run it now.
    /// Returns what launchctl said when it refused, or nil when it accepted.
    static func start() async -> String? {
        if await launchctl(["print", "\(domain)/\(label)"]).status != 0 {
            let loaded = await launchctl(["bootstrap", domain, plist.path])
            if loaded.status != 0 { return loaded.output }
        }
        let started = await launchctl(["kickstart", "\(domain)/\(label)"])
        return started.status == 0 ? nil : started.output
    }

    /// Stops the running service and starts it again. Returns what launchctl said when it refused.
    static func restart() async -> String? {
        let restarted = await launchctl(["kickstart", "-k", "\(domain)/\(label)"])
        return restarted.status == 0 ? nil : restarted.output
    }

    /// Runs launchctl off the main thread. The output is read to its end before waiting, so a long answer can't fill
    /// the pipe and stall it.
    private static func launchctl(_ arguments: [String]) async -> (status: Int32, output: String) {
        await Task.detached {
            let process = Process()
            let pipe = Pipe()
            process.executableURL = URL(fileURLWithPath: "/bin/launchctl")
            process.arguments = arguments
            process.standardOutput = pipe
            process.standardError = pipe
            do {
                try process.run()
            } catch {
                return (-1, error.localizedDescription)
            }
            let data = pipe.fileHandleForReading.readDataToEndOfFile()
            process.waitUntilExit()
            let output = String(decoding: data, as: UTF8.self).trimmingCharacters(in: .whitespacesAndNewlines)
            return (process.terminationStatus, output)
        }.value
    }
}
