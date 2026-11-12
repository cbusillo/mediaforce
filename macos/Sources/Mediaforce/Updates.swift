import Foundation
import Sparkle

/// Mediaforce's updates. Sparkle finds, downloads, checks and installs a new app with its standard windows. Before
/// Sparkle may offer a build, the bundled server gets ready for it: new work waits, running files finish, and the old
/// app and database are saved. Only that exact prepared build may proceed, and a build that failed is never offered
/// again. After Sparkle relaunches the new app, it starts and checks its own server before work resumes.
@MainActor
final class Updates: NSObject, SPUUpdaterDelegate {
    static let shared = Updates()

    private var controller: SPUStandardUpdaterController?
    /// The build whose old app and database are saved, so Sparkle may install it.
    private var preparedBuild: String?
    private var failedBuilds: Set<String> = []
    private var preparing: Task<Void, Never>?
    /// Why the offered update is waiting, in plain words; empty when nothing waits.
    private(set) var waiting: [String] = []

    /// Release builds name a feed; source builds have none and never check.
    static var isConfigured: Bool {
        !(Bundle.main.object(forInfoDictionaryKey: "SUFeedURL") as? String ?? "").isEmpty
    }

    var canCheck: Bool { controller?.updater.canCheckForUpdates ?? false }

    /// Finishes an update this app was installed by, then lets Sparkle check on its usual schedule.
    func start() async {
        guard Self.isConfigured, controller == nil else { return }
        if let status = await ServerUpdateStep.run(["status"]) {
            failedBuilds = Set(status.failedBuilds)
            if let intent = status.intent, let target = intent.to {
                if target == ServerUpdateStep.ownBuild, ["prepared", "starting"].contains(intent.phase) {
                    if let problem = await ServerUpdateStep.run(["finish"]).flatMap(\.error) {
                        waiting = [problem]
                    }
                } else if intent.phase == "prepared" {
                    preparedBuild = target
                }
            }
        }
        controller = SPUStandardUpdaterController(startingUpdater: true, updaterDelegate: self, userDriverDelegate: nil)
    }

    func checkForUpdates() {
        controller?.checkForUpdates(nil)
    }

    // Sparkle calls this before it shows, downloads or installs anything; a thrown error stops all three.
    nonisolated func updater(_ updater: SPUUpdater, shouldProceedWithUpdate item: SUAppcastItem, updateCheck: SPUUpdateCheck) throws {
        let build = item.versionString
        try MainActor.assumeIsolated {
            if failedBuilds.contains(build) {
                throw UpdateWait(Words.updateFailedBefore(build: item.displayVersionString))
            }
            if preparedBuild == build { return }
            if preparedBuild == nil, preparing == nil { prepare(build) }
            throw UpdateWait(waiting.isEmpty ? Words.updateGettingReady : Words.updateWaiting(waiting))
        }
    }

    nonisolated func updater(
        _ updater: SPUUpdater, userDidMake choice: SPUUserUpdateChoice, forUpdate item: SUAppcastItem, state: SPUUserUpdateState
    ) {
        // Nothing is downloaded, so nothing can install on quit: the unchanged old server may resume.
        guard choice != .install, state.stage == .notDownloaded else { return }
        Task { @MainActor in await self.cancel() }
    }

    /// Runs the server's preparation until it is done, rechecking every minute while files are still running.
    private func prepare(_ build: String) {
        preparing = Task {
            defer { preparing = nil }
            while !Task.isCancelled {
                guard let answer = await ServerUpdateStep.run(["prepare", "--build", build]) else {
                    waiting = [Words.updateServerUnavailable]
                    return
                }
                if let problem = answer.error {
                    waiting = [problem]
                    return
                }
                waiting = answer.intent?.waiting ?? []
                if answer.intent?.phase == "prepared" {
                    preparedBuild = build
                    controller?.updater.checkForUpdatesInBackground()
                    return
                }
                try? await Task.sleep(for: .seconds(60))
            }
        }
    }

    private func cancel() async {
        guard preparedBuild != nil || preparing != nil else { return }
        preparing?.cancel()
        if let problem = await ServerUpdateStep.run(["cancel"]).flatMap(\.error) {
            waiting = [problem]
            return
        }
        preparedBuild = nil
        waiting = []
    }
}

struct UpdateWait: LocalizedError {
    let errorDescription: String?
    init(_ description: String) { errorDescription = description }
}

/// One step of the bundled server's update transaction, run from this app's own server executable.
enum ServerUpdateStep {
    struct Intent: Decodable {
        let phase: String
        let to: String?
        let waiting: [String]?
    }

    struct Answer: Decodable {
        let error: String?
        let intent: Intent?
        let failedBuilds: [String]

        private enum Keys: String, CodingKey { case error, result }
        private enum ResultKeys: String, CodingKey { case intent, failedBuilds = "failed_builds", phase }

        init(from decoder: Decoder) throws {
            let outer = try decoder.container(keyedBy: Keys.self)
            error = try outer.decodeIfPresent(String.self, forKey: .error)
            guard let result = try? outer.nestedContainer(keyedBy: ResultKeys.self, forKey: .result) else {
                intent = nil
                failedBuilds = []
                return
            }
            failedBuilds = try result.decodeIfPresent([String].self, forKey: .failedBuilds) ?? []
            // `status` nests the intent; `prepare` and `finish` answer with the intent itself.
            intent = result.contains(.phase)
                ? try outer.decode(Intent.self, forKey: .result)
                : try result.decodeIfPresent(Intent.self, forKey: .intent)
        }
    }

    static var ownBuild: String { Bundle.main.object(forInfoDictionaryKey: "CFBundleVersion") as? String ?? "" }

    /// The saved service's config, so each step addresses the same database the server uses.
    static var savedConfig: String? {
        guard let data = try? Data(contentsOf: BackgroundService.plist),
              let item = try? PropertyListSerialization.propertyList(from: data, format: nil) as? [String: Any],
              let arguments = item["ProgramArguments"] as? [String],
              let index = arguments.firstIndex(of: "--config"), index + 1 < arguments.count
        else { return nil }
        return arguments[index + 1]
    }

    static func run(_ step: [String]) async -> Answer? {
        guard let config = savedConfig else { return nil }
        let server = Bundle.main.bundleURL.appending(path: "Contents/MacOS/mediaforce-server")
        return await Task.detached {
            let process = Process()
            let pipe = Pipe()
            process.executableURL = server
            process.arguments = ["--config", config, "service", "update"] + step
            process.standardOutput = pipe
            do {
                try process.run()
            } catch {
                return nil
            }
            let data = pipe.fileHandleForReading.readDataToEndOfFile()
            process.waitUntilExit()
            let line = String(decoding: data, as: UTF8.self).split(separator: "\n").last.map(String.init) ?? ""
            return try? JSONDecoder().decode(Answer.self, from: Data(line.utf8))
        }.value
    }
}
