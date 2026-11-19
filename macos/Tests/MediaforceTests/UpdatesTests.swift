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
