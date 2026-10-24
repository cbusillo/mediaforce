import Foundation
import XCTest
@testable import Mediaforce

final class AttentionTests: XCTestCase {
    private struct Finished {
        let prefix: String
        let at: String
    }

    private func facts(
        finished: [Finished] = [],
        waitingSamples: [(prefix: String, state: String, action: String)] = [],
        computers: [(key: String, state: String)] = [],
        backups: Int = 0
    ) throws -> MainWindowFacts {
        let finishedJSON = finished.map { season in
            #"{"prefix": "\#(season.prefix)", "show_prefix": "tv/Futurama", "show": "Futurama", "season": 3, "episode_count": 15, "bytes_saved": 4000000000, "finished_at": "\#(season.at)"}"#
        }
        let samplesJSON = waitingSamples.map { sample in
            #"{"prefix": "\#(sample.prefix)", "show_prefix": "tv/Expanse", "show": "The Expanse", "season": 3, "state": "\#(sample.state)", "action": "\#(sample.action)", "failure": null}"#
        }
        let computersJSON = computers.map { computer in
            #"{"key": "\#(computer.key)", "name": "\#(computer.key)", "platform": "macos", "state": "\#(computer.state)", "running_count": 0, "schedule_opens_at": null}"#
        }
        let json = """
        {"observed_at": "2026-10-24T12:00:00+00:00",
         "work": {"state": "idle", "paused_by_owner": false, "stop_reasons": [], "running_count": 0, "waiting_count": 0},
         "library": {"tv": {"show_count": 0, "season_count": 0, "episode_count": 0, "total_bytes": 0, "original_bytes": 0},
                     "movies": {"title_count": 0, "total_bytes": 0, "original_bytes": 0},
                     "other": {"item_count": 0, "total_bytes": 0, "original_bytes": 0}},
         "space_saved": {"bytes": 0, "item_count": 0, "episode_count": 0, "season_count": \(finished.count)},
         "recently_finished_seasons": [\(finishedJSON.joined(separator: ","))],
         "biggest_not_started": [],
         "samples": {"waiting_on_you": [\(samplesJSON.joined(separator: ","))], "approved_not_started": []},
         "computers": [\(computersJSON.joined(separator: ","))],
         "events": [], "last_change": null,
         "original_backups": {"count": \(backups), "bytes": \(backups * 1_000_000_000)},
         "kept_original": {"kept_item_count": 0}}
        """
        return try JSONDecoder.mediaforce.decode(MainWindowFacts.self, from: Data(json.utf8))
    }

    private let older = Finished(prefix: "tv/F/Season 2", at: "2026-10-20T00:00:00+00:00")
    private let newer = Finished(prefix: "tv/F/Season 3", at: "2026-10-22T00:00:00+00:00")

    func testFirstLookNotifiesNothingButCountsTheBadge() throws {
        let now = try facts(finished: [older], waitingSamples: [("tv/E/Season 3", "ready_to_compare", "review_sample")], backups: 4)
        let attention = Attention.review(now, remembered: nil)
        XCTAssertEqual(attention.notices, [])
        XCTAssertEqual(attention.badgeCount, 2)
    }

    func testEachNewThingNotifiesOnce() throws {
        let before = Attention.review(try facts(finished: [older]), remembered: nil).memory
        let now = try facts(
            finished: [newer, older],
            waitingSamples: [("tv/E/Season 3", "ready_to_compare", "review_sample")],
            computers: [("mbp", "password_needed"), ("mini", "storage_unreachable")],
            backups: 15
        )
        let first = Attention.review(now, remembered: before)
        let second = Attention.review(now, remembered: first.memory)

        XCTAssertEqual(first.notices.map(\.title), [
            "Futurama, Season 3 is finished",
            "The Expanse, Season 3 sample is ready",
            "mbp needs you",
            "Original backups are ready to delete",
        ])
        XCTAssertEqual(second.notices, [])
        XCTAssertEqual(first.badgeCount, 3)
    }

    func testManySeasonsFinishedWhileClosedNotifyOnce() throws {
        let before = Attention.review(try facts(finished: [older]), remembered: nil).memory
        let visible = (1...12).map { Finished(prefix: "tv/F/Season \($0 + 10)", at: "2026-10-23T00:00:\(String(format: "%02d", $0))+00:00") }

        let allNew = Attention.review(try facts(finished: visible), remembered: before)
        let someNew = Attention.review(try facts(finished: Array(visible.suffix(4)) + [older]), remembered: before)

        XCTAssertEqual(allNew.notices.map(\.title), ["At least 12 seasons finished"])
        XCTAssertEqual(someNew.notices.map(\.title), ["4 seasons finished"])
    }

    func testSeasonsThatComeBackOrMoveIntoTheListDoNotNotify() throws {
        let shown = Attention.review(try facts(finished: [newer]), remembered: AttentionMemory())
        let gone = Attention.review(try facts(), remembered: shown.memory)
        let back = Attention.review(try facts(finished: [newer, older]), remembered: gone.memory)
        XCTAssertEqual(back.notices, [])
    }

    func testANewFinishIsAnnouncedEvenWhenAnotherSeasonLeftTheLibrary() throws {
        let shown = Attention.review(try facts(finished: [older]), remembered: AttentionMemory())
        let swapped = Attention.review(try facts(finished: [newer]), remembered: shown.memory)
        XCTAssertEqual(swapped.notices.map(\.title), ["Futurama, Season 3 is finished"])
    }

    func testClearingAnItemClearsItsBadgeAndItCanNotifyAgainLater() throws {
        let waiting = try facts(waitingSamples: [("tv/E/Season 3", "ready_to_compare", "review_sample")], backups: 2)
        let shown = Attention.review(waiting, remembered: AttentionMemory())
        let cleared = Attention.review(try facts(), remembered: shown.memory)
        let back = Attention.review(waiting, remembered: cleared.memory)

        XCTAssertEqual(cleared.badgeCount, 0)
        XCTAssertEqual(cleared.notices, [])
        XCTAssertEqual(back.notices.map(\.title), ["The Expanse, Season 3 sample is ready", "Original backups are ready to delete"])
    }

    func testASampleThatCannotReachItsSizeAsksForASizeDecision() throws {
        let attention = Attention.review(
            try facts(waitingSamples: [("tv/E/Season 3", "needs_help", "choose_size")]),
            remembered: AttentionMemory()
        )
        XCTAssertEqual(attention.notices.map(\.title), ["The Expanse, Season 3 needs a size decision"])
    }

    func testFewerBackupsDoNotNotify() throws {
        let many = Attention.review(try facts(backups: 10), remembered: AttentionMemory())
        let fewer = Attention.review(try facts(backups: 4), remembered: many.memory)
        XCTAssertEqual(fewer.notices, [])
        XCTAssertEqual(fewer.badgeCount, 1)
    }

    func testProblemsTheOwnerCannotFixDoNotNotify() throws {
        let attention = Attention.review(try facts(computers: [("mini", "storage_unreachable"), ("x", "outside_schedule")]), remembered: AttentionMemory())
        XCTAssertEqual(attention.notices, [])
        XCTAssertEqual(attention.badgeCount, 0)
    }

    func testCountsReadNaturally() {
        XCTAssertEqual(Words.count(1, "episode"), "1 episode")
        XCTAssertEqual(Words.count(15, "episode"), "15 episodes")
    }
}
