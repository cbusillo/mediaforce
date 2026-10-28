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
                      validation: String? = nil, kept: String? = nil, question: String = "null",
                      waitReason: String = "null") -> String {
        let quoted = { (value: String?) in value.map { #""\#($0)""# } ?? "null" }
        return #"""
        {"library_item_id": \#(id), "rel_path": "tv/Futurama/Season 2/Futurama.S02E0\#(id).mkv",
         "name": "Futurama.S02E0\#(id).mkv", "episode": \#(id), "stage": "\#(stage)", "validation": \#(quoted(validation)),
         "wait_reason": \#(waitReason), "kept_reason": \#(quoted(kept)), "held_reason": null, "percent_complete": null, "size_question": \#(question),
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

    func testEachWaitSaysWhyInPlainWords() throws {
        let notMadeAgain = { (blockers: String) in #"{"code": "not_made_again", "blockers": [\#(blockers)]}"# }
        let season = try facts([
            file(1, stage: "needs_you", bytes: 1),
            file(2, stage: "needs_you", bytes: 1, waitReason: #"{"code": "something_new"}"#),
            file(3, stage: "needs_you", bytes: 1, waitReason: #"{"code": "stopped"}"#),
            file(4, stage: "needs_you", bytes: 1, waitReason: #"{"code": "storage_io"}"#),
            file(5, stage: "needs_you", bytes: 1, waitReason: #"{"code": "restart_stopped", "computer": "Attic Mini"}"#),
            file(6, stage: "needs_you", bytes: 1, waitReason: notMadeAgain("")),
            file(7, stage: "needs_you", bytes: 1, waitReason: notMadeAgain(#""approval_missing""#)),
            file(8, stage: "needs_you", bytes: 1, waitReason: notMadeAgain(#""original_unreachable""#)),
            file(9, stage: "needs_you", bytes: 1, waitReason: notMadeAgain(#""approval_missing", "original_unreachable""#)),
        ])
        let label = season.files.map(Words.fileState)
        let reason = season.files.map(Words.waitingReason)

        XCTAssertFalse((label + reason).contains(where: \.isEmpty))
        // A reason this app does not know yet reads like a file with no reason at all.
        XCTAssertEqual(label[1], label[0])
        XCTAssertEqual(reason[1], reason[0])
        for known in 2...5 {
            XCTAssertNotEqual(label[known], label[0])
            XCTAssertNotEqual(reason[known], reason[0])
        }
        XCTAssertNotEqual(reason[2], reason[3])
        XCTAssertTrue(reason[4].contains("Attic Mini"))
        // Each blocker shows in the sentence, so two blockers read unlike either one alone or none.
        XCTAssertEqual(Set(reason[5...8]).count, 4)
    }

    func testStoppedPublishingSaysWhyItNeedsAttention() throws {
        let season = try facts([
            file(1, stage: "needs_you", bytes: 100, waitReason: #"{"code": "publish_failed"}"#),
            file(2, stage: "needs_you", bytes: 100, waitReason: #"{"code": "publish_unsafe"}"#),
            file(3, stage: "published", bytes: 100),
        ])

        XCTAssertEqual(season.waiting.map(\.libraryItemId), [1, 2])
        XCTAssertTrue(Words.fileState(season.files[0]).localizedCaseInsensitiveContains("publish"))
        XCTAssertTrue(Words.waitingReason(season.files[0]).contains("passed its check"))
        XCTAssertTrue(Words.fileState(season.files[1]).localizedCaseInsensitiveContains("restor"))
        XCTAssertTrue(Words.waitingReason(season.files[1]).contains("before publishing"))
        XCTAssertNotEqual(Words.waitingReason(season.files[0]), Words.waitingReason(season.files[1]))
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
        for place in [Place.item("tv/Futurama/Season 2"), .sample("tv/The Expanse/Season 3"), .library, .spaceSaved, .computer("mini"), .computer(nil)] {
            XCTAssertEqual(Place(notificationValue: place.notificationValue), place)
        }
        // Notifications sent before Computers moved into Settings still open it.
        XCTAssertEqual(Place(notificationValue: "computers"), .computer(nil))
        XCTAssertNil(Place(notificationValue: "web:/folders/tv"))
    }
}
