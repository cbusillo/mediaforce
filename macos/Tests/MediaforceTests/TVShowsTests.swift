import Foundation
import XCTest
@testable import Mediaforce

final class TVShowsTests: XCTestCase {
    private func season(_ number: Int?, state: String, tone: String, action: String? = nil, needsYou: Bool? = false) -> String {
        let folder = number.map { "Season \($0)" } ?? "Specials"
        let seasonJSON = number.map { "\($0)" } ?? "null"
        let actionJSON = action.map { "\"\($0)\"" } ?? "null"
        let needsYouJSON = needsYou.map { ", \"needs_you\": \($0)" } ?? ""
        return #"{"prefix": "tv/Futurama/\#(folder)", "season": \#(seasonJSON), "state": "\#(state)", "action": \#(actionJSON), "failure": null, "tone": "\#(tone)"\#(needsYouJSON), "episode_count": 13, "bytes": 4000000000}"#
    }

    private func show(_ seasons: [String], estimate: String = "5100000000", looseFiles: String = "") throws -> TVShowsFacts.Show {
        let json = """
        {"shows": [{"show_prefix": "tv/Futurama", "show": "Futurama", "season_count": \(seasons.count), "episode_count": 26,
                    "bytes": 8000000000, "estimated_saved_bytes": \(estimate), "seasons": [\(seasons.joined(separator: ", "))]\(looseFiles)}]}
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
        XCTAssertNil(decoded.looseFiles)
        let legacy = try show([season(2, state: "ready_to_compare", tone: "ready", needsYou: nil)])
        XCTAssertEqual(legacy.seasons.first?.prefix, decoded.seasons.first?.prefix)
    }

    func testAShowWithOnlyFilesOutsideASeasonFolderIsListedAndSaysWhy() throws {
        let loose = #", "loose_files": {"state": "not_in_season_folder", "episode_count": 3, "bytes": 2000000000}"#
        let decoded = try show([], estimate: "0", looseFiles: loose)
        let files = try XCTUnwrap(decoded.looseFiles)
        XCTAssertEqual(decoded.id, "tv/Futurama")
        XCTAssertEqual(files.state, "not_in_season_folder")
        XCTAssertEqual(files.episodeCount, 3)
        XCTAssertEqual(files.bytes, 2_000_000_000)
        let many = Words.looseFiles(files)
        XCTAssertTrue(many.hasPrefix("3 episodes (\(Words.size(2_000_000_000)))"), many)
        let oneJSON = loose.replacingOccurrences(of: #""episode_count": 3"#, with: #""episode_count": 1"#)
        let oneFile = try XCTUnwrap(try show([], looseFiles: oneJSON).looseFiles)
        let one = Words.looseFiles(oneFile)
        XCTAssertTrue(one.hasPrefix("1 episode ("), one)
        // One file reads in the singular past its count, not as "1 episode aren't ... move them".
        XCTAssertNotEqual(one.replacingOccurrences(of: "1 episode", with: "3 episodes"), many)
    }

    func testASeasonWithNothingLeftToCompressDoesNotSayItIsWaiting() {
        XCTAssertNotEqual(Words.seasonState("no_work", nil), Words.seasonState(nil, nil))
    }

    func testAShowTakesTheMostUrgentStateOfItsSeasons() throws {
        let cases: [([String], LibraryStateGroup)] = [
            ([season(1, state: "finished", tone: "success"), season(2, state: "needs_help", tone: "attention", action: "retry_sample", needsYou: true)], .needsYou),
            ([season(1, state: "making_season", tone: "active", needsYou: true), season(2, state: "ready_to_compare", tone: "ready", needsYou: true)], .needsYou),
            ([season(1, state: "sample_waiting", tone: "attention"), season(2, state: "needs_test", tone: "quiet")], .inProgress),
            ([season(1, state: "ready_to_finish", tone: "ready"), season(2, state: "finished", tone: "success")], .readyToCompress),
            ([season(1, state: "needs_test", tone: "quiet")], .readyToCompress),
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
            season(10, state: "making_test", tone: "active"),
            season(11, state: "sample_waiting", tone: "active"),
        ]
        let opensItem = [
            season(7, state: "needs_help", tone: "attention", action: "retry_encode"),
            season(8, state: "needs_help", tone: "attention", action: "review_files"),
            season(9, state: "ready_to_finish", tone: "ready", action: "promote_outputs"),
        ]
        let decoded = try show(opensSample + opensItem).seasons
        XCTAssertEqual(decoded.map(\.opensSample), Array(repeating: true, count: 8) + Array(repeating: false, count: 3))
        XCTAssertTrue(decoded.filter { !$0.opensSample }.allSatisfy { Words.seasonButton($0) == Words.openItem })
        XCTAssertFalse(decoded.filter(\.opensSample).contains { Words.seasonButton($0) == Words.openItem })
    }

    func testCompressingWithFilesThatNeedYouAndMotionChecksFirstAreSaid() throws {
        func flagged(_ json: String, _ flag: String) throws -> String {
            var object = try XCTUnwrap(JSONSerialization.jsonObject(with: Data(json.utf8)) as? [String: Any])
            object[flag] = true
            return String(decoding: try JSONSerialization.data(withJSONObject: object), as: UTF8.self)
        }
        let compressing = season(1, state: "making_season", tone: "active")
        let approved = season(2, state: "ready_to_make", tone: "ready", action: "queue_encode")
        let decoded = try show([compressing, approved, try flagged(compressing, "needs_you"), try flagged(approved, "checks_only"), try flagged(approved, "needs_you")]).seasons
        XCTAssertEqual(decoded.map(\.needsYou), [false, false, true, false, true])
        XCTAssertEqual(decoded.map(\.checksOnly), [false, false, false, true, false])
        XCTAssertNotEqual(Words.tvSeasonState(decoded[2]), Words.tvSeasonState(decoded[0]))
        XCTAssertNotEqual(Words.tvSeasonState(decoded[3]), Words.tvSeasonState(decoded[1]))
        XCTAssertEqual(Words.seasonButton(decoded[3]), Words.startCompressing)
        XCTAssertNotEqual(Words.tvSeasonState(decoded[4]), Words.tvSeasonState(decoded[1]))
    }

    func testSearchMatchesTheShowOrASeasonFolder() throws {
        let decoded = try show([season(2, state: "needs_test", tone: "quiet"), season(nil, state: "needs_test", tone: "quiet")])
        XCTAssertTrue(decoded.matches(""))
        XCTAssertTrue(decoded.matches("futur"))
        XCTAssertTrue(decoded.matches("specials"))
        XCTAssertFalse(decoded.matches("Season 9"))
    }
}
