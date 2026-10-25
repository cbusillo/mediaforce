import Foundation
import XCTest
@testable import Mediaforce

final class ItemTests: XCTestCase {
    private func facts(_ files: [String], sound: String = "null", airing: String = "null") throws -> ItemFacts {
        let json = #"""
        {"prefix": "tv/Futurama/Season 2", "domain": "tv", "kind": "tv_season", "title": "Futurama · Season 2",
         "show": "Futurama", "show_prefix": "tv/Futurama", "season": 2, "season_folder": "Season 2",
         "parent_prefix": "tv/Futurama", "files": [\#(files.joined(separator: ","))],
         "sound": \#(sound), "still_airing": \#(airing)}
        """#
        return try JSONDecoder.mediaforce.decode(ItemFacts.self, from: Data(json.utf8))
    }

    private func file(_ id: Int, stage: String, bytes: Int64, original: Int64? = nil, backup: Bool = false,
                      validation: String? = nil, kept: String? = nil, question: String = "null") -> String {
        let quoted = { (value: String?) in value.map { #""\#($0)""# } ?? "null" }
        return #"""
        {"library_item_id": \#(id), "rel_path": "tv/Futurama/Season 2/Futurama.S02E0\#(id).mkv",
         "name": "Futurama.S02E0\#(id).mkv", "episode": \#(id), "stage": "\#(stage)", "validation": \#(quoted(validation)),
         "kept_reason": \#(quoted(kept)), "held_reason": null, "percent_complete": null, "size_question": \#(question),
         "bytes": \#(bytes), "original_bytes": \#(original.map(String.init) ?? "null"), "original_backup": \#(backup)}
        """#
    }

    func testTheSummaryCountsWhatIsDoneKeptAndWaiting() throws {
        let season = try facts([
            file(1, stage: "published", bytes: 135, original: 271, backup: true),
            file(2, stage: "published", bytes: 136, original: 270),
            file(3, stage: "kept_original", bytes: 500, kept: "put_back"),
            file(7, stage: "needs_you", bytes: 232, validation: "failed"),
        ])

        XCTAssertEqual(Words.itemTitle(season), "Season 2")
        XCTAssertEqual(Words.itemSummary(season), "2 episodes compressed, 1 kept original, 1 waiting for you.")
        XCTAssertEqual(season.originalBytes, 271 + 270 + 500 + 232)
        XCTAssertEqual(season.waiting.map(\.libraryItemId), [7])
    }

    func testEachFileSaysWhyInPlainWords() throws {
        let season = try facts([
            file(3, stage: "kept_original", bytes: 500, kept: "put_back"),
            file(7, stage: "needs_you", bytes: 232, validation: "failed"),
        ])

        XCTAssertEqual(Words.fileState(season.files[0]), "Kept original: you put the original back")
        XCTAssertEqual(Words.fileState(season.files[1]), "Didn't pass its check")
        XCTAssertTrue(Words.waitingReason(season.files[1]).hasPrefix("The compressed copy didn't pass its check"))
    }

    func testASizeQuestionNamesBothSizes() throws {
        let question = #"{"job_id": "job-7", "rel_path": "x", "goal_bytes": 300000000, "smallest_quality_safe_bytes": 420000000, "reason": "quality_needs_more"}"#
        let season = try facts([file(7, stage: "needs_you", bytes: 900, question: question)])
        let waiting = try XCTUnwrap(season.waiting.first)

        XCTAssertEqual(waiting.sizeQuestion?.jobId, "job-7")
        XCTAssertEqual(Words.fileState(waiting), "Needs more than its size goal")
        XCTAssertEqual(Words.waitingReason(waiting), "To look right it needs 420 MB, more than its 300 MB goal.")
        XCTAssertEqual(Words.allowLarger(try XCTUnwrap(waiting.sizeQuestion)), "Allow 420 MB")
    }

    func testTheShowChoicesDecodeForTheSettingsSection() throws {
        let season = try facts(
            [file(1, stage: "published", bytes: 1)],
            sound: #"{"show_prefix": "tv/Futurama", "current": "speech_stereo", "options": ["keep_surround", "speech_stereo"]}"#,
            airing: #"{"series_prefix": "tv/Futurama", "mode": "auto", "airing": false}"#
        )

        XCTAssertEqual(season.sound?.options.map(Words.soundName), ["Film", "Speech-heavy"])
        let airing = try XCTUnwrap(season.stillAiring)
        XCTAssertTrue(Words.stillAiringExplanation(airing).hasSuffix("TMDB says it has ended."))
    }

    func testANotificationOpensThePageItIsAbout() {
        for place in [Place.item("tv/Futurama/Season 2"), .sample("tv/The Expanse/Season 3"), .library, .spaceSaved, .computers] {
            XCTAssertEqual(Place(notificationValue: place.notificationValue), place)
        }
        XCTAssertNil(Place(notificationValue: "web:/folders/tv"))
    }
}
