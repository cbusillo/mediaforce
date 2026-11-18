import AppKit
import Combine
import os
import Sparkle
import SwiftUI
import UserNotifications

/// Mediaforce updates itself between files, with no click (Director decision,
/// https://github.com/cbusillo/mediaforce/issues/1314#issuecomment-6841277256). Sparkle checks on its schedule but may
/// download only a build the bundled server has prepared: at once when nothing runs, otherwise after running files that
/// finish soon. Nothing waits merely because an update exists. Each launch lets the server start and check the new
/// build, or go back to the saved one on its own. Sparkle checks hourly; Check for Updates… runs that same background
/// check now and then says plainly what it found.
@MainActor
final class Updates: NSObject, SPUUpdaterDelegate, ObservableObject {
    static let shared = Updates()
    static let logger = Logger(subsystem: Bundle.main.bundleIdentifier ?? Words.appName, category: "updates")

    @Published private(set) var canCheckForUpdates = false
    /// Shows the answer to Check for Updates…; a test reads it instead.
    var showAnswer: @MainActor (String) -> Void = Updates.alert

    private var controller: SPUStandardUpdaterController?
    private var checker: UpdateChecker?
    private var state = ServerUpdateStep.Answer()
    private var preparing: Task<Void, Never>?
    private var asked = false
    /// What this check found, in the words its answer uses.
    private var found: String?

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
        let controller = SPUStandardUpdaterController(startingUpdater: true, updaterDelegate: self, userDriverDelegate: nil)
        self.controller = controller
        use(controller.updater)
        // Quit or restarted after preparing: the server is stopped, so fetch the prepared build now, not on schedule.
        if state.intent?.phase == "prepared" { checker?.checkForUpdatesInBackground() }
    }

    func use(_ checker: UpdateChecker) {
        self.checker = checker
        checker.canCheck.assign(to: &$canCheckForUpdates)
    }

    /// Check for Updates…: not Sparkle's own window, whose Install button would skip the wait for quiet time, but the
    /// background check it runs every hour, so what it finds is admitted the same way.
    func checkNow() {
        guard let checker, canCheckForUpdates else { return }
        asked = true
        checker.checkForUpdatesInBackground()
    }

    /// What a found build is told: go ahead only once the server prepared it, otherwise wait and let the server prepare.
    func admit(build: String, version: String) throws {
        found = nil
        if state.failedBuilds.contains(build) {
            found = Words.updateFailedBefore(build: version)
            throw UpdateWait(Words.updateFailedBefore(build: version))
        }
        if let intent = state.intent, intent.to == build, intent.phase == "prepared" { return }
        if preparing == nil { prepare(build) }
        found = Words.updateWillInstall(version: version)
        throw UpdateWait(Words.updateWaitingForQuietTime)
    }

    /// The end of a check: only a check someone asked for gets an answer.
    func finishCheck(error: Error?) {
        defer { (asked, found) = (false, nil) }
        guard asked, let answer = Self.answer(found: found, error: error) else { return }
        showAnswer(answer)
    }

    static func answer(found: String?, error: Error?) -> String? {
        if let found { return found }
        // No error and nothing found to wait for: Sparkle went on to install, and the relaunched app says so.
        guard let error = error as NSError? else { return nil }
        if error.domain == SUSparkleErrorDomain, error.code == Int(SUError.noUpdateError.rawValue) { return Words.upToDate }
        return Words.updateCheckFailed(error.localizedDescription)
    }

    // Sparkle calls this before it downloads anything; a thrown error stops the download until the next check.
    nonisolated func updater(_ updater: SPUUpdater, shouldProceedWithUpdate item: SUAppcastItem, updateCheck: SPUUpdateCheck) throws {
        let (build, version) = (item.versionString, item.displayVersionString)
        try MainActor.assumeIsolated { try admit(build: build, version: version) }
    }

    nonisolated func updater(_ updater: SPUUpdater, didFinishUpdateCycleFor updateCheck: SPUUpdateCheck, error: (any Error)?) {
        MainActor.assumeIsolated { finishCheck(error: error) }
    }

    // The prepared build is downloaded: install it now and relaunch; the server is already stopped and saved.
    nonisolated func updater(_ updater: SPUUpdater, willInstallUpdateOnQuit item: SUAppcastItem, immediateInstallationBlock: @escaping () -> Void) -> Bool {
        MainActor.assumeIsolated {
            // An answer still on screen must not hold the app open; it has nothing left to say.
            if let window = NSApp.modalWindow, window.identifier == Self.answerWindow { NSApp.abortModal() }
        }
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
                    checker?.checkForUpdatesInBackground()
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

    private static let answerWindow = NSUserInterfaceItemIdentifier("update-answer")

    /// The usual Mac answer to Check for Updates…, shown from the run loop so the update keeps moving behind it.
    private static func alert(_ text: String) {
        logger.notice("Check for Updates answered: \(text, privacy: .public)")
        RunLoop.main.perform(inModes: [.common]) {
            MainActor.assumeIsolated {
                let alert = NSAlert()
                alert.messageText = text
                alert.window.identifier = answerWindow
                alert.runModal()
            }
        }
    }

    private func notify(_ text: String) {
        guard !BackgroundService.isProof else { return }
        let content = UNMutableNotificationContent()
        content.title = Words.appName
        content.body = text
        UNUserNotificationCenter.current().add(UNNotificationRequest(identifier: "update", content: content, trigger: nil))
    }
}

/// The part of Sparkle's updater the app uses, so a test can stand in for it.
@MainActor
protocol UpdateChecker: AnyObject {
    var canCheck: AnyPublisher<Bool, Never> { get }
    func checkForUpdatesInBackground()
}

extension SPUUpdater: UpdateChecker {
    var canCheck: AnyPublisher<Bool, Never> { publisher(for: \.canCheckForUpdates).eraseToAnyPublisher() }
}

/// Check for Updates… in the app menu, after About; off while a check or an install is under way.
struct CheckForUpdatesButton: View {
    @ObservedObject var updates: Updates

    var body: some View {
        Button(Words.checkForUpdates) { updates.checkNow() }
            .disabled(!updates.canCheckForUpdates)
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
