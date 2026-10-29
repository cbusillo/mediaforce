import Foundation
import XCTest
@testable import Mediaforce

final class AttentionTests: XCTestCase {
    private struct Finished {
        let prefix: String
        let at: String
        var replaced = 15
        var kept = 0
        var saved: Int64 = 4_000_000_000
    }

    private func facts(
        finished: [Finished] = [],
        waitingSamples: [(prefix: String, state: String, action: String)] = [],
        computers: [(key: String, state: String, needsYou: Bool)] = [],
        computerReason: String? = nil,
        computerSince: String? = nil,
        backups: Int = 0
    ) throws -> MainWindowFacts {
        let finishedJSON = finished.map { season in
            #"{"prefix": "\#(season.prefix)", "show_prefix": "tv/Futurama", "show": "Futurama", "season": 3, "episode_count": \#(season.replaced), "kept_original_count": \#(season.kept), "bytes_saved": \#(season.saved), "finished_at": "\#(season.at)"}"#
        }
        let keptJSON = finished.filter { $0.kept > 0 }.map { season in
            #"{"prefix": "\#(season.prefix)", "show_prefix": "tv/Futurama", "show": "Futurama", "season": 3, "reason": "motion_mixed", "item_count": \#(season.kept)}"#
        }
        let samplesJSON = waitingSamples.map { sample in
            #"{"prefix": "\#(sample.prefix)", "show_prefix": "tv/Expanse", "show": "The Expanse", "season": 3, "state": "\#(sample.state)", "action": "\#(sample.action)", "failure": null}"#
        }
        let reason = computerReason.map { #""\#($0)""# } ?? "null"
        let since = computerSince.map { #""\#($0)""# } ?? "null"
        let computersJSON = computers.map { computer in
            #"{"key": "\#(computer.key)", "name": "\#(computer.key)", "platform": "macos", "state": "\#(computer.state)", "running_count": 0, "schedule_opens_at": null, "attention_reason": \#(reason), "attention_since": \#(since), "needs_you": \#(computer.needsYou)}"#
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
         "kept_original": {"kept_item_count": 0, "kept": [\(keptJSON.joined(separator: ","))]}}
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

    func testASeasonWithFileDecisionsNotifiesAndCountsOnce() throws {
        let baseline = try facts()
        var value = try XCTUnwrap(JSONSerialization.jsonObject(with: Data(MainWindowTests.facts.utf8)) as? [String: Any])
        var work = try XCTUnwrap(value["work"] as? [String: Any])
        work["seasons_needing_you"] = [[
            "prefix": "tv/Futurama/Season 2", "season": 2, "state": "making_season", "action": NSNull(),
            "tone": "active", "episode_count": 3, "bytes": 1000, "needs_you": true,
        ]]
        value["work"] = work
        value["samples"] = ["waiting_on_you": [], "approved_not_started": []]
        value["computers"] = []
        value["original_backups"] = ["count": 0, "bytes": 0]
        value["recently_finished_seasons"] = []
        let changed = try JSONDecoder.mediaforce.decode(MainWindowFacts.self, from: JSONSerialization.data(withJSONObject: value))
        let first = Attention.review(changed, remembered: Attention.review(baseline, remembered: nil).memory)
        XCTAssertEqual(first.badgeCount, Attention.waitingItems(changed).count)
        XCTAssertEqual(first.badgeCount, 1)
        XCTAssertEqual(first.notices.first?.opens, .item("tv/Futurama/Season 2"))
        XCTAssertTrue(Attention.review(changed, remembered: first.memory).notices.isEmpty)
        var seasons = try XCTUnwrap(work["seasons_needing_you"] as? [[String: Any]])
        seasons[0]["state"] = "needs_help"
        seasons[0]["action"] = "retry_encode"
        work["seasons_needing_you"] = seasons
        value["work"] = work
        let newReason = try JSONDecoder.mediaforce.decode(MainWindowFacts.self, from: JSONSerialization.data(withJSONObject: value))
        let next = Attention.review(newReason, remembered: first.memory)
        XCTAssertEqual(next.notices.count, 1)
        XCTAssertEqual(next.badgeCount, first.badgeCount)
    }

    func testEachNewThingNotifiesOnce() throws {
        let before = Attention.review(try facts(finished: [older]), remembered: nil).memory
        let now = try facts(
            finished: [newer, older],
            waitingSamples: [("tv/E/Season 3", "ready_to_compare", "review_sample")],
            computers: [("mbp", "password_needed", true), ("mini", "storage_unreachable", false)],
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

    func testOneStorageIncidentNotifiesOnceAcrossDifferentFailedChecks() throws {
        let since = "2026-10-26T10:00:00Z"
        let first = Attention.review(try facts(
            computers: [("mini", "recovery_required", true)], computerReason: "Storage request is still running", computerSince: since
        ), remembered: AttentionMemory())
        XCTAssertEqual(first.notices.count, 1)
        let failedCheck = Attention.review(try facts(
            computers: [("mini", "check_failed", true)], computerReason: "Could not finish its check", computerSince: since
        ), remembered: first.memory)
        XCTAssertTrue(failedCheck.notices.isEmpty)
        XCTAssertEqual(failedCheck.badgeCount, 1)
        let storageAgain = Attention.review(try facts(
            computers: [("mini", "recovery_required", true)], computerReason: "Storage request is still running", computerSince: since
        ), remembered: failedCheck.memory)
        XCTAssertTrue(storageAgain.notices.isEmpty)
        let ready = Attention.review(try facts(computers: [("mini", "ready", false)]), remembered: storageAgain.memory)
        XCTAssertEqual(ready.badgeCount, 0)
        XCTAssertTrue(ready.memory.waiting.isEmpty)
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

    func testASeasonKeptEntirelyOriginalSaysWhyInsteadOfWhatWasSaved() throws {
        let allKept = Finished(prefix: "tv/F/Season 4", at: "2026-10-23T00:00:00+00:00", replaced: 0, kept: 3, saved: 0)
        let mixed = Finished(prefix: "tv/F/Season 5", at: "2026-10-22T12:00:00+00:00", replaced: 12, kept: 3)
        let before = Attention.review(try facts(finished: [older]), remembered: nil).memory

        let bodies = Attention.review(try facts(finished: [allKept, mixed, newer, older]), remembered: before).notices.map(\.body)

        guard bodies.count == 3 else { return XCTFail("Expected three finished notices, got \(bodies)") }
        let (keptBody, mixedBody, replacedBody) = (bodies[0], bodies[1], bodies[2])
        XCTAssertNotEqual(keptBody, replacedBody)
        XCTAssertTrue(keptBody.contains(Words.keptReason("motion_mixed")))
        XCTAssertFalse(keptBody.contains(Words.size(0)))
        XCTAssertTrue(mixedBody.contains("\(Words.count(3, "episode")) kept original"))
        XCTAssertFalse(replacedBody.contains("kept original"))
    }

    func testFinishedLatelyShowsKeptOriginalForASeasonThatSavedNothing() throws {
        let allKept = Finished(prefix: "tv/F/Season 4", at: "2026-10-23T00:00:00+00:00", replaced: 0, kept: 3, saved: 0)
        let seasons = try facts(finished: [allKept, newer]).recentlyFinishedSeasons

        XCTAssertEqual(Words.finishedSaved(seasons[0]), Words.keptOriginal)
        XCTAssertNotEqual(Words.finishedSaved(seasons[1]), Words.keptOriginal)
        XCTAssertFalse(Words.finishedSummary(seasons[0]).contains(Words.size(0)))
    }

    func testWaitingListMatchesBadgeWithoutACapAndClearsWithFacts() throws {
        let samples = (1...40).map { ("tv/E/Season \($0)", "ready_to_compare", "review_sample") }
        let waiting = try facts(waitingSamples: samples, computers: [("mini", "password_needed", true)], backups: 12)
        let rows = Attention.waitingItems(waiting)
        XCTAssertEqual(rows.count, samples.count + 2)
        XCTAssertEqual(rows.count, Attention.waitingOnYou(waiting))
        XCTAssertEqual(rows.count, Attention.review(waiting, remembered: nil).badgeCount)
        XCTAssertEqual(Set(rows.map(\.id)).count, rows.count)
        XCTAssertTrue(rows.allSatisfy { $0.opens != nil })
        XCTAssertEqual(Attention.openingPlace(waiting, fallback: .sample("tv/E/Season 1")), .needsYou)
        XCTAssertEqual(Attention.openingPlace(nil), .needsYou)
        let cleared = try facts()
        XCTAssertTrue(Attention.waitingItems(cleared).isEmpty)
        XCTAssertEqual(Attention.waitingOnYou(cleared), 0)
        XCTAssertEqual(Attention.openingPlace(cleared, fallback: .spaceSaved), .spaceSaved)
        XCTAssertEqual(Attention.openingPlace(cleared), .library)
        XCTAssertEqual(Place(notificationValue: Place.needsYou.notificationValue), .needsYou)
    }

    func testCountsReadNaturally() {
        XCTAssertEqual(Words.count(1, "episode"), "1 episode")
        XCTAssertEqual(Words.count(15, "episode"), "15 episodes")
    }
}
