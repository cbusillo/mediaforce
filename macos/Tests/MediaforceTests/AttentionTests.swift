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
        kinds: [(kind: String, sampleSeason: String?)] = [],
        kindStatus: String? = nil,
        kindReady: Bool? = nil,
        startPrefix: String? = nil,
        oneTime: Bool = false,
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
        let kindsJSON = kinds.map { kind in
            let season = kind.sampleSeason.map { #""\#($0)""# } ?? "null"
            let status = kindStatus.map { #""\#($0)""# } ?? "null"
            let ready = kindReady.map { String($0) } ?? "null"
            let start = startPrefix.map { #""\#($0)""# } ?? "null"
            return #"{"kind": "\#(kind.kind)", "sample_season": \#(season), "sample_status": \#(status), "sample_ready": \#(ready), "sample_start_prefix": \#(start), "one_time": \#(oneTime)}"#
        }
        let reason = computerReason.map { #""\#($0)""# } ?? "null"
        let since = computerSince.map { #""\#($0)""# } ?? "null"
        let computersJSON = computers.map { computer in
            #"{"key": "\#(computer.key)", "name": "\#(computer.key)", "platform": "macos", "state": "\#(computer.state)", "running_count": 0, "schedule_opens_at": null, "attention_reason": \#(reason), "attention_since": \#(since), "needs_you": \#(computer.needsYou)}"#
        }
        let json = """
        {"observed_at": "2026-10-24T12:00:00+00:00",
         "work": {"state": "idle", "paused_by_owner": false, "stop_reasons": [], "running_count": 0, "waiting_count": 0, "kinds_needing_you": [\(kindsJSON.joined(separator: ","))]},
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

    func testCancelledOriginalWaitIsVisibleAndNotifiesOnlyWhenAProblemNeedsAttention() throws {
        let remembered = Attention.review(try facts(), remembered: nil).memory
        var preview = try originalApprovalFixture()
        preview.waiting = "Waiting for checks to be started again"
        var current = try facts()
        current.originalPreviews = [preview]
        let waiting = Attention.review(current, remembered: remembered)
        XCTAssertEqual(waiting.badgeCount, 0)
        XCTAssertTrue(waiting.notices.isEmpty)
        XCTAssertEqual(Attention.waitingItems(current).map(\.body), [try XCTUnwrap(preview.waiting)])
        preview.problems = [OriginalProblem(itemId: nil, relPath: nil, reason: "The original-file checks need attention")]
        current.originalPreviews = [preview]
        let problem = Attention.review(current, remembered: waiting.memory)
        XCTAssertEqual(problem.badgeCount, 1)
        XCTAssertEqual(problem.notices.map(\.opens), [.needsYou])
    }

    func testAKindDecisionNotifiesOnceCountsTheBadgeAndOpensTheKind() throws {
        let before = Attention.review(try facts(), remembered: nil).memory
        let pending = try facts(kinds: [("darker_live_action", "tv/The Blacklist/Season 10")])
        let first = Attention.review(pending, remembered: before)
        XCTAssertEqual(first.badgeCount, 1)
        let sampleWaiting = try facts(waitingSamples: [("tv/E/Season 3", "ready_to_compare", "review_sample")])
        XCTAssertEqual(Words.workHeadline(pending), Words.workHeadline(sampleWaiting))
        XCTAssertNotEqual(Words.workHeadline(pending), Words.workHeadline(try facts()))
        XCTAssertNotNil(Words.workExplanation(pending))
        XCTAssertEqual(first.notices.map(\.opens), [.kind("darker_live_action")])
        let destination = try XCTUnwrap(first.notices.first?.opens)
        let decoded = try XCTUnwrap(Place(notificationValue: destination.notificationValue))
        XCTAssertEqual(decoded, destination)
        XCTAssertEqual(Attention.openingPlace(pending, fallback: decoded), .needsYou)
        XCTAssertEqual(Attention.openingPlace(try facts(), fallback: decoded), destination)
        XCTAssertEqual(Attention.waitingItems(pending), first.notices)
        XCTAssertTrue(Attention.review(pending, remembered: first.memory).notices.isEmpty)
        XCTAssertEqual(Attention.review(try facts(), remembered: first.memory).badgeCount, 0)
    }

    func testFirstLookNotifiesNothingButCountsTheBadge() throws {
        let now = try facts(finished: [older], waitingSamples: [("tv/E/Season 3", "ready_to_compare", "review_sample")], backups: 4)
        let attention = Attention.review(now, remembered: nil)
        XCTAssertEqual(attention.notices, [])
        XCTAssertEqual(attention.badgeCount, 2)
    }

    func testAKindWithoutASampleStaysReachableAndNotifiesWhenReady() throws {
        let before = try facts(kinds: [("bright_live_action", nil)], kindStatus: "not_started", kindReady: false)
        let waiting = Attention.review(before, remembered: nil)
        XCTAssertEqual(waiting.badgeCount, 0)
        XCTAssertTrue(Attention.review(before, remembered: Attention.review(try facts(), remembered: nil).memory).notices.isEmpty)
        XCTAssertEqual(Attention.waitingItems(before).map(\.opens), [.kind("bright_live_action")])
        let queued = try facts(kinds: [("bright_live_action", "tv/Psych/Season 3")], kindStatus: "queued", kindReady: false)
        let making = try facts(kinds: [("bright_live_action", "tv/Psych/Season 3")], kindStatus: "running", kindReady: false)
        XCTAssertTrue(Attention.review(queued, remembered: waiting.memory).notices.isEmpty)
        XCTAssertTrue(Attention.review(making, remembered: Attention.review(queued, remembered: waiting.memory).memory).notices.isEmpty)
        let failed = try facts(kinds: [("bright_live_action", "tv/Psych/Season 3")], kindStatus: "failed", kindReady: false)
        XCTAssertEqual(Attention.review(failed, remembered: Attention.review(making, remembered: waiting.memory).memory).notices.count, 1)
        let ready = try facts(kinds: [("bright_live_action", "tv/Psych/Season 3")], kindStatus: "pending_review", kindReady: true)
        let after = Attention.review(ready, remembered: waiting.memory)
        XCTAssertEqual(after.badgeCount, 1)
        XCTAssertEqual(after.notices.map(\.opens), [.kind("bright_live_action")])
        XCTAssertNotEqual(Attention.waitingItems(before).first?.body, after.notices.first?.body)
        let cartoons = try facts(kinds: [("cartoon", "tv/Futurama/Season 1")], kindStatus: "pending_review", kindReady: true, oneTime: true)
        XCTAssertEqual(Attention.waitingOnYou(cartoons), 1)
        XCTAssertNotEqual(Attention.waitingItems(cartoons).first?.body, Attention.waitingItems(before).first?.body)
    }

    func testAFirstSampleChoiceIsBadgedAndOpensTheSeasonThatNeedsIt() throws {
        let retrying = try facts(kinds: [("bright_live_action", nil)], kindStatus: "retry_backoff", kindReady: false)
        let choice = try facts(kinds: [("bright_live_action", nil)], kindStatus: "choose_size", kindReady: false,
                               startPrefix: "tv/Psych/Season 3")
        let after = Attention.review(choice, remembered: Attention.review(retrying, remembered: nil).memory)
        XCTAssertEqual(after.badgeCount, 1)
        XCTAssertEqual(after.notices.map(\.opens), [.sample("tv/Psych/Season 3")])
        XCTAssertTrue(Attention.review(choice, remembered: after.memory).notices.isEmpty)
        let unsuitable = try facts(waitingSamples: [("tv/Psych/Season 3", "ready_to_compare", "review_sample")],
                                   kinds: [("bright_live_action", "tv/Psych/Season 3")], kindStatus: "pending_review", kindReady: false)
        XCTAssertEqual(Attention.waitingItems(unsuitable).count, 2)
        let noticed = Attention.review(unsuitable, remembered: Attention.review(try facts(), remembered: nil).memory)
        XCTAssertEqual(noticed.badgeCount, 1)
        XCTAssertEqual(noticed.notices.count, 1)
    }

    func testAKindsButtonNamesWhatItAsksNotWhereItOpens() throws {
        let sizeChoice = try facts(kinds: [("bright_live_action", nil)], kindStatus: "choose_size", kindReady: false,
                                   startPrefix: "tv/Psych/Season 3")
        let size = try XCTUnwrap(sizeChoice.work.kindsNeedingYou.first)
        XCTAssertEqual(Words.kindAction(size), Words.sampleAction("choose_size"))
        let ready = try facts(kinds: [("bright_live_action", "tv/Psych/Season 3")], kindStatus: "pending_review", kindReady: true)
        XCTAssertEqual(Words.kindAction(try XCTUnwrap(ready.work.kindsNeedingYou.first)), Words.sampleAction("review_sample"))
        let blocked = try facts(kinds: [("bright_live_action", nil)], kindStatus: "sample_blocked", kindReady: false,
                                startPrefix: "tv/Psych/Season 3")
        XCTAssertEqual(Words.kindAction(try XCTUnwrap(blocked.work.kindsNeedingYou.first)), Words.sampleAction("make_sample"))
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

    @MainActor
    func testAShowCardGivesAReasonOnlyWhenItAddsOne() throws {
        let ready = try facts(waitingSamples: [(prefix: "tv/E/Season 3", state: "ready_to_compare", action: "review_sample")])
        XCTAssertNil(WaitingShow(try XCTUnwrap(Attention.waitingItems(ready).first), facts: ready).description)
        let failed = try JSONDecoder.mediaforce.decode(MainWindowFacts.self, from: Data(MainWindowTests.facts.utf8))
        let item = try XCTUnwrap(Attention.waitingItems(failed).first { $0.opens == .sample("tv/Lucifer/Season 2") })
        XCTAssertEqual(WaitingShow(item, facts: failed).description, Words.sampleFailure("missing_clips"))
    }

    func testNeedsYouPutsKindsFirstAndDrawsEveryWaitingItem() throws {
        let waiting = try facts(waitingSamples: [(prefix: "tv/E/Season 3", state: "ready_to_compare", action: "review_sample")],
                                computers: [(key: "mini", state: "password_needed", needsYou: true)],
                                kinds: [(kind: "cartoon", sampleSeason: "tv/F/Season 1")], backups: 3)
        let groups = WaitingGroups(waiting)
        XCTAssertEqual(groups.first, groups.kinds.first)
        XCTAssertEqual(groups.kinds.count, 1)
        XCTAssertEqual(groups.shows.map(\.opens), [.sample("tv/E/Season 3")])
        let drawn = groups.kinds + groups.shows + groups.computers + [groups.backups].compactMap { $0 }
        XCTAssertEqual(Set(drawn.map(\.id)), Set(Attention.waitingItems(waiting).map(\.id)))
        XCTAssertTrue(WaitingGroups(try facts()).isEmpty)
    }

    func testNeedsYouStatusDoesNotPointAtItsOwnItems() throws {
        let waiting = try facts(kinds: [(kind: "cartoon", sampleSeason: "tv/F/Season 1")])
        XCTAssertNotNil(Words.workExplanation(waiting))
        XCTAssertNil(Words.workExplanation(waiting, onNeedsYou: true))
    }

    func testCountsReadNaturally() {
        XCTAssertEqual(Words.count(1, "episode"), "1 episode")
        XCTAssertEqual(Words.count(15, "episode"), "15 episodes")
    }
}
