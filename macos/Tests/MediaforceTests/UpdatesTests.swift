import Combine
import Foundation
import Sparkle
import XCTest
@testable import Mediaforce

@MainActor
final class UpdatesTests: XCTestCase {
    private final class FakeUpdater: UpdateChecker {
        let can = CurrentValueSubject<Bool, Never>(false)
        var checks = 0
        var canCheck: AnyPublisher<Bool, Never> { can.eraseToAnyPublisher() }
        func checkForUpdatesInBackground() { checks += 1 }
    }

    private var answers: [String] = []

    private func updates(_ updater: FakeUpdater) -> Updates {
        let updates = Updates()
        updates.use(updater)
        updates.showAnswer = { self.answers.append($0) }
        updates.notify = { self.notices.append($0) }
        updates.runStep = { _ in ServerUpdateStep.Answer(intent: .init(phase: "draining", to: "3002")) }
        return updates
    }

    private var notices: [String] = []
    private var clock = Date(timeIntervalSince1970: 0)
    private var waits: [Duration] = []

    /// The server answers each prepare step from `script` in turn; each wait moves the clock and runs `during`.
    private func scripted(_ updates: Updates, _ script: [ServerUpdateStep.Answer],
                          during: @escaping @MainActor (Int) -> Void = { _ in }) {
        var tries = 0
        updates.runStep = { _ in
            defer { tries += 1 }
            return script[tries]
        }
        updates.now = { self.clock }
        updates.sleep = { wait in
            self.waits.append(wait)
            self.clock += TimeInterval(wait.components.seconds)
            during(self.waits.count)
        }
    }

    private static let failed = ServerUpdateStep.Answer(error: "invalid literal for int() with base 10: 'archive-activity'")
    private static let prepared = ServerUpdateStep.Answer(intent: .init(phase: "prepared", to: "3002"))

    func testTheMenuItemIsOnOnlyWhileTheUpdaterCanCheck() {
        let updater = FakeUpdater()
        let updates = updates(updater)
        XCTAssertFalse(updates.canCheckForUpdates)
        updater.can.send(true)
        XCTAssertTrue(updates.canCheckForUpdates)
        updater.can.send(false)
        XCTAssertFalse(updates.canCheckForUpdates)
        updates.checkNow()
        XCTAssertEqual(updater.checks, 0, "a check already under way is not started again")
    }

    func testChoosingItRunsTheUpdatersOwnBackgroundCheck() {
        let updater = FakeUpdater()
        let updates = updates(updater)
        updater.can.send(true)
        updates.checkNow()
        XCTAssertEqual(updater.checks, 1)
    }

    func testAFoundBuildWaitsForQuietTimeAndSaysSo() {
        let updater = FakeUpdater()
        let updates = updates(updater)
        updater.can.send(true)
        updates.checkNow()
        XCTAssertThrowsError(try updates.admit(build: "3002", version: "0.5.0"))
        updates.finishCheck(error: UpdateWait(Words.updateWaitingForQuietTime))
        XCTAssertEqual(answers, ["Mediaforce 0.5.0 will install between files."])
    }

    func testUpToDateAndFailedChecksSaySo() {
        let updater = FakeUpdater()
        let updates = updates(updater)
        updater.can.send(true)
        updates.checkNow()
        updates.finishCheck(error: NSError(domain: SUSparkleErrorDomain, code: Int(SUError.noUpdateError.rawValue)))
        updates.checkNow()
        updates.finishCheck(error: NSError(domain: NSURLErrorDomain, code: NSURLErrorNotConnectedToInternet,
                                           userInfo: [NSLocalizedDescriptionKey: "The Internet connection appears to be offline."]))
        XCTAssertEqual(answers, ["You're up to date.",
                                 "Mediaforce couldn't check for updates. The Internet connection appears to be offline."])
    }

    func testAFailingStepIsTriedAgainOnTheBackoffUntilItPrepares() async {
        let updater = FakeUpdater()
        let updates = updates(updater)
        scripted(updates, Array(repeating: Self.failed, count: 6) + [Self.prepared])
        XCTAssertThrowsError(try updates.admit(build: "3002", version: "0.5.3"))
        await updates.preparing?.value
        XCTAssertEqual(waits, [60, 120, 300, 600, 600, 600].map { Duration.seconds($0) })
        XCTAssertEqual(updater.checks, 1, "once prepared, Sparkle checks again to install")
        XCTAssertEqual(notices, [], "a failure shorter than an hour stays quiet")
    }

    func testALastingFailureIsToldOnceInPlainWordsAndASuccessClearsIt() async {
        let updater = FakeUpdater()
        let updates = updates(updater)
        updater.can.send(true)
        let stuck = "Mediaforce 0.5.3 couldn't get ready to install: invalid literal for int() with base 10: 'archive-activity'. It keeps trying."
        // 1+2+5 minutes, then nine 10-minute waits: past an hour, and still failing for a while after.
        let draining = ServerUpdateStep.Answer(intent: .init(phase: "draining", to: "3002"))
        scripted(updates, Array(repeating: Self.failed, count: 13) + [draining, Self.prepared]) { wait in
            guard wait == 13 || wait == 14 else { return }
            // Check for Updates… while the update is stuck, then once it got going again.
            updates.checkNow()
            do { try updates.admit(build: "3002", version: "0.5.3") } catch {}
            updates.finishCheck(error: UpdateWait(Words.updateWaitingForQuietTime))
        }
        XCTAssertThrowsError(try updates.admit(build: "3002", version: "0.5.3"))
        await updates.preparing?.value
        XCTAssertEqual(notices, [stuck])
        XCTAssertEqual(answers, [stuck, "Mediaforce 0.5.3 will install between files."])
    }

    func testPreparedDownloadFailureRestoresServiceAndReportsOnce() async {
        let updates = updates(FakeUpdater())
        var steps: [[String]] = []
        let restored = expectation(description: "cancel the prepared update")
        updates.runStep = { arguments in
            steps.append(arguments)
            if arguments == ["cancel", "--build", "3002"] {
                restored.fulfill()
                return ServerUpdateStep.Answer()
            }
            return Self.prepared
        }
        XCTAssertThrowsError(try updates.admit(build: "3002", version: "0.5.3"))
        updates.finishCheck(error: UpdateWait(Words.updateWaitingForQuietTime))
        await updates.preparing?.value
        let failure = URLError(.networkConnectionLost)
        updates.finishCheck(error: failure)
        await fulfillment(of: [restored], timeout: 2)
        updates.finishCheck(error: failure)
        XCTAssertEqual(steps, [["prepare", "--build", "3002"], ["cancel", "--build", "3002"]])
        XCTAssertEqual(notices, [Words.updateCheckFailed(failure.localizedDescription)])
    }

    func testRollbackTellsTheOwnerBeforeReopeningTheSavedApp() async {
        let updates = updates(FakeUpdater())
        updates.runStep = { _ in .init(outcome: "rolled back") }
        let reopened = expectation(description: "open the saved app")
        updates.relaunch = {
            XCTAssertEqual(self.notices, [Words.updateRolledBack])
            reopened.fulfill()
        }
        await updates.start()
        await fulfillment(of: [reopened], timeout: 2)
    }

    func testCancelledPreparationCannotAdmitTheCachedBuild() async throws {
        let runtime = FileManager.default.temporaryDirectory.appending(path: UUID().uuidString)
        try FileManager.default.createDirectory(at: runtime, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: runtime) }
        let intent = runtime.appending(path: "update-intent.json")
        try Data().write(to: runtime.appending(path: "deploy.lock"))
        try JSONSerialization.data(withJSONObject: ["phase": "prepared", "to": "3002", "app": Bundle.main.bundleURL.path]).write(to: intent)
        let updates = updates(FakeUpdater())
        updates.runStep = { _ in .init(runtime: runtime.path, intent: Self.prepared.intent) }
        XCTAssertThrowsError(try updates.admit(build: "3002", version: "0.5.3"))
        await updates.preparing?.value
        let prepared = try Data(contentsOf: intent)
        try FileManager.default.removeItem(at: intent)
        XCTAssertThrowsError(try updates.admit(build: "3002", version: "0.5.3"))
        await updates.preparing?.value
        try prepared.write(to: intent)
        XCTAssertNoThrow(try updates.admit(build: "3002", version: "0.5.3"))
        let claimed = try JSONDecoder().decode(ServerUpdateStep.Intent.self, from: Data(contentsOf: intent))
        XCTAssertEqual(claimed.phase, "downloading")
        let aborted = expectation(description: "the failed download returns the saved service")
        updates.runStep = { _ in
            let restored = try? JSONDecoder().decode(ServerUpdateStep.Intent.self, from: Data(contentsOf: intent))
            XCTAssertEqual(restored?.phase, "prepared")
            aborted.fulfill()
            return .init()
        }
        updates.finishCheck(error: URLError(.networkConnectionLost))
        await fulfillment(of: [aborted], timeout: 2)
    }

    func testTheHourlyCheckStaysQuiet() {
        let updater = FakeUpdater()
        let updates = updates(updater)
        updater.can.send(true)
        XCTAssertThrowsError(try updates.admit(build: "3002", version: "0.5.0"))
        updates.finishCheck(error: UpdateWait(Words.updateWaitingForQuietTime))
        updates.finishCheck(error: NSError(domain: SUSparkleErrorDomain, code: Int(SUError.noUpdateError.rawValue)))
        XCTAssertEqual(answers, [])
    }
}
