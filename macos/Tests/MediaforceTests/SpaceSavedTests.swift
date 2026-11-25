import Foundation
import XCTest
@testable import Mediaforce

final class SpaceSavedTests: XCTestCase {
    @MainActor
    func testWaitingBackupsTakeTheirOwnShareOfTheBarUntilDeleted() throws {
        let facts = try JSONDecoder.mediaforce.decode(MainWindowFacts.self, from: Data(MainWindowTests.facts.utf8))
        let model = MainWindowModel(client: MediaforceClient(baseURL: URL(filePath: "/fixture")))
        func segments(_ bytes: Int64) -> [SavingsSummary.Segment] {
            SavingsSummary(facts: facts, listing: MediaListing(), machines: ComputersStore(), posters: model.posters,
                           backupBytes: bytes).segments
        }
        let waiting = segments(500)
        let cleared = segments(0)
        let backup = try XCTUnwrap(waiting.first { $0.label == Words.originalBackups })
        XCTAssertEqual(backup.bytes, 500)
        XCTAssertTrue(backup.tip.contains(Words.size(500)))
        XCTAssertFalse(cleared.contains { $0.label == Words.originalBackups })
        XCTAssertEqual(waiting.compactMap(\.bytes).reduce(0, +), cleared.compactMap(\.bytes).reduce(0, +))
    }

    func testGroupedInnerRingCountsOnlyPublishedFilesAtTheirOriginalSize() throws {
        let compressedShow = "tv/Small"
        let original: Int64 = 80
        let rows = (0..<60).map { index in
            let id = index == 0 ? compressedShow : "tv/Show \(index)"
            let bytes: Int64 = index == 0 ? 200 : 1000
            return MediaRow(id: id, name: id, kind: .tv, detail: "", bytes: bytes, saved: nil, state: .inProgress,
                            looseFiles: .init(state: "", episodeCount: 2, bytes: bytes))
        }
        let library = SavingsLibrary(rows: rows, sizes: [compressedShow: .init(saved: 60, finishedSeason: false, compressedBefore: original)])
        let group = try XCTUnwrap(library.groups.first { $0.slices.contains { $0.id == compressedShow } && !$0.isShow })
        let inner = RingSector.sectors(library.groups, zoomed: false).filter { $0.group == group.id && $0.slice == nil }
        let green = try XCTUnwrap(inner.first { $0.color == .green })
        let totalAngle = inner.reduce(0) { $0 + $1.end - $1.start }
        XCTAssertEqual((green.end - green.start) / totalAngle, Double(original) / Double(group.before), accuracy: 1e-10)
    }

    func testFinishEstimateUsesFourFullWeeksAndSkipsTheWeekStillRunning() throws {
        let bytes = [10, 10, 10, 10, 1000]
        let weeks = bytes.enumerated().map { SavingsHistory.Week(start: String($0.offset), bytes: Int64($0.element)) }
        let history = SavingsHistory(weeks: weeks, wins: [])
        let now = try XCTUnwrap(ActivityFacts.date("2026-11-24T12:00:00Z"))
        XCTAssertEqual(history.finish(remaining: 20, at: now), Calendar.current.date(byAdding: .day, value: 14, to: now))
        XCTAssertNil(history.finish(remaining: nil, at: now))
    }

    func testSeasonsWithBackupsBecomeOneTilePerShow() throws {
        let completed = try JSONDecoder.mediaforce.decode(SpaceSavedFacts.Completed.self, from: Data("""
        {"folders": [
          {"prefix": "tv/Futurama/Season 3", "title": "Futurama · Season 3", "subtitle": "Futurama",
           "archived_backup_count": 15, "archived_backup_size_bytes": 6000, "total_bytes_saved": 4000},
          {"prefix": "tv/House/Season 3", "title": "House · Season 3", "subtitle": "House",
           "archived_backup_count": 1, "archived_backup_size_bytes": 300, "total_bytes_saved": 100},
          {"prefix": "tv/Futurama/Season 2", "title": "Futurama · Season 2", "subtitle": "Futurama",
           "archived_backup_count": 19, "archived_backup_size_bytes": 7000, "total_bytes_saved": 5000},
          {"prefix": "tv/Lost/Season 1", "title": "Lost · Season 1", "subtitle": "Lost",
           "archived_backup_count": 0, "archived_backup_size_bytes": 0, "total_bytes_saved": 9000}],
         "completed_count": 4,
         "archive_cleanup": {"archive_root": "/x", "file_count": 35, "total_size_bytes": 13300, "has_cleanup": true}}
        """.utf8))
        let shows = SpaceSavedFacts.shows(completed.folders)
        XCTAssertEqual(shows.map(\.prefix), ["tv/Futurama", "tv/House"])
        XCTAssertEqual(shows.first?.seasonPrefixes, ["tv/Futurama/Season 3", "tv/Futurama/Season 2"])
        XCTAssertEqual(shows.first?.backupBytes, 13000)
    }

    private func cleanup(_ json: String) throws -> CleanupFolder {
        try JSONDecoder.mediaforce.decode(CleanupFolder.self, from: Data(json.utf8))
    }

    func testUnavailableBackupCountShowsTheReasonAndOffersNoDeletion() throws {
        let folder = try cleanup(#"{"file_count": 0, "total_size_bytes": 0, "waiting_reason": "Waiting for storage."}"#)
        let status = BackupsStatus(cleanup: folder, asking: false)
        XCTAssertEqual(status.line, "Waiting for storage.")
        XCTAssertFalse(status.showsDelete)
    }

    func testARunningDeletionShowsItsProgressAndTheButtonCannotBePressed() throws {
        let folder = try cleanup("""
        {"file_count": 22, "total_size_bytes": 92000000000, "deletion": {"state": "running", "total_count": 34,
         "total_bytes": 143000000000, "deleted_count": 12, "freed_bytes": 51000000000, "kept_count": 0,
         "failed_file": null, "failure_reason": null}}
        """)
        let status = BackupsStatus(cleanup: folder, asking: false)
        XCTAssertTrue(status.deleting)
        XCTAssertTrue(status.showsDelete)
        XCTAssertFalse(status.canDelete)
        XCTAssertEqual(status.line, "Deleting 34 original backups… 12 of 34, \(Words.size(51000000000)) back so far")
        XCTAssertEqual(status.progress ?? 0, 12.0 / 34.0, accuracy: 0.001)
        XCTAssertNil(status.outcome)
    }

    func testTheButtonCannotBePressedBeforeMediaforceFirstReportsTheDeletion() throws {
        let status = BackupsStatus(cleanup: try cleanup(#"{"file_count": 34, "total_size_bytes": 143}"#), asking: true)
        XCTAssertFalse(status.canDelete)
        XCTAssertEqual(status.line, "Deleting original backups…")
        XCTAssertNil(status.progress)
    }

    func testAFinishedDeletionSaysWhatCameBack() throws {
        let folder = try cleanup("""
        {"file_count": 0, "total_size_bytes": 0, "deletion": {"state": "finished", "total_count": 34,
         "deleted_count": 34, "freed_bytes": 143000000000, "kept_count": 0, "failed_file": null, "failure_reason": null}}
        """)
        let status = BackupsStatus(cleanup: folder, asking: false)
        XCTAssertFalse(status.deleting)
        XCTAssertTrue(status.showsDelete)
        XCTAssertFalse(status.canDelete)
        XCTAssertEqual(status.line, "None waiting · 34 deleted, \(Words.size(143000000000))")
        XCTAssertNil(status.outcome)
    }

    func testSkippedBackupsShowEachReasonAndLeaveDeleteAvailable() throws {
        let folder = try cleanup("""
        {"file_count": 2, "total_size_bytes": 92, "deletion": {"state": "finished", "total_count": 5,
         "deleted_count": 3, "freed_bytes": 51000000000, "kept_count": 0, "skipped_files": [
         {"file": "tv/Lost/Pilot.mkv", "reason": "Permission denied"}, {"file": "movie/Pilot.mkv", "reason": "File is busy"}]}}
        """)
        let status = BackupsStatus(cleanup: folder, asking: false)
        let outcome = try XCTUnwrap(status.outcome)
        XCTAssertTrue(status.canDelete)
        XCTAssertTrue(outcome.failed)
        XCTAssertTrue(outcome.text.contains(Words.count(3, "original backup")))
        XCTAssertTrue(outcome.text.contains(Words.size(51000000000)))
        let lines = outcome.text.split(separator: "\n")
        for (file, reason) in [("tv/Lost/Pilot.mkv", "Permission denied"), ("movie/Pilot.mkv", "File is busy")] {
            XCTAssertTrue(lines.contains { $0.contains(file) && $0.contains(reason) }, outcome.text)
        }
    }

    func testAFailedDeletionNamesTheFileAndWhyAndAllowsAnotherTry() throws {
        let folder = try cleanup("""
        {"file_count": 22, "total_size_bytes": 92, "deletion": {"state": "failed", "total_count": 34,
         "deleted_count": 12, "freed_bytes": 51000000000, "kept_count": 0, "failed_file": "tv/Lost/Season 1/Pilot.mkv",
         "failure_reason": "Permission denied"}}
        """)
        let status = BackupsStatus(cleanup: folder, asking: false)
        XCTAssertTrue(status.canDelete)
        XCTAssertEqual(status.outcome?.failed, true)
        XCTAssertEqual(status.outcome?.text, "Couldn't delete tv/Lost/Season 1/Pilot.mkv: Permission denied. Mediaforce stopped there. "
            + "12 of 34 were deleted, \(Words.size(51000000000)) back; the rest are still there.")
    }
}
