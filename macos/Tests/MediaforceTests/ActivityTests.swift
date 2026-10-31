import Foundation
import XCTest
@testable import Mediaforce

final class ActivityTests: XCTestCase {
    private let payload = """
    {"starts_at":"2026-10-31T14:00:00.123456+00:00","ends_at":"2026-11-01T14:00:00.123456+00:00",
     "computers":[{"key":"mini","windows":[{"starts_at":"2026-10-31T14:00:00+00:00","ends_at":"2026-11-01T14:00:00+00:00"}],
                   "running":[],"finish_current_file":true}],
     "lined_up":[{"id":"a","prefix":"tv/Show/Season 1","show_prefix":"tv/Show","show":"Show","title":"Episode 1",
                  "starts_at":null,"finishes_at":null,"computer_key":null,"computer_name":null,"waiting":"Waiting for storage","progress":null}],
     "earlier_holds":[],"next_season":{"prefix":"tv/Next/Season 2","show_prefix":"tv/Next","show":"Next","season":2}}
    """

    func testUnknownStartsStayVisibleWithoutAnInventedTimeOrComputer() throws {
        let activity = try JSONDecoder.mediaforce.decode(ActivityFacts.self, from: Data(payload.utf8))
        XCTAssertEqual(activity.linedUp.count, 1)
        XCTAssertNil(activity.linedUp[0].startsAt)
        XCTAssertNil(activity.linedUp[0].computerKey)
        XCTAssertNotNil(activity.linedUp[0].waiting)
        XCTAssertEqual(activity.nextSeason?.season, 2)
    }

    func testTimelineUsesElapsedTimeAndClipsFilesAtItsEdges() throws {
        let activity = try JSONDecoder.mediaforce.decode(ActivityFacts.self, from: Data(payload.utf8))
        XCTAssertEqual(try XCTUnwrap(activity.fraction("2026-11-01T02:00:00.123456+00:00")), 0.5, accuracy: 0.00001)
        XCTAssertEqual(activity.fraction("2026-10-30T14:00:00+00:00"), 0)
        XCTAssertEqual(activity.fraction("2026-11-02T14:00:00+00:00"), 1)
        XCTAssertNil(activity.fraction(""))
        XCTAssertNil(ActivityFacts.time(nil))
    }

    func testActivityHasItsOwnBackDestination() {
        var navigation = PageNavigation()
        navigation.open(.activity)
        navigation.open(.item("tv/Show/Season 1"))
        navigation.goBack()
        XCTAssertEqual(navigation.place, .activity)
    }
}
