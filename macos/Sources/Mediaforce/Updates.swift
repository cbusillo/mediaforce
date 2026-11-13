import AppKit
import Sparkle
import UserNotifications

/// Mediaforce updates itself between files, with no click (Director decision,
/// https://github.com/cbusillo/mediaforce/issues/1314#issuecomment-6841277256). Sparkle checks on its schedule but may
/// download only a build the bundled server has prepared: at once when nothing runs, otherwise after running files that
/// finish soon. Nothing waits merely because an update exists. Each launch lets the server start and check the new
/// build, or go back to the saved one on its own.
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
        // Recovery runs on every launch, even in a build that no longer names a feed.
        state = await ServerUpdateStep.run(["reconcile"])
        switch state.outcome {
        case "updated": notify(Words.updated)
        case "rolled back": return relaunchRestoredApp()
        default: if let problem = state.error, state.intent != nil { notify(Words.updateNeedsYou(problem)) }
        }
        if let intent = state.intent, intent.phase == "draining" { prepare(intent.to) }
        if state.intent?.phase == "prepared", !Self.isConfigured { state = await ServerUpdateStep.run(["cancel"]) }
        guard Self.isConfigured, controller == nil else { return }
        controller = SPUStandardUpdaterController(startingUpdater: true, updaterDelegate: self, userDriverDelegate: nil)
        // Quit or restarted after preparing: the server is stopped, so fetch the prepared build now, not on schedule.
        if state.intent?.phase == "prepared" { controller?.updater.checkForUpdatesInBackground() }
    }

    // Sparkle calls this before it downloads anything; a thrown error stops the download until the next check.
    nonisolated func updater(_ updater: SPUUpdater, shouldProceedWithUpdate item: SUAppcastItem, updateCheck: SPUUpdateCheck) throws {
        let build = item.versionString
        try MainActor.assumeIsolated {
            if state.failedBuilds.contains(build) { throw UpdateWait(Words.updateFailedBefore(build: item.displayVersionString)) }
            if let intent = state.intent, intent.to == build, intent.phase == "prepared" { return }
            if preparing == nil { prepare(build) }
            throw UpdateWait(Words.updateWaitingForQuietTime)
        }
    }

    // The prepared build is downloaded: install it now and relaunch; the server is already stopped and saved.
    nonisolated func updater(_ updater: SPUUpdater, willInstallUpdateOnQuit item: SUAppcastItem, immediateInstallationBlock: @escaping () -> Void) -> Bool {
        immediateInstallationBlock()
        return true
    }

    // The feed no longer offers the prepared build: the unchanged old server resumes.
    nonisolated func updaterDidNotFindUpdate(_ updater: SPUUpdater) {
        Task { @MainActor in
            if self.state.intent?.phase == "prepared" { self.state = await ServerUpdateStep.run(["cancel"]) }
        }
    }

    /// Asks the server each minute whether now is the time; once it has prepared, Sparkle checks again and installs.
    private func prepare(_ build: String) {
        preparing = Task {
            defer { preparing = nil }
            while !Task.isCancelled {
                state = await ServerUpdateStep.run(["prepare", "--build", build])
                guard state.error == nil else { return }
                if state.intent?.phase == "prepared" {
                    controller?.updater.checkForUpdatesInBackground()
                    return
                }
                try? await Task.sleep(for: .seconds(60))
            }
        }
    }

    /// After going back, this process is the failed build; the restored app at the same place takes over.
    private func relaunchRestoredApp() {
        NSWorkspace.shared.openApplication(at: Bundle.main.bundleURL, configuration: {
            let configuration = NSWorkspace.OpenConfiguration()
            configuration.createsNewApplicationInstance = true
            return configuration
        }()) { _, _ in Task { @MainActor in NSApp.terminate(nil) } }
    }

    private func notify(_ text: String) {
        guard !BackgroundService.isProof else { return }
        let content = UNMutableNotificationContent()
        content.title = Words.appName
        content.body = text
        UNUserNotificationCenter.current().add(UNNotificationRequest(identifier: "update", content: content, trigger: nil))
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
    }

    struct Answer: Decodable {
        var error: String?
        var outcome: String?
        var intent: Intent?
        var failedBuilds: [String] = []

        enum CodingKeys: String, CodingKey { case error, outcome, intent, failedBuilds = "failed_builds" }
    }

    static func run(_ step: [String]) async -> Answer {
        let server = Bundle.main.bundleURL.appending(path: "Contents/MacOS/mediaforce-server")
        return await Task.detached {
            let process = Process()
            let pipe = Pipe()
            process.executableURL = server
            process.arguments = ["service", "update"] + step
            process.standardOutput = pipe
            guard (try? process.run()) != nil else { return Answer(error: Words.updateServerUnavailable) }
            let data = pipe.fileHandleForReading.readDataToEndOfFile()
            process.waitUntilExit()
            let line = String(decoding: data, as: UTF8.self).split(separator: "\n").last.map(String.init) ?? ""
            return (try? JSONDecoder().decode(Answer.self, from: Data(line.utf8))) ?? Answer(error: Words.updateServerUnavailable)
        }.value
    }
}
