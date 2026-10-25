import Foundation
import XCTest
@testable import Mediaforce

final class SettingsTests: XCTestCase {
    private let serverFacts = """
    {"libraries": [{"key": "movies", "name": "Movies", "folder": "/Volumes/media/movies", "type": "movie"},
                   {"key": "tv", "name": "Tv", "folder": "/Volumes/media/tv", "type": "tv"}],
     "working_folder": "/Volumes/media/transcode", "catalog_refresh_hours": 6.0,
     "types": ["tv", "movie", "spatial", "other"]}
    """

    private func facts() throws -> LibrarySettingsFacts {
        try JSONDecoder.mediaforce.decode(LibrarySettingsFacts.self, from: Data(serverFacts.utf8))
    }

    private func json(_ save: LibrarySettingsSave) throws -> NSDictionary {
        try XCTUnwrap(JSONSerialization.jsonObject(with: JSONEncoder.mediaforce.encode(save)) as? NSDictionary)
    }

    func testLibrarySettingsDecodeFromServerShape() throws {
        let decoded = try facts()
        XCTAssertEqual(decoded.libraries.map(\.key), ["movies", "tv"])
        XCTAssertEqual(decoded.libraries.map(\.type), ["movie", "tv"])
        XCTAssertEqual(decoded.libraries[0].folder, "/Volumes/media/movies")
        XCTAssertNil(decoded.libraries[0].typeChangeConfirmation)
        XCTAssertEqual(decoded.workingFolder, "/Volumes/media/transcode")
        XCTAssertEqual(decoded.catalogRefreshHours, 6)
        XCTAssertEqual(decoded.types, ["tv", "movie", "spatial", "other"])
    }

    func testSaveBodyIsTheLibrarySectionTheServerReads() throws {
        var draft = LibrarySettingsDraft(try facts())
        draft.rows[1].library.name = "TV"
        draft.addLibrary(type: "other")
        draft.rows[2].library.name = "Home Videos"
        draft.rows[2].library.folder = "/Volumes/media/home"
        draft.workingFolder = " /Volumes/media/work "
        draft.catalogRefreshHours = 12

        XCTAssertEqual(try json(draft.saveBody), [
            "libraries": [
                ["key": "movies", "name": "Movies", "folder": "/Volumes/media/movies", "type": "movie"],
                ["key": "tv", "name": "TV", "folder": "/Volumes/media/tv", "type": "tv"],
                ["key": "", "name": "Home Videos", "folder": "/Volumes/media/home", "type": "other"],
            ],
            "working_folder": "/Volumes/media/work",
            "catalog_refresh_hours": 12,
        ] as NSDictionary)
    }

    func testSavedLibraryChangesKindOnlyThroughItsConfirmedPreview() throws {
        let saved = try facts()
        var draft = LibrarySettingsDraft(saved)
        let movies = draft.rows[0].id

        XCTAssertEqual(draft.chooseType("other", for: movies, saved: saved), .needsPreview(key: "movies"))
        XCTAssertEqual(draft.rows[0].library.type, "movie")

        draft.confirmType(
            LibraryTypePreview(key: "movies", fromType: "movie", toType: "other", itemCount: 40, acknowledgement: "movies:movie->other"),
            for: movies
        )
        let body = try json(draft.saveBody)
        let sent = try XCTUnwrap((body["libraries"] as? [NSDictionary])?.first)
        XCTAssertEqual(sent["type"] as? String, "other")
        XCTAssertEqual(sent["type_change_confirmation"] as? String, "movies:movie->other")

        XCTAssertEqual(draft.chooseType("movie", for: movies, saved: saved), .applied)
        XCTAssertEqual(draft.rows[0].library.type, "movie")
        XCTAssertNil(draft.rows[0].library.typeChangeConfirmation)
    }

    func testNewLibraryChoosesItsKindWithoutAPreview() throws {
        let saved = try facts()
        var draft = LibrarySettingsDraft(saved)
        draft.addLibrary(type: "other")
        let added = try XCTUnwrap(draft.rows.last?.id)

        XCTAssertEqual(draft.chooseType("tv", for: added, saved: saved), .applied)
        XCTAssertEqual(draft.rows.last?.library.type, "tv")
    }

    func testEveryRefusalCodeHasItsOwnWords() {
        let codes = [
            "working_folder_missing", "working_folder_not_absolute", "refresh_interval_invalid", "library_incomplete",
            "library_name_too_long", "library_folder_not_absolute", "library_folder_duplicate", "library_folders_overlap",
            "library_contains_working_folder", "library_type_unsupported", "library_type_unconfirmed", "no_libraries",
        ]
        let words = codes.map { Words.librarySaveRefused($0) }
        XCTAssertEqual(Set(words).count, codes.count)
        XCTAssertFalse(words.contains(Words.librarySaveRefused("invalid_settings")))
    }
}
