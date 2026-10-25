import Foundation
import XCTest
@testable import Mediaforce

final class PausedBarTests: XCTestCase {
    private func facts(pausedByOwner: Bool) throws -> MainWindowFacts {
        var json = try XCTUnwrap(JSONSerialization.jsonObject(with: Data(MainWindowTests.facts.utf8)) as? [String: Any])
        var work = try XCTUnwrap(json["work"] as? [String: Any])
        work["paused_by_owner"] = pausedByOwner
        json["work"] = work
        return try JSONDecoder.mediaforce.decode(MainWindowFacts.self, from: JSONSerialization.data(withJSONObject: json))
    }

    func testTheBarShowsExactlyWhileTheDirectorHasPaused() throws {
        XCTAssertTrue(PausedBar.shows(try facts(pausedByOwner: true)))
        XCTAssertFalse(PausedBar.shows(try facts(pausedByOwner: false)))
    }
}
