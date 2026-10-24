import Foundation
import XCTest
@testable import Mediaforce

final class AttentionTests: XCTestCase {
    private func facts(
        finished: [String] = [],
        waitingSamples: [(prefix: String, state: String)] = [],
        computers: [(key: String, state: String)] = [],
        backups: Int = 0
    ) throws -> MainWindowFacts {
        let finishedJSON = finished.map { prefix in
            #"{"prefix": "\#(prefix)", "show_prefix": "tv/Futurama", "show": "Futurama", "season": 3, "episode_count": 15, "bytes_saved": 4000000000, "finished_at": "2026-10-22T04:14:18+00:00"}"#
        }
        let samplesJSON = waitingSamples.map { sample in
            #"{"prefix": "\#(sample.prefix)", "show_prefix": "tv/Expanse", "show": "The Expanse", "season": 3, "state": "\#(sample.state)", "action": null, "failure": null}"#
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
         "space_saved": {"bytes": 0, "item_count": 0, "episode_count": 0, "season_count": 0},
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

    func testFirstLookNotifiesNothingButCountsTheBadge() throws {
        let attention = Attention.review(try facts(finished: ["tv/F/Season 3"], waitingSamples: [("tv/E/Season 3", "ready_to_compare")], backups: 4), remembered: nil)
        XCTAssertEqual(attention.notices, [])
        XCTAssertEqual(attention.badgeCount, 2)
    }

    func testEachNewThingNotifiesOnce() throws {
        let before = Attention.review(try facts(), remembered: nil).memory
        let now = try facts(
            finished: ["tv/F/Season 3"],
            waitingSamples: [("tv/E/Season 3", "ready_to_compare")],
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

    func testClearingAnItemClearsItsBadgeAndItCanNotifyAgainLater() throws {
        let waiting = try facts(waitingSamples: [("tv/E/Season 3", "ready_to_compare")], backups: 2)
        let shown = Attention.review(waiting, remembered: AttentionMemory())
        let cleared = Attention.review(try facts(), remembered: shown.memory)
        let back = Attention.review(waiting, remembered: cleared.memory)

        XCTAssertEqual(cleared.badgeCount, 0)
        XCTAssertEqual(cleared.notices, [])
        XCTAssertEqual(back.notices.map(\.title), ["The Expanse, Season 3 sample is ready", "Original backups are ready to delete"])
    }

    func testFewerBackupsDoNotNotify() throws {
        let many = Attention.review(try facts(backups: 10), remembered: AttentionMemory())
        let fewer = Attention.review(try facts(backups: 4), remembered: many.memory)
        XCTAssertEqual(fewer.notices, [])
        XCTAssertEqual(fewer.badgeCount, 1)
    }

    func testAFinishedSeasonNeverNotifiesTwice() throws {
        let shown = Attention.review(try facts(finished: ["tv/F/Season 3"]), remembered: AttentionMemory())
        let droppedOff = Attention.review(try facts(), remembered: shown.memory)
        let returned = Attention.review(try facts(finished: ["tv/F/Season 3"]), remembered: droppedOff.memory)
        XCTAssertEqual(returned.notices, [])
    }

    func testCountsReadNaturally() {
        XCTAssertEqual(Words.count(1, "episode"), "1 episode")
        XCTAssertEqual(Words.count(15, "episode"), "15 episodes")
    }

    func testProblemsTheOwnerCannotFixDoNotNotify() throws {
        let attention = Attention.review(try facts(computers: [("mini", "storage_unreachable"), ("x", "outside_schedule")]), remembered: AttentionMemory())
        XCTAssertEqual(attention.notices, [])
        XCTAssertEqual(attention.badgeCount, 0)
    }
}
