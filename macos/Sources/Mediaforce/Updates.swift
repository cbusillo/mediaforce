import AppKit
import Sparkle

/// Mediaforce's updates. Sparkle finds, downloads, checks and installs a new app with its standard windows. Before
/// Sparkle may offer a build, the bundled server gets ready for it: new work waits, running files finish, and the old
/// app and database are saved. Only that exact prepared build may proceed, and a build that failed is never offered
/// again. Each launch lets the server finish whatever an interrupted update left.
@MainActor
final class Updates: NSObject, SPUUpdaterDelegate {
    static let shared = Updates()

    private var controller: SPUStandardUpdaterController?
    private var state = ServerUpdateStep.Answer()
    private var preparing: Task<Void, Never>?

    /// Release builds name a feed; source builds have none and never check.
    static var isConfigured: Bool {
        !(Bundle.main.object(forInfoDictionaryKey: "SUFeedURL") as? String ?? "").isEmpty
    }

    func start() async {
        guard Self.isConfigured, controller == nil else { return }
        state = await ServerUpdateStep.run(["reconcile"])
        if let intent = state.intent {
            switch intent.phase {
            case "fenced": prepare(intent.to)
            case "failed": offerRollback(intent.to)
            default: break
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
            if state.failedBuilds.contains(build) { throw UpdateWait(Words.updateFailedBefore(build: item.displayVersionString)) }
            if let intent = state.intent, intent.to == build, intent.phase == "prepared" { return }
            if preparing == nil, state.intent == nil { prepare(build) }
            throw UpdateWait(state.error ?? state.intent?.waiting.flatMap { $0.isEmpty ? nil : Words.updateWaiting($0) } ?? Words.updateGettingReady)
        }
    }

    nonisolated func updater(
        _ updater: SPUUpdater, userDidMake choice: SPUUserUpdateChoice, forUpdate item: SUAppcastItem, state: SPUUserUpdateState
    ) {
        // Nothing downloaded, so nothing can install on quit: the unchanged old server may resume.
        guard choice != .install, state.stage == .notDownloaded else { return }
        Task { @MainActor in
            self.preparing?.cancel()
            self.state = await ServerUpdateStep.run(["cancel"])
        }
    }

    /// Runs the server's preparation until it is done, rechecking every minute while files are still running.
    private func prepare(_ build: String) {
        preparing = Task {
            defer { preparing = nil }
            while !Task.isCancelled {
                state = await ServerUpdateStep.run(["prepare", "--build", build])
                guard state.error == nil, let intent = state.intent else { return }
                if intent.phase == "prepared" {
                    controller?.updater.checkForUpdatesInBackground()
                    return
                }
                try? await Task.sleep(for: .seconds(60))
            }
        }
    }

    /// A new version that failed its checks stays stopped; going back is the Director's call, offered here.
    private func offerRollback(_ build: String) {
        let alert = NSAlert()
        alert.messageText = Words.updateFailedTitle
        alert.informativeText = Words.updateFailedBefore(build: build)
        alert.addButton(withTitle: Words.updateGoBack)
        alert.addButton(withTitle: Words.updateNotNow)
        guard alert.runModal() == .alertFirstButtonReturn else { return }
        Task { state = await ServerUpdateStep.run(["rollback"]) }
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
        let to: String
        let waiting: [String]?
    }

    struct Answer: Decodable {
        var error: String?
        var intent: Intent?
        var failedBuilds: [String] = []

        enum CodingKeys: String, CodingKey { case error, intent, failedBuilds = "failed_builds" }
    }

    static func run(_ step: [String]) async -> Answer {
        guard let config = BackgroundService.savedConfig else { return Answer(error: Words.updateServerUnavailable) }
        let server = Bundle.main.bundleURL.appending(path: "Contents/MacOS/mediaforce-server")
        return await Task.detached {
            let process = Process()
            let pipe = Pipe()
            process.executableURL = server
            process.arguments = ["--config", config, "service", "update"] + step
            process.standardOutput = pipe
            guard (try? process.run()) != nil else { return Answer(error: Words.updateServerUnavailable) }
            let data = pipe.fileHandleForReading.readDataToEndOfFile()
            process.waitUntilExit()
            let line = String(decoding: data, as: UTF8.self).split(separator: "\n").last.map(String.init) ?? ""
            return (try? JSONDecoder().decode(Answer.self, from: Data(line.utf8))) ?? Answer(error: Words.updateServerUnavailable)
        }.value
    }
}
