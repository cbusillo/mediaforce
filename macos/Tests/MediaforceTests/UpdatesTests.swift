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
        return updates
    }

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
