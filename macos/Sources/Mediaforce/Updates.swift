import AppKit
import Combine
import os
import Sparkle
import SwiftUI
import UserNotifications

/// Mediaforce updates itself between files, with no click (Director decision,
/// https://github.com/cbusillo/mediaforce/issues/1314#issuecomment-6841277256). Sparkle checks on its schedule but may
/// download only a build the bundled server has prepared: at once when nothing runs, otherwise after running files that
/// finish soon, or after an hour of waiting for that. Nothing else waits because an update exists. Each launch lets the
/// server start and check the new build, or go back to the saved one on its own. Sparkle checks hourly; Check for
/// Updates… runs that same background check now and then says plainly what it found. A server step that fails is tried again on a backoff while the app
/// runs; one that keeps failing for an hour is told once, and Check for Updates… gives the same reason.
@MainActor
final class Updates: NSObject, SPUUpdaterDelegate, ObservableObject {
    static let shared = Updates()
    static let logger = Logger(subsystem: Bundle.main.bundleIdentifier ?? Words.appName, category: "updates")

    @Published private(set) var canCheckForUpdates = false
    /// Shows the answer to Check for Updates…; a test reads it instead.
    var showAnswer: @MainActor (String) -> Void = Updates.alert
    /// Tells the Director in a notification; a test reads it instead.
    var notify: @MainActor (String) -> Void = Updates.post
    var relaunch: @MainActor () -> Void = Updates.relaunchRestoredApp
    /// The server's update steps, the wait between tries and the time; a test stands in for each.
    var runStep: @MainActor ([String]) async -> ServerUpdateStep.Answer = ServerUpdateStep.run
    var sleep: @MainActor (Duration) async throws -> Void = { try await Task.sleep(for: $0) }
    var now: @MainActor () -> Date = Date.init

    /// How long a failed step waits before the next try: 1, 2 and 5 minutes, then every 10.
    static let retryWaits: [Duration] = [.seconds(60), .seconds(120), .seconds(300), .seconds(600)]
    static let failureIsToldAfter: TimeInterval = 60 * 60

    private var controller: SPUStandardUpdaterController?
    private var checker: UpdateChecker?
    private var state = ServerUpdateStep.Answer()
    private(set) var preparing: Task<Void, Never>?
    private var cancelling = false
    private var asked = false
    /// Why the waiting update has kept failing to get ready, once that has lasted an hour.
    private var stuck: String?
    /// What this check found, in the words its answer uses.
    private var found: String?

    /// Release builds name a feed; source builds have none and never check.
    static var isConfigured: Bool {
        !(Bundle.main.object(forInfoDictionaryKey: "SUFeedURL") as? String ?? "").isEmpty
    }

    func start() async {
        // Recovery runs on every launch, even in a build that no longer names a feed.
        state = await runStep(["reconcile"])
        switch state.outcome {
        case "updated": notify(Words.updated)
        case "rolled back":
            notify(Words.updateRolledBack)
            return relaunch()
        default: if let problem = state.error, state.intent != nil { notify(Words.updateNeedsYou(problem)) }
        }
        if let intent = state.intent, intent.phase == "draining" { prepare(intent.to) }
        if state.intent?.phase == "prepared", !Self.isConfigured { state = await runStep(["cancel"]) }
        guard Self.isConfigured, controller == nil else { return }
        let controller = SPUStandardUpdaterController(startingUpdater: true, updaterDelegate: self, userDriverDelegate: nil)
        self.controller = controller
        use(controller.updater)
        // Quit or restarted after preparing: the server is stopped, so fetch the prepared build now, not on schedule.
        if state.intent?.awaitsSparkle == true { checker?.checkForUpdatesInBackground() }
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
        guard !cancelling else { throw UpdateWait(Words.updateWaitingForQuietTime) }
        if state.failedBuilds.contains(build) {
            found = Words.updateFailedBefore(build: version)
            throw UpdateWait(Words.updateFailedBefore(build: version))
        }
        if let intent = state.intent, intent.awaitsSparkle, let runtime = state.runtime,
           Self.setDownloadPhase("downloading", build: build, expected: intent, runtime: URL(filePath: runtime)) {
            state.intent = .init(phase: "downloading", to: build, stage: intent.stage)
            return
        }
        if preparing == nil { prepare(build, version: version) }
        found = stuck ?? Words.updateWillInstall(version: version)
        throw UpdateWait(Words.updateWaitingForQuietTime)
    }

    private static func setDownloadPhase(_ phase: String, build: String, expected: ServerUpdateStep.Intent, runtime: URL) -> Bool {
        guard let lock = try? FileHandle(forUpdating: runtime.appending(path: "deploy.lock")) else { return false }
        defer { try? lock.close() }
        guard flock(lock.fileDescriptor, LOCK_EX | LOCK_NB) == 0 else { return false }
        let record = runtime.appending(path: "update-intent.json")
        do {
            let data = try Data(contentsOf: record)
            let intent = try JSONDecoder().decode(ServerUpdateStep.Intent.self, from: data)
            guard intent.phase == expected.phase, intent.stage == expected.stage, intent.to == expected.to,
                  intent.app == Bundle.main.bundleURL.path,
                  var fields = try JSONSerialization.jsonObject(with: data) as? [String: Any] else { return false }
            fields["phase"] = phase
            fields["to"] = build
            try JSONSerialization.data(withJSONObject: fields).write(to: record, options: .atomic)
            return true
        } catch {
            return false
        }
    }

    /// The end of a check: only a check someone asked for gets an answer.
    func finishCheck(error: Error?) {
        defer { (asked, found) = (false, nil) }
        if let error { cancelPreparedUpdate(error: error) }
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
            if let window = NSApp.modalWindow, window.identifier == Self.answerWindow { NSApp.abortModal() }
        }
        immediateInstallationBlock()
        return true
    }

    nonisolated func updater(_ updater: SPUUpdater, didAbortWithError error: any Error) {
        MainActor.assumeIsolated { cancelPreparedUpdate(error: error) }
    }

    nonisolated func userDidCancelDownload(_ updater: SPUUpdater) {
        MainActor.assumeIsolated { cancelPreparedUpdate(error: URLError(.cancelled)) }
    }

    private func cancelPreparedUpdate(error: Error) {
        guard let intent = state.intent, intent.awaitsSparkle, !cancelling, found == nil, !(error is UpdateWait) else { return }
        if intent.phase == "downloading" {
            guard let runtime = state.runtime,
                  Self.setDownloadPhase("prepared", build: intent.to, expected: intent, runtime: URL(filePath: runtime)) else {
                notify(Words.updateNeedsYou("Open Mediaforce again to finish the update."))
                return
            }
        }
        cancelling = true
        let answer = Self.answer(found: nil, error: error)
        let report = asked || answer == Words.upToDate ? nil : answer
        Task {
            defer { cancelling = false }
            state = await runStep(["cancel", "--build", intent.to])
            if let problem = state.error { notify(Words.updateNeedsYou(problem)) }
            else if let report { notify(report) }
        }
    }

    /// Asks the server each minute whether now is the time; once it has prepared, Sparkle checks again and installs.
    /// A failed step is tried again on `retryWaits`; it never cuts off a running file, since the server only drains.
    private func prepare(_ build: String, version: String? = nil) {
        preparing = Task {
            defer { preparing = nil }
            var failures = 0
            var failingSince: Date?
            while !Task.isCancelled {
                state = await runStep(["prepare", "--build", build])
                if let problem = state.error {
                    let since = failingSince ?? now()
                    failingSince = since
                    if stuck == nil, now().timeIntervalSince(since) >= Self.failureIsToldAfter {
                        let words = Words.updateKeepsFailing(version: version, problem)
                        stuck = words
                        notify(words)
                    }
                    failures += 1
                    try? await sleep(Self.retryWaits[min(failures, Self.retryWaits.count) - 1])
                    continue
                }
                (failures, failingSince, stuck) = (0, nil, nil)
                if state.intent?.phase == "prepared" {
                    checker?.checkForUpdatesInBackground()
                    return
                }
                try? await sleep(.seconds(60))
            }
        }
    }

    /// After going back, this process is the failed build; the restored app at the same place takes over.
    private static func relaunchRestoredApp() {
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

    private static func post(_ text: String) {
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
        var app: String?
        var stage: String?
        var awaitsSparkle: Bool { phase == "prepared" || phase == "downloading" }
    }

    struct Answer: Decodable {
        var error: String?
        var outcome: String?
        var runtime: String?
        var intent: Intent?
        var failedBuilds: [String] = []

        enum CodingKeys: String, CodingKey { case error, outcome, runtime, intent, failedBuilds = "failed_builds" }
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
