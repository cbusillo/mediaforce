import Foundation
import XCTest
@testable import Mediaforce

final class OtherMediaTests: XCTestCase {
    // Every state code the server sends, with the group the page puts it in.
    private let groups: [(String, LibraryStateGroup)] = [
        ("cannot_start", .needsYou),
        ("needs_attention", .needsYou),
        ("compressing", .inProgress),
        ("ready", .readyForYou),
        ("view_only", .noActiveWork),
        ("no_work", .noActiveWork),
    ]

    private func facts(_ json: String) throws -> OtherMediaFacts {
        try JSONDecoder.mediaforce.decode(OtherMediaFacts.self, from: Data(json.utf8))
    }

    func testOtherMediaDecodeFromServerShape() throws {
        let decoded = try facts("""
        {"items": [{"prefix": "other/Trips", "title": "Trips", "library": "Home videos", "file_count": 12,
                    "bytes": 9000000000, "estimated_saved_bytes": 6000000000, "state": "ready"},
                   {"prefix": "other/Wedding.mkv", "title": "Wedding.mkv", "library": null, "file_count": 1,
                    "bytes": 500, "estimated_saved_bytes": null, "state": "view_only"}],
         "truncated": true}
        """)
        XCTAssertEqual(decoded.items.map(\.id), ["other/Trips", "other/Wedding.mkv"])
        XCTAssertEqual(decoded.items.map(\.estimatedSavedBytes), [6_000_000_000, nil])
        XCTAssertEqual(decoded.items.map(\.stateGroup), [.readyForYou, .noActiveWork])
        XCTAssertEqual(decoded.fileCount, 13)
        XCTAssertTrue(decoded.truncated)
        XCTAssertTrue(decoded.items[0].matches("trip"))
        XCTAssertFalse(decoded.items[0].matches("wedding"))
    }

    func testEachStateCodeLandsInItsGroup() {
        for (code, group) in groups {
            XCTAssertEqual(LibraryStateGroup(otherMediaState: code), group, code)
        }
        XCTAssertEqual(Set(groups.map { $0.1 }), Set(LibraryStateGroup.allCases))
    }

    func testEveryStateCodeHasItsOwnWords() {
        let words = groups.map { Words.otherMediaState($0.0) }
        XCTAssertEqual(Set(words).count, groups.count)
        XCTAssertFalse(groups.filter { $0.0 != "no_work" }.contains { Words.otherMediaState($0.0) == Words.otherMediaState("unknown") })
    }
}
