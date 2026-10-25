import Foundation
import XCTest
@testable import Mediaforce

final class TVShowsTests: XCTestCase {
    private func season(_ number: Int?, state: String, tone: String, action: String? = nil) -> String {
        let folder = number.map { "Season \($0)" } ?? "Specials"
        let seasonJSON = number.map { "\($0)" } ?? "null"
        let actionJSON = action.map { "\"\($0)\"" } ?? "null"
        return #"{"prefix": "tv/Futurama/\#(folder)", "season": \#(seasonJSON), "state": "\#(state)", "action": \#(actionJSON), "failure": null, "tone": "\#(tone)", "episode_count": 13, "bytes": 4000000000}"#
    }

    private func show(_ seasons: [String], estimate: String = "5100000000") throws -> TVShowsFacts.Show {
        let json = """
        {"shows": [{"show_prefix": "tv/Futurama", "show": "Futurama", "season_count": \(seasons.count), "episode_count": 26,
                    "bytes": 8000000000, "estimated_saved_bytes": \(estimate), "seasons": [\(seasons.joined(separator: ", "))]}]}
        """
        return try XCTUnwrap(JSONDecoder.mediaforce.decode(TVShowsFacts.self, from: Data(json.utf8)).shows.first)
    }

    func testShowsDecodeFromServerShape() throws {
        let decoded = try show([season(2, state: "ready_to_compare", tone: "ready", action: "review_sample"), season(nil, state: "finished", tone: "success")])
        XCTAssertEqual(decoded.id, "tv/Futurama")
        XCTAssertEqual(decoded.estimatedSavedBytes, 5_100_000_000)
        XCTAssertEqual(decoded.seasons.map(\.season), [2, nil])
        XCTAssertEqual(decoded.seasons.map(Words.seasonName), ["Season 2", "Specials"])
        XCTAssertNil(try show([], estimate: "null").estimatedSavedBytes)
    }

    func testAShowTakesTheMostUrgentStateOfItsSeasons() throws {
        let cases: [([String], LibraryStateGroup)] = [
            ([season(1, state: "finished", tone: "success"), season(2, state: "needs_help", tone: "attention", action: "retry_sample")], .needsYou),
            ([season(1, state: "making_season", tone: "active"), season(2, state: "ready_to_compare", tone: "ready")], .inProgress),
            ([season(1, state: "sample_waiting", tone: "quiet"), season(2, state: "needs_test", tone: "quiet")], .inProgress),
            ([season(1, state: "ready_to_finish", tone: "ready"), season(2, state: "finished", tone: "success")], .readyForYou),
            ([season(1, state: "needs_test", tone: "quiet")], .readyForYou),
            ([season(1, state: "finished", tone: "success"), season(2, state: "motion_held", tone: "quiet")], .noActiveWork),
        ]
        for (seasons, group) in cases {
            XCTAssertEqual(try show(seasons).stateGroup, group, "\(seasons)")
        }
    }

    func testSampleAndApprovedSeasonsOpenTheSamplePage() throws {
        let opensSample = [
            season(1, state: "ready_to_compare", tone: "ready", action: "review_sample"),
            season(2, state: "needs_help", tone: "attention", action: "make_sample"),
            season(3, state: "needs_help", tone: "attention", action: "retry_sample"),
            season(4, state: "needs_help", tone: "attention", action: "choose_size"),
            season(5, state: "needs_test", tone: "quiet", action: "make_sample"),
            season(6, state: "ready_to_make", tone: "ready", action: "queue_encode"),
        ]
        let opensBrowser = [
            season(7, state: "needs_help", tone: "attention", action: "retry_encode"),
            season(8, state: "needs_help", tone: "attention", action: "review_files"),
            season(9, state: "ready_to_finish", tone: "ready", action: "promote_outputs"),
        ]
        let decoded = try show(opensSample + opensBrowser).seasons
        XCTAssertEqual(decoded.map(\.opensSample), Array(repeating: true, count: 6) + Array(repeating: false, count: 3))
        XCTAssertTrue(decoded.filter { !$0.opensSample }.allSatisfy { Words.seasonButton($0) == Words.openInBrowser })
        XCTAssertFalse(decoded.filter(\.opensSample).contains { Words.seasonButton($0) == Words.openInBrowser })
    }

    func testCompressingWithFilesThatNeedYouAndMotionChecksFirstAreSaid() throws {
        func flagged(_ json: String, _ flag: String) -> String {
            json.replacingOccurrences(of: #""bytes": 4000000000}"#, with: #""bytes": 4000000000, "\#(flag)": true}"#)
        }
        let compressing = season(1, state: "making_season", tone: "active")
        let approved = season(2, state: "ready_to_make", tone: "ready", action: "queue_encode")
        let decoded = try show([compressing, approved, flagged(compressing, "files_need_you"), flagged(approved, "checks_only")]).seasons
        XCTAssertEqual(decoded.map(\.filesNeedYou), [false, false, true, false])
        XCTAssertEqual(decoded.map(\.checksOnly), [false, false, false, true])
        XCTAssertNotEqual(Words.tvSeasonState(decoded[2]), Words.tvSeasonState(decoded[0]))
        XCTAssertNotEqual(Words.tvSeasonState(decoded[3]), Words.tvSeasonState(decoded[1]))
        XCTAssertEqual(Words.seasonButton(decoded[3]), Words.startCompressing)
    }

    func testSearchMatchesTheShowOrASeasonFolder() throws {
        let decoded = try show([season(2, state: "needs_test", tone: "quiet"), season(nil, state: "needs_test", tone: "quiet")])
        XCTAssertTrue(decoded.matches(""))
        XCTAssertTrue(decoded.matches("futur"))
        XCTAssertTrue(decoded.matches("specials"))
        XCTAssertFalse(decoded.matches("Season 9"))
    }
}
