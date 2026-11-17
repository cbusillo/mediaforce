import Foundation
import XCTest
@testable import Mediaforce

final class PausedBarTests: XCTestCase {
    static let waitingSeason = """
    {"prefix": "tv/ER/Season 1", "show_prefix": "tv/ER", "show": "ER", "season_folder": "Season 1", "season": 1}
    """

    private func facts(pausedByOwner: Bool, waitingSeasons: Int = 0) throws -> MainWindowFacts {
        var json = try XCTUnwrap(JSONSerialization.jsonObject(with: Data(MainWindowTests.facts.utf8)) as? [String: Any])
        var work = try XCTUnwrap(json["work"] as? [String: Any])
        work["paused_by_owner"] = pausedByOwner
        let season = try JSONSerialization.jsonObject(with: Data(Self.waitingSeason.utf8))
        work["paused_check_seasons"] = Array(repeating: season, count: waitingSeasons)
        json["work"] = work
        return try JSONDecoder.mediaforce.decode(MainWindowFacts.self, from: JSONSerialization.data(withJSONObject: json))
    }

    func testThePausedBarShowsWhilePausedEvenWhenTheConnectionIsLost() throws {
        let paused = try facts(pausedByOwner: true)
        let running = try facts(pausedByOwner: false)
        XCTAssertEqual(TopNotice.shown(connection: .reachable, facts: paused), [.paused])
        XCTAssertEqual(TopNotice.shown(connection: .unreachable, facts: paused), [.stale, .paused])
        XCTAssertEqual(TopNotice.shown(connection: .failing, facts: running), [.stale])
        XCTAssertEqual(TopNotice.shown(connection: .reachable, facts: running), [])
        let pausedChecks = try facts(pausedByOwner: false, waitingSeasons: 1)
        XCTAssertEqual(TopNotice.shown(connection: .reachable, facts: pausedChecks), [.paused])
        XCTAssertEqual(TopNotice.shown(connection: .unreachable, facts: pausedChecks), [.stale, .paused])
    }

    func testPausedChecksNameWhatWaitsAndNeverReadAsTheOwnersPause() throws {
        let ownerPause = Words.pausedBar(try facts(pausedByOwner: true).work)
        let checks = try facts(pausedByOwner: false, waitingSeasons: 1).work
        let checksAndMore = try facts(pausedByOwner: false, waitingSeasons: 3).work

        XCTAssertNotEqual(Words.pausedBar(checks), ownerPause)
        XCTAssertTrue(Words.pausedBar(checks).contains(Words.season("ER", 1)))
        XCTAssertNotEqual(Words.pausedBar(checksAndMore), Words.pausedBar(checks))
        XCTAssertEqual(Words.pausedBar(try facts(pausedByOwner: true, waitingSeasons: 1).work), ownerPause)
        XCTAssertTrue(try XCTUnwrap(Words.workExplanation(try facts(pausedByOwner: true, waitingSeasons: 1)))
            .contains(Words.checksPaused(checks)))
    }
}
