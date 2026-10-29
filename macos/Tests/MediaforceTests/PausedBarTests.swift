import Foundation
import XCTest
@testable import Mediaforce

final class PausedBarTests: XCTestCase {
    private func facts(pausedByOwner: Bool, checksPaused: Bool = false) throws -> MainWindowFacts {
        var json = try XCTUnwrap(JSONSerialization.jsonObject(with: Data(MainWindowTests.facts.utf8)) as? [String: Any])
        var work = try XCTUnwrap(json["work"] as? [String: Any])
        work["paused_by_owner"] = pausedByOwner
        work["checks_paused"] = checksPaused
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
        let pausedChecks = try facts(pausedByOwner: false, checksPaused: true)
        XCTAssertEqual(TopNotice.shown(connection: .reachable, facts: pausedChecks), [.paused])
        XCTAssertEqual(TopNotice.shown(connection: .unreachable, facts: pausedChecks), [.stale, .paused])
    }
}
