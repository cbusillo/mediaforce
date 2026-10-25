import Foundation
import XCTest
@testable import Mediaforce

final class SpaceSavedTests: XCTestCase {
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

    func testTheDeleteLineSaysWhatComesBack() {
        XCTAssertEqual(Words.deleteBackupsLine(46, 20_000_000_000), "Delete 46 original backups to get back \(Words.size(20_000_000_000)).")
        XCTAssertEqual(Words.deleteBackupsLine(1, 300), "Delete 1 original backup to get back \(Words.size(300)).")
    }
}
