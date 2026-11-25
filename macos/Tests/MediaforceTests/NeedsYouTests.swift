import Foundation
import XCTest
@testable import Mediaforce

@MainActor
final class NeedsYouTests: XCTestCase {
    private static let kinds = """
    {"kinds": [{"kind": "live_action", "one_time": false, "seasons": 3, "waiting_bytes": 900000000000,
                "shows": [{"prefix": "tv/Psych", "seasons": 2, "waiting_bytes": 600000000000, "sample_season": "tv/Psych/Season 3"},
                          {"prefix": "tv/Monk", "seasons": 1, "waiting_bytes": 300000000000, "sample_season": "tv/Monk/Season 1"}],
                "settings": [{"show": "tv/Big Brother (US)", "from_prefix": "tv/Big Brother (US)", "size_goal_mode": "normalized",
                              "size_goal_mb": 220.0, "library_item_id": 10441}],
                "approval": null, "sample_season": null},
               {"kind": "cartoon", "one_time": true, "seasons": 1, "waiting_bytes": 5000000000,
                "shows": [{"prefix": "tv/Futurama", "seasons": 1, "waiting_bytes": 5000000000, "sample_season": "tv/Futurama/Season 3"}],
                "settings": [], "approval": null, "sample_season": null}],
     "left_out": []}
    """

    /// The shared main-window facts (a sample to make again, a computer needing its password, 46 backups), with two groups
    /// of shows and a season whose files need the Director added.
    private func facts(askGenreSound: Bool = false) throws -> MainWindowFacts {
        var value = try XCTUnwrap(JSONSerialization.jsonObject(with: Data(MainWindowTests.facts.utf8)) as? [String: Any])
        var work = try XCTUnwrap(value["work"] as? [String: Any])
        work["needs_genre_sound_decision"] = askGenreSound
        work["kinds_needing_you"] = [
            ["kind": "live_action", "sample_season": NSNull(), "sample_status": "not_started", "sample_ready": false,
             "one_time": false, "settings_available": true],
            ["kind": "cartoon", "sample_season": NSNull(), "sample_status": "choose_size", "sample_ready": false, "one_time": true,
             "sample_start_prefix": "tv/Futurama/Season 3", "settings_available": false],
        ]
        work["seasons_needing_you"] = [[
            "prefix": "tv/Suits/Season 6", "season": 6, "state": "needs_help", "action": "review_files",
            "tone": "attention", "episode_count": 16, "bytes": 7000000000, "needs_you": true,
        ]]
        value["work"] = work
        return try JSONDecoder.mediaforce.decode(MainWindowFacts.self, from: JSONSerialization.data(withJSONObject: value))
    }

    private func kinds() throws -> KindsFacts {
        try JSONDecoder.mediaforce.decode(KindsFacts.self, from: Data(Self.kinds.utf8))
    }

    private func file(_ validation: String?, question: String = "null", additional: String = "[]") throws -> ItemFacts.File {
        let validationJSON = validation.map { #""\#($0)""# } ?? "null"
        return try JSONDecoder.mediaforce.decode(ItemFacts.File.self, from: Data(#"""
        {"library_item_id": 15, "rel_path": "tv/Suits/Season 6/Suits.S06E15.mkv", "name": "Suits.S06E15.mkv", "season": 6,
         "episode": 15, "stage": "needs_you", "validation": \#(validationJSON), "wait_reason": null,
         "additional_wait_reasons": \#(additional), "kept_reason": null, "held_reason": null, "percent_complete": null,
         "size_question": \#(question), "bytes": 3500000000, "original_bytes": null, "original_backup": false}
        """#.utf8))
    }

    private func answers(_ ask: NeedsYouAsk) -> [ItemAction] {
        ask.answers.compactMap { answer in
            if case .file(let action, _, _) = answer.action { action } else { nil }
        }
    }

    func testMotionQuestionOffersBothAnswersAndKeepsSettledFilesOutOfTheGroup() throws {
        let season = "tv/Suits/Season 6"
        let rows: [[String: Any]] = [(1, true, false), (2, false, false), (3, true, true)].map { id, safe, kept in
            ["library_item_id": id, "rel_path": "\(season)/\(id).mkv", "name": "\(id).mkv", "stage": kept ? "kept_original" : "held",
             "bytes": 100, "original_backup": false, "can_compress_as_is": safe,
             "held_reason": kept ? NSNull() as Any : "motion_pattern", "kept_reason": kept ? "unclear" as Any : NSNull()]
        }
        let data = try JSONSerialization.data(withJSONObject: ["prefix": season, "domain": "tv", "kind": "tv_season",
                                                               "title": "Season 6", "files": rows])
        let item = try JSONDecoder.mediaforce.decode(ItemFacts.self, from: data)
        let answers = NeedsYouItem.items(try facts(), kinds: nil, files: [season: item]).flatMap(\.asks).flatMap(\.answers)
        XCTAssertEqual(answers.compactMap { if case .motion(_, let compress) = $0.action { compress } else { nil } }, [true, false])
        XCTAssertEqual(item.motionFiles.filter(\.needsMotionChoice).map(\.id), [1])
        XCTAssertEqual(item.motionFiles.map(\.id), [1, 2])
    }

    func testBackupsRowAndItsButtonOpenTheCleanupSection() throws {
        let row = try XCTUnwrap(NeedsYouItem.items(try facts(), kinds: nil).first { $0.id == Attention.backupsNoticeID })
        let destination = try XCTUnwrap(row.opens)
        XCTAssertEqual(destination, .originalBackups)
        guard case .open(let buttonDestination) = row.asks.first?.answers.first?.action else {
            return XCTFail("Missing backup review button")
        }
        XCTAssertEqual(buttonDestination, destination)
    }

    func testTheOneTimeSoundQuestionCountsOnceAndOffersBothAnswers() throws {
        let before = try facts(askGenreSound: true)
        let after = try facts()
        XCTAssertEqual(Attention.waitingOnYou(before), Attention.waitingOnYou(after) + 1)
        let answers = NeedsYouItem.items(before, kinds: nil).flatMap(\.asks).flatMap(\.answers)
        XCTAssertEqual(answers.compactMap { if case .genreSound(let enabled) = $0.action { enabled } else { nil } }, [true, false])
    }

    func testThePageDrawsWhatTheBadgeCountsWithWhatStopsWorkFirstThenTheMostSpace() throws {
        let facts = try facts()
        let items = NeedsYouItem.items(facts, kinds: try kinds())
        XCTAssertEqual(items.count, Attention.waitingOnYou(facts))
        XCTAssertEqual(Set(items.map(\.id)), Set(Attention.waitingItems(facts).filter(\.notifies).map(\.id)))
        XCTAssertEqual(items.first?.id, Attention.waitingItems(facts).first { $0.opens == .computer("mini") }?.id)
        let sized = items.dropFirst().compactMap(\.bytes)
        XCTAssertEqual(sized, sized.sorted(by: >))
        XCTAssertTrue(items.allSatisfy { !$0.asks.isEmpty && $0.asks.allSatisfy { !$0.answers.isEmpty } })
        XCTAssertTrue(NeedsYouItem.items(try JSONDecoder.mediaforce.decode(MainWindowFacts.self, from: Data("""
        {"observed_at": "2026-10-24T12:00:00+00:00",
         "work": {"state": "idle", "paused_by_owner": false, "stop_reasons": [], "running_count": 0, "waiting_count": 0},
         "library": {"tv": {"show_count": 0, "season_count": 0, "episode_count": 0, "total_bytes": 0, "original_bytes": 0},
                     "movies": {"title_count": 0, "total_bytes": 0, "original_bytes": 0},
                     "other": {"item_count": 0, "total_bytes": 0, "original_bytes": 0}},
         "space_saved": {"bytes": 0, "item_count": 0, "episode_count": 0, "season_count": 0},
         "recently_finished_seasons": [], "biggest_not_started": [],
         "samples": {"waiting_on_you": [], "approved_not_started": []}, "computers": [], "events": [], "last_change": null,
         "original_backups": {"count": 0, "bytes": 0}, "kept_original": {"kept_item_count": 0}}
        """.utf8)), kinds: nil).isEmpty)
    }

    func testAGroupWithAnApprovedShowIsAnsweredOnItsRowWithThatShowsSettings() throws {
        let kinds = try kinds()
        let group = try XCTUnwrap(NeedsYouItem.items(try facts(), kinds: kinds).first { $0.id.hasPrefix("kind:live_action:") })
        let answer = try XCTUnwrap(group.asks.first?.answers.first)
        guard case .useSettings(let kind, let from) = answer.action else { return XCTFail("Not answered on its row") }
        XCTAssertEqual(kind, "live_action")
        XCTAssertEqual(from, kinds.kinds.first?.firstSetting?.fromPrefix)
        XCTAssertEqual(group.bytes, kinds.kinds.first?.waitingBytes)
    }

    func testAFirstSampleSizeIsChosenOnTheSamplePageItNeeds() throws {
        let row = try XCTUnwrap(NeedsYouItem.items(try facts(), kinds: try kinds()).first { $0.id.hasPrefix("kind:cartoon:") })
        let answers = try XCTUnwrap(row.asks.first).answers
        XCTAssertEqual(answers.count, 1)
        guard case .open(let place) = try XCTUnwrap(answers.first).action else { return XCTFail("Not opened") }
        XCTAssertEqual(place, .sample("tv/Futurama/Season 3"))
    }

    func testEachFileQuestionIsAnsweredOnItsRow() throws {
        let season = "tv/Suits/Season 6"
        XCTAssertEqual(answers(NeedsYouItem.fileAsk(try file("size_held"), season: season)), [.keepThisCopy, .makeAgain])
        let question = #"{"job_id": "j1", "goal_bytes": 800000000, "smallest_quality_safe_bytes": 1200000000}"#
        XCTAssertEqual(answers(NeedsYouItem.fileAsk(try file(nil, question: question), season: season)), [.keepOriginal, .allowLarger])
        let held = try file(nil, additional: #"[{"code": "containment_unproven", "computer": "M2 MBP"}]"#)
        XCTAssertEqual(answers(NeedsYouItem.fileAsk(held, season: season)), [.releaseHold])
    }

    func testAFileTheDirectorJudgesIsWatchedFirstInItsSeasonPlayer() throws {
        let season = "tv/Suits/Season 6"
        let question = #"{"job_id": "j1", "goal_bytes": 800000000, "smallest_quality_safe_bytes": 1200000000}"#
        for file in [try file("size_held"), try file(nil, question: question)] {
            let watch = try XCTUnwrap(NeedsYouItem.fileAsk(file, season: season).look, "\(file.validation ?? "size question") has no Watch")
            XCTAssertEqual(watch.title, Words.watchIt)
            guard case .open(let place) = watch.action else { return XCTFail("Watch does not open a page") }
            var navigation = PageNavigation()
            navigation.select(.needsYou)
            navigation.open(place)
            XCTAssertEqual(navigation.place, .item(season, file: file.id))
            XCTAssertEqual(navigation.back, .needsYou)
        }
        // Releasing a hold is not a judgment of the file, so it has nothing to watch.
        XCTAssertNil(NeedsYouItem.fileAsk(try file(nil, additional: #"[{"code": "containment_unproven"}]"#), season: season).look)
    }

    func testTheSeasonPageOpensWithTheFileItWasAskedToShowInThePlayer() throws {
        let waiting = { (id: Int) in
            #"""
            {"library_item_id": \#(id), "rel_path": "tv/Suits/Season 6/Suits.S06E\#(id).mkv", "name": "Suits.S06E\#(id).mkv",
             "episode": \#(id), "stage": "needs_you", "validation": "size_held", "wait_reason": {"code": "size_held"},
             "additional_wait_reasons": [], "kept_reason": null, "held_reason": null, "percent_complete": null,
             "size_question": null, "bytes": 1, "original_bytes": null, "original_backup": false}
            """#
        }
        let season = try JSONDecoder.mediaforce.decode(ItemFacts.self, from: Data(#"""
        {"prefix": "tv/Suits/Season 6", "domain": "tv", "kind": "tv_season", "title": "Suits · Season 6", "show": "Suits",
         "show_prefix": "tv/Suits", "season": 6, "parent_prefix": "tv/Suits", "sound": null, "still_airing": null,
         "files": [\#(waiting(14)), \#(waiting(15))]}
        """#.utf8))
        let model = MainWindowModel(client: MediaforceClient(baseURL: ServerConnection.localServer))
        let page = ItemPage(prefix: season.prefix, model: model, openSample: { _ in }, openItem: { _ in }, selected: 15)
        // Without it the page would open on the first file waiting.
        XCTAssertEqual(season.selectedFile(page.selected)?.id, 15)
    }

    func testASeasonRowAsksEachWaitingFileOnceItsFilesAreRead() throws {
        let facts = try facts()
        let prefix = "tv/Suits/Season 6"
        let unread = try XCTUnwrap(NeedsYouItem.items(facts, kinds: nil).first { $0.id.hasPrefix("season:\(prefix):") })
        XCTAssertEqual(unread.asks.count, 1)
        let files = try JSONDecoder.mediaforce.decode(ItemFacts.self, from: Data(#"""
        {"prefix": "tv/Suits/Season 6", "domain": "tv", "kind": "tv_season", "title": "Suits · Season 6", "show": "Suits",
         "show_prefix": "tv/Suits", "season": 6, "parent_prefix": "tv/Suits", "sound": null, "still_airing": null, "files": [
          {"library_item_id": 14, "rel_path": "tv/Suits/Season 6/Suits.S06E14.mkv", "name": "Suits.S06E14.mkv", "episode": 14,
           "stage": "published", "validation": null, "wait_reason": null, "additional_wait_reasons": [], "kept_reason": null,
           "held_reason": null, "percent_complete": null, "size_question": null, "bytes": 1, "original_bytes": 9, "original_backup": true},
          {"library_item_id": 15, "rel_path": "tv/Suits/Season 6/Suits.S06E15.mkv", "name": "Suits.S06E15.mkv", "episode": 15,
           "stage": "needs_you", "validation": "size_held", "wait_reason": {"code": "size_held"}, "additional_wait_reasons": [],
           "kept_reason": null, "held_reason": null, "percent_complete": null, "size_question": null, "bytes": 3500000000,
           "original_bytes": null, "original_backup": false}]}
        """#.utf8))
        let read = try XCTUnwrap(NeedsYouItem.items(facts, kinds: nil, files: [prefix: files]).first { $0.id == unread.id })
        XCTAssertEqual(read.asks.count, files.waiting.count)
        XCTAssertEqual(answers(try XCTUnwrap(read.asks.first)), [.keepThisCopy, .makeAgain])
        XCTAssertEqual(read.bytes, files.waiting.reduce(0) { $0 + $1.bytes })
    }
}
