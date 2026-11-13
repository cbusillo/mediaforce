import Foundation
import XCTest
@testable import Mediaforce

final class KindsTests: XCTestCase {
    private func facts(approval: String = "null") throws -> KindsFacts {
        try JSONDecoder.mediaforce.decode(KindsFacts.self, from: Data("""
        {"kinds": [{"kind": "live_action", "one_time": false, "seasons": 3, "waiting_bytes": 9000,
                    "shows": [{"prefix": "tv/Psych", "seasons": 2, "waiting_bytes": 6000, "sample_season": "tv/Psych/Season 3"},
                              {"prefix": "tv/Futurama", "seasons": 1, "waiting_bytes": 3000, "sample_season": "tv/Futurama/Season 1"}],
                    "settings": [{"show": "tv/Big Brother (US)", "from_prefix": "tv/Big Brother (US)", "size_goal_mode": "normalized",
                                  "size_goal_mb": 220.0, "library_item_id": 10441},
                                 {"show": "tv/Raising Hope", "from_prefix": "tv/Raising Hope/Season 2", "size_goal_mode": "normalized",
                                  "size_goal_mb": 300.0, "library_item_id": null}],
                    "approval": \(approval), "sample_season": "tv/Psych/Season 3"}],
         "unmeasured_seasons": 2, "folder_approved_seasons": 0, "tallest_picture": 1080,
         "left_out": [{"season": "tv/Suits/Season 4", "kind": "unmeasured", "waiting_bytes": 100},
                      {"season": "tv/Dollhouse/Season 1", "kind": "unmeasured", "waiting_bytes": 700}]}
        """.utf8))
    }

    func testThePagePlaysTheDecidedSettingElseTheFirstWithAnEpisode() throws {
        XCTAssertEqual(try facts().kinds[0].firstSetting?.name, "Big Brother (US)")
        let decided = try facts(approval: """
        {"from_prefix": "tv/Raising Hope/Season 2", "approved_at": "2026-10-26T15:00:00+00:00",
         "size_goal_mode": "normalized", "size_goal_mb": 300.0, "library_item_id": null}
        """).kinds[0]
        XCTAssertEqual(decided.firstSetting?.show, "tv/Raising Hope")
        XCTAssertEqual(decided.approval?.showPrefix, "tv/Raising Hope")
    }

    func testALooksApprovalSaysWhether4KStays4K() throws {
        let kept = try facts(approval: """
        {"from_prefix": "tv/Raising Hope/Season 2", "approved_at": "now", "keep_full_height": true, "library_item_id": null}
        """)
        XCTAssertEqual(kept.tallestPicture, 1080)
        XCTAssertEqual(kept.kinds[0].approval?.keepFullHeight, true)
        let older = try JSONDecoder.mediaforce.decode(KindsFacts.self, from: Data(#"{"kinds": [], "left_out": []}"#.utf8))
        XCTAssertNil(older.tallestPicture)
    }

    func testAMovieTitleIsToldApartFromAShow() throws {
        let movie = KindsFacts.Show(prefix: "movies/Heat (1995)", seasons: 1, waitingBytes: 1, sampleSeason: "movies/Heat (1995)")
        XCTAssertTrue(movie.isMovie)
        XCTAssertFalse(try facts().kinds[0].shows.contains(where: \.isMovie))
    }

    func testTheNewestSamplesSettingComesBeforeAnOlderWatchableShow() throws {
        let older = try facts().kinds[0].settings[0]
        let latest = KindsFacts.Setting(show: "tv/The Blacklist", fromPrefix: "tv/The Blacklist/Season 10",
                                       sizeGoalMode: "normalized", sizeGoalMb: 300, libraryItemId: nil)
        let pending = KindsFacts.Kind(kind: "live_action", oneTime: false, seasons: 1, waitingBytes: 1000,
                                     shows: [], settings: [older, latest], approval: nil, sampleSeason: latest.fromPrefix)
        XCTAssertEqual(pending.sampleSetting, latest)
        XCTAssertEqual(pending.firstSetting, latest)
        XCTAssertTrue(pending.showsSampleFirst)
        var unrelated = KindsFacts.Kind(kind: "live_action", oneTime: false, seasons: 1, waitingBytes: 1000,
                                       shows: [], settings: [older], approval: nil, sampleSeason: latest.fromPrefix)
        XCTAssertFalse(unrelated.showsSampleFirst)
        unrelated.sampleReady = false
        unrelated.sampleStatus = "pending_review"
        XCTAssertTrue(unrelated.offersSample)
        unrelated.sampleReady = true
        XCTAssertFalse(unrelated.offersSample)
    }

    func testTheApprovedKindCarriesItsOwnSavedSampleClips() throws {
        let decided = try facts(approval: """
        {"from_prefix": "tv/Raising Hope/Season 2", "approved_at": "now", "library_item_id": null,
         "sample_moments": [{"at_seconds": 600, "original": {"url": "/watched/source.mp4", "size_bytes": 1000},
                             "sample": {"url": "/watched/sample.mp4", "size_bytes": 100}}]}
        """).kinds[0]
        XCTAssertEqual(decided.approval?.sampleMoments?.first?.sample.url, "/watched/sample.mp4")
        XCTAssertNil(try facts().kinds[0].approval)
    }

    func testAKindNamesTheShowWhoseSampleIsBeingMadeAndWhereEachShowsSampleWouldBe() throws {
        let kind = try facts().kinds[0]
        XCTAssertEqual(kind.sampleShow, "tv/Psych")
        XCTAssertEqual(kind.shows.map(\.sampleSeason), ["tv/Psych/Season 3", "tv/Futurama/Season 1"])
        XCTAssertEqual(Words.makeSampleOf(kind.shows[0].name), "Make a Sample of Psych")
    }

    func testTheSeasonsNoKindCoversListOneRowEachWithTheirShow() throws {
        let rows = MediaRow.seasons(try facts().leftOut, shows: [
            MediaRow(id: "tv/Dollhouse", name: "Dollhouse", kind: .tv, detail: "", bytes: 0, saved: nil, state: .needsYou),
        ])
        XCTAssertEqual(rows.map(\.id), ["tv/Dollhouse/Season 1", "tv/Suits/Season 4"])
        XCTAssertTrue(rows[0].name.contains("Dollhouse") && rows[0].name.contains("Season 1"))
        XCTAssertEqual(rows.map(\.state), [.needsYou, .readyToCompress])
        // A season shows its show's poster.
        XCTAssertEqual(rows.map(\.posterPrefix), ["tv/Dollhouse", "tv/Suits"])
    }

    func testASettingSaysItsOwnSizeAndWhetherItScalesWithRuntime() throws {
        let perFortyFive = try facts().kinds[0].settings[0]
        let perEpisode = KindsFacts.Setting(show: "tv/X", fromPrefix: "tv/X", sizeGoalMode: "absolute", sizeGoalMb: 220, libraryItemId: nil)
        XCTAssertTrue(Words.sizeSetting(perFortyFive).contains("220"))
        XCTAssertNotEqual(Words.sizeSetting(perFortyFive), Words.sizeSetting(perEpisode))
    }

    func testEveryRefusalCodeHasItsOwnWords() {
        let codes = ["approval_not_current", "size_goal_not_scalable", "source_kind_differs", "no_folder_approval", "kind_is_one_time"]
        XCTAssertEqual(Set(codes.map(Words.kindRefusal)).count, codes.count)
        XCTAssertFalse(codes.contains { Words.kindRefusal($0) == Words.kindRefusal("unknown") })
    }
}
