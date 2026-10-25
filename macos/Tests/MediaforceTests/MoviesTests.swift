import Foundation
import XCTest
@testable import Mediaforce

final class MoviesTests: XCTestCase {
    // Every state code the server sends, with the group the web library puts it in.
    private let groups: [(String, LibraryStateGroup)] = [
        ("cannot_start", .needsYou),
        ("needs_file_choice", .needsYou),
        ("needs_attention", .needsYou),
        ("compressing", .inProgress),
        ("ready", .readyForYou),
        ("view_only", .noActiveWork),
        ("finished", .noActiveWork),
        ("motion_held", .noActiveWork),
        ("no_work", .noActiveWork),
    ]

    private func movies(_ json: String) throws -> [MoviesFacts.Movie] {
        try JSONDecoder.mediaforce.decode(MoviesFacts.self, from: Data(json.utf8)).movies
    }

    func testMoviesDecodeFromServerShape() throws {
        let decoded = try movies("""
        {"movies": [{"prefix": "movies/Heat (1995)", "title": "Heat (1995)", "library": "Movies", "file_count": 2,
                     "bytes": 9000000000, "estimated_saved_bytes": 6000000000, "state": "ready"},
                    {"prefix": "movies/Up (2009)", "title": "Up (2009)", "library": null, "file_count": 1,
                     "bytes": 500, "estimated_saved_bytes": null, "state": "no_work"}]}
        """)
        XCTAssertEqual(decoded.map(\.id), ["movies/Heat (1995)", "movies/Up (2009)"])
        XCTAssertEqual(decoded.map(\.estimatedSavedBytes), [6_000_000_000, nil])
        XCTAssertEqual(decoded.map(\.stateGroup), [.readyForYou, .noActiveWork])
        XCTAssertTrue(decoded[0].matches("heat"))
        XCTAssertFalse(decoded[0].matches("alien"))
    }

    func testEachStateCodeLandsInTheWebLibrarysGroup() {
        for (code, group) in groups {
            XCTAssertEqual(LibraryStateGroup(movieState: code), group, code)
        }
        XCTAssertEqual(Set(groups.map { $0.1 }), Set(LibraryStateGroup.allCases))
    }

    func testEveryStateCodeHasItsOwnWords() {
        let words = groups.map { Words.movieState($0.0) }
        XCTAssertEqual(Set(words).count, groups.count)
        XCTAssertFalse(groups.filter { $0.0 != "no_work" }.contains { Words.movieState($0.0) == Words.movieState("unknown") })
    }
}
