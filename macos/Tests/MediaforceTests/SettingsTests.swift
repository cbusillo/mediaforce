import Foundation
import XCTest
@testable import Mediaforce

final class SettingsTests: XCTestCase {
    private let serverFacts = """
    {"libraries": [{"key": "movies", "name": "Movies", "folder": "/Volumes/media/movies", "type": "movie",
                    "plex_path": "/data/movies"},
                   {"key": "tv", "name": "Tv", "folder": "/Volumes/media/tv", "type": "tv", "plex_path": ""}],
     "working_folder": "/Volumes/media/transcode", "catalog_refresh_hours": 6.0,
     "types": ["tv", "movie", "spatial", "other"], "revision": "r1"}
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
        XCTAssertEqual(decoded.libraries.map(\.plexPath), ["/data/movies", ""])
        XCTAssertNil(decoded.libraries[0].typeChangeConfirmation)
        XCTAssertEqual(decoded.workingFolder, "/Volumes/media/transcode")
        XCTAssertEqual(decoded.catalogRefreshHours, 6)
        XCTAssertEqual(decoded.types, ["tv", "movie", "spatial", "other"])
        XCTAssertEqual(decoded.revision, "r1")
    }

    func testSaveBodyIsTheLibrarySectionTheServerReads() throws {
        var draft = LibrarySettingsDraft(try facts())
        draft.rows[1].library.name = "TV"
        draft.rows[1].library.plexPath = "/data/tv"
        draft.addLibrary(type: "other")
        draft.rows[2].library.name = "Home Videos"
        draft.rows[2].library.folder = "/Volumes/media/home"
        draft.workingFolder = " /Volumes/media/work "
        draft.catalogRefreshHours = 12

        XCTAssertEqual(try json(draft.saveBody), [
            "libraries": [
                ["key": "movies", "name": "Movies", "folder": "/Volumes/media/movies", "type": "movie",
                 "plex_path": "/data/movies"],
                ["key": "tv", "name": "TV", "folder": "/Volumes/media/tv", "type": "tv", "plex_path": "/data/tv"],
                ["key": "", "name": "Home Videos", "folder": "/Volumes/media/home", "type": "other", "plex_path": ""],
            ],
            "working_folder": "/Volumes/media/work",
            "catalog_refresh_hours": 12,
            "revision": "r1",
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
            "plex_path_relative", "plex_path_duplicate",
        ]
        let words = codes.map { Words.librarySaveRefused($0) }
        XCTAssertEqual(Set(words).count, codes.count)
        XCTAssertFalse(words.contains(Words.librarySaveRefused("invalid_settings")))
    }

    private let computerFacts = """
    {"computers": [{"id": "0", "name": "Studio", "address": "studio.local", "abilities": ["encode_queue"],
                    "schedule": "nights", "libraries": [], "at_once": 2, "wake_address": "00:11:22:33:44:55",
                    "priority": 40, "media_access": "stream", "mediaforce_folder": "/Users/me/mediaforce",
                    "working_folder": "", "scratch_folder": "/var/tmp/mf",
                    "library_folders": [{"library": "movies_4k", "folder": "/Volumes/remote/movies"}],
                    "start_command": "", "stop_command": "", "start_wait_seconds": 300}],
     "schedules": [{"key": "nights", "name": "Nights", "days": ["mon", "tue"], "all_day_days": ["sun"],
                    "start_hour": 22, "end_hour": 6}],
     "built_in_schedules": ["always", "never"], "abilities": ["encode_queue", "sample_calibration"],
     "media_access_options": ["mounted", "stream"], "days": ["mon", "tue", "wed", "thu", "fri", "sat", "sun"],
     "libraries": [{"key": "movies_4k", "name": "Movies"}, {"key": "tv", "name": "TV"}], "revision": "c1"}
    """

    private func computers() throws -> ComputerSettingsFacts {
        try JSONDecoder.mediaforce.decode(ComputerSettingsFacts.self, from: Data(computerFacts.utf8))
    }

    private func json(_ save: ComputerSettingsSave) throws -> NSDictionary {
        try XCTUnwrap(JSONSerialization.jsonObject(with: JSONEncoder.mediaforce.encode(save)) as? NSDictionary)
    }

    func testComputerSettingsDecodeFromServerShape() throws {
        let decoded = try computers()
        let studio = try XCTUnwrap(decoded.computers.first)
        XCTAssertEqual(studio.address, "studio.local")
        XCTAssertEqual(studio.atOnce, 2)
        XCTAssertEqual(studio.wakeAddress, "00:11:22:33:44:55")
        XCTAssertEqual(studio.mediaAccess, "stream")
        XCTAssertEqual(studio.libraryFolders, [ComputerSettingsFacts.LibraryFolder(library: "movies_4k", folder: "/Volumes/remote/movies")])
        XCTAssertEqual(studio.startWaitSeconds, 300)
        XCTAssertEqual(decoded.schedules.first?.allDayDays, ["sun"])
        XCTAssertEqual(decoded.builtInSchedules, ["always", "never"])
        XCTAssertEqual(decoded.mediaAccessOptions, ["mounted", "stream"])
        XCTAssertEqual(decoded.libraries.map(\.key), ["movies_4k", "tv"])
        XCTAssertEqual(decoded.revision, "c1")
    }

    func testComputerSaveBodyIsTheSectionTheServerReads() throws {
        let saved = try computers()
        var draft = ComputerSettingsDraft(saved)
        draft.computers[0].computer.name = "Studio Mac"
        draft.addSchedule(days: ["sat"])
        let newKey = draft.schedules[1].schedule.key
        draft.schedules[1].schedule.name = "Weekends"
        draft.addComputer(abilities: saved.abilities, mediaAccess: "mounted")
        draft.computers[1].computer.address = "laptop.local"
        draft.computers[1].computer.schedule = newKey

        let body = try json(draft.saveBody)
        let sentComputers = try XCTUnwrap(body["computers"] as? [NSDictionary])
        XCTAssertEqual(sentComputers[0], [
            "id": "0", "name": "Studio Mac", "address": "studio.local", "abilities": ["encode_queue"],
            "schedule": "nights", "libraries": [String](), "at_once": 2, "wake_address": "00:11:22:33:44:55",
            "priority": 40, "media_access": "stream", "mediaforce_folder": "/Users/me/mediaforce",
            "working_folder": "", "scratch_folder": "/var/tmp/mf",
            "library_folders": [["library": "movies_4k", "folder": "/Volumes/remote/movies"]],
            "start_command": "", "stop_command": "", "start_wait_seconds": 300,
        ] as NSDictionary)
        XCTAssertEqual(sentComputers[1]["id"] as? String, "")
        XCTAssertEqual(sentComputers[1]["schedule"] as? String, newKey)
        let sentSchedules = try XCTUnwrap(body["schedules"] as? [NSDictionary])
        XCTAssertEqual(sentSchedules[0], [
            "key": "nights", "name": "Nights", "days": ["mon", "tue"], "all_day_days": ["sun"],
            "start_hour": 22, "end_hour": 6,
        ] as NSDictionary)
        XCTAssertEqual(sentSchedules[1]["key"] as? String, newKey)
        XCTAssertEqual(body["revision"] as? String, "c1")
    }

    func testChoosingEveryLibraryMeansAllLibraries() {
        let all = ["movies", "tv", "other"]
        XCTAssertEqual(ComputerSettingsDraft.togglingLibrary("tv", in: [], all: all), ["movies", "other"])
        XCTAssertEqual(ComputerSettingsDraft.togglingLibrary("tv", in: ["movies", "other"], all: all), [])
        XCTAssertEqual(ComputerSettingsDraft.togglingLibrary("movies", in: ["movies"], all: all), ["movies"])
    }

    func testADayRunsInTheWindowOrAllDayNeverBoth() throws {
        let days = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
        let nights = try XCTUnwrap(try computers().schedules.first)

        let allDayMonday = ComputerSettingsDraft.togglingDay("mon", allDay: true, in: nights, order: days)
        XCTAssertEqual(allDayMonday.days, ["tue"])
        XCTAssertEqual(allDayMonday.allDayDays, ["mon", "sun"])

        let windowSunday = ComputerSettingsDraft.togglingDay("sun", allDay: false, in: nights, order: days)
        XCTAssertEqual(windowSunday.days, ["mon", "tue", "sun"])
        XCTAssertEqual(windowSunday.allDayDays, [])
    }

    func testEveryComputerRefusalCodeHasItsOwnWords() {
        let codes = [
            "computer_address_missing", "computer_address_duplicate", "computer_abilities_missing",
            "computer_priority_invalid", "computer_at_once_invalid", "computer_start_wait_invalid",
            "computer_scratch_folder_not_absolute", "computer_library_folders_invalid",
            "computer_library_folder_unknown", "schedule_duplicate", "schedule_days_missing",
            "schedule_hours_invalid", "schedule_unknown",
        ]
        let words = codes.map { Words.computerSaveRefused($0) }
        XCTAssertEqual(Set(words).count, codes.count)
        XCTAssertFalse(words.contains(Words.computerSaveRefused("invalid_settings")))
    }

    private let metadataFacts = """
    {"plex": {"enabled": true, "address": "http://plex.local:32400", "key_name": "MEDIAFORCE_PLEX_TOKEN",
              "key_found": true},
     "tmdb": {"enabled": false, "key_name": "MEDIAFORCE_TMDB_TOKEN", "key_found": false},
     "revision": "m1"}
    """

    func testMetadataSettingsDecodeFromServerShape() throws {
        let decoded = try JSONDecoder.mediaforce.decode(MetadataSettingsFacts.self, from: Data(metadataFacts.utf8))
        XCTAssertEqual(decoded.plex, .init(
            enabled: true, address: "http://plex.local:32400", keyName: "MEDIAFORCE_PLEX_TOKEN", keyFound: true
        ))
        XCTAssertEqual(decoded.tmdb, .init(enabled: false, keyName: "MEDIAFORCE_TMDB_TOKEN", keyFound: false))
        XCTAssertEqual(decoded.revision, "m1")
    }

    func testMetadataSaveBodySendsOnlyWhatTheTabEdits() throws {
        let decoded = try JSONDecoder.mediaforce.decode(MetadataSettingsFacts.self, from: Data(metadataFacts.utf8))
        var draft = MetadataSettingsDraft(decoded)
        draft.plex.address = " https://plex.example.com "
        draft.tmdb.enabled = true

        let body = try XCTUnwrap(
            JSONSerialization.jsonObject(with: JSONEncoder.mediaforce.encode(draft.saveBody)) as? NSDictionary
        )
        XCTAssertEqual(body, [
            "plex": ["enabled": true, "address": "https://plex.example.com"],
            "tmdb": ["enabled": true],
            "revision": "m1",
        ] as NSDictionary)
    }

    func testEveryMetadataRefusalCodeHasItsOwnWords() {
        let codes = ["plex_address_invalid", "tmdb_address_invalid"]
        let words = codes.map { Words.metadataSaveRefused($0) }
        XCTAssertEqual(Set(words).count, codes.count)
        XCTAssertFalse(words.contains(Words.metadataSaveRefused("invalid_settings")))
    }

    private let compressionFacts = """
    {"aim": "size_first_review", "size_goal_mb": 300.0, "size_goal_minutes": 45.0, "size_limit_percent": 10.0,
     "largest_percent": 80.0, "tallest_picture": 1080, "grain": 8, "measurement": "vmaf",
     "appearance_target": 85.0, "appearance_minimum": 80.0, "detail_target": 41.0, "detail_minimum": 35.0,
     "sample_method": "ab_av1_fast_sample", "size_goal_mode": "normalized",
     "aims": ["size_first_review", "quality_first_sample"], "measurements": ["auto", "vmaf", "xpsnr"],
     "sample_methods": ["ab_av1_fast_sample"], "revision": "v1"}
    """

    private func compression() throws -> CompressionSettingsFacts {
        try JSONDecoder.mediaforce.decode(CompressionSettingsFacts.self, from: Data(compressionFacts.utf8))
    }

    private let compressionRefusalCodes = [
        "size_goal_invalid", "size_goal_minutes_invalid", "size_limit_invalid", "sample_size_limit_invalid",
        "largest_size_invalid", "tallest_picture_invalid", "grain_invalid", "appearance_target_invalid",
        "appearance_minimum_invalid", "detail_target_invalid", "detail_minimum_invalid",
        "appearance_minimum_above_target", "detail_minimum_above_target", "unknown_aim", "unknown_measurement",
        "unknown_sample_method",
    ]

    func testCompressionSettingsDecodeFromServerShape() throws {
        let decoded = try compression()
        XCTAssertEqual(decoded.aim, "size_first_review")
        XCTAssertEqual(decoded.sizeGoalMb, 300)
        XCTAssertEqual(decoded.sizeGoalMinutes, 45)
        XCTAssertEqual(decoded.sizeLimitPercent, 10)
        XCTAssertEqual(decoded.largestPercent, 80)
        XCTAssertEqual(decoded.tallestPicture, 1080)
        XCTAssertEqual(decoded.grain, 8)
        XCTAssertEqual(decoded.measurement, "vmaf")
        XCTAssertEqual([decoded.appearanceTarget, decoded.appearanceMinimum], [85, 80])
        XCTAssertEqual([decoded.detailTarget, decoded.detailMinimum], [41, 35])
        XCTAssertEqual(decoded.sampleMethod, "ab_av1_fast_sample")
        XCTAssertEqual(decoded.sizeGoalMode, "normalized")
        XCTAssertEqual(decoded.measurements, ["auto", "vmaf", "xpsnr"])
        XCTAssertEqual(decoded.revision, "v1")
    }

    func testCompressionSaveBodyIsTheSectionTheServerReads() throws {
        var draft = CompressionSettingsDraft(try compression())
        draft.saveBody.aim = "quality_first_sample"
        draft.saveBody.sizeGoalMb = 450
        draft.saveBody.tallestPicture = 720
        draft.saveBody.measurement = "auto"

        let body = try XCTUnwrap(
            JSONSerialization.jsonObject(with: JSONEncoder.mediaforce.encode(draft.saveBody)) as? NSDictionary
        )
        XCTAssertEqual(body, [
            "aim": "quality_first_sample", "size_goal_mb": 450, "size_goal_minutes": 45, "size_limit_percent": 10,
            "largest_percent": 80, "tallest_picture": 720, "grain": 8, "measurement": "auto",
            "appearance_target": 85, "appearance_minimum": 80, "detail_target": 41, "detail_minimum": 35,
            "sample_method": "ab_av1_fast_sample", "revision": "v1",
        ] as NSDictionary)
    }

    func testEveryCompressionChoiceHasItsOwnWords() throws {
        let facts = try compression()
        let groups: [([String], (String) -> String)] = [
            (facts.aims, Words.compressionAim),
            (facts.measurements, Words.qualityMeasurement),
            (facts.sampleMethods, Words.sampleMethod),
            ([facts.sizeGoalMode], Words.sizeGoalScaling),
        ]
        for (codes, words) in groups {
            let shown = codes.map(words)
            XCTAssertEqual(Set(shown).count, codes.count)
            XCTAssertFalse(shown.contains(words("not-a-choice")))
        }
    }

    func testEveryCompressionRefusalCodeHasItsOwnWords() {
        let words = compressionRefusalCodes.map { Words.compressionSaveRefused($0) }
        XCTAssertEqual(Set(words).count, compressionRefusalCodes.count)
        XCTAssertFalse(words.contains(Words.compressionSaveRefused("invalid_settings")))
    }

    func testCompressionWordsNeverUseInternalTerms() throws {
        let facts = try compression()
        var shown: [String] = [
            Words.compressionTab, Words.aimSection, Words.aimPicker, Words.sizeSection, Words.sizeGoal,
            Words.megabytes, Words.forAnEpisodeOf, Words.minutes, Words.sizeLimit, Words.sizeLimitHelp,
            Words.largestSize, Words.percentOfOriginal, Words.largestSizeHelp, Words.percent, Words.pictureSection,
            Words.tallestPicture, Words.pixelsTall, Words.tallestPictureHelp, Words.grain, Words.grainHelp,
            Words.advancedCompression, Words.measurementPicker, Words.appearanceTarget, Words.appearanceMinimum,
            Words.detailTarget, Words.detailMinimum, Words.scoresHelp, Words.samplePicker,
            Words.tallestPictureSummary(0), Words.tallestPictureSummary(1080),
            Words.sizeGoalScaling(facts.sizeGoalMode), Words.compressionSaveRefused("invalid_settings"),
        ]
        shown += facts.aims.map(Words.compressionAim)
        shown += facts.measurements.map(Words.qualityMeasurement)
        shown += facts.sampleMethods.map(Words.sampleMethod)
        shown += compressionRefusalCodes.map { Words.compressionSaveRefused($0) }
        let internalTerms = ["VMAF", "XPSNR", "CRF", "PSNR", "AV1", "SVT", "LEDGER", "CADENCE", "SHARD", "MANIFEST"]
        for text in shown {
            for term in internalTerms {
                XCTAssertFalse(text.uppercased().contains(term), "\(text) says \(term)")
            }
        }
    }
}

final class ComputerFixTests: XCTestCase {
    private func host(_ json: String) throws -> HostsFacts.Host {
        try JSONDecoder.mediaforce.decode(HostsFacts.self, from: Data("{\"hosts\": [\(json)]}".utf8)).hosts[0]
    }

    func testAComputerOffersTheFixesTheServerAllows() throws {
        let unusable = try host("""
        {"key": "mini", "available": false, "setup_supported": true, "setup_requires_password": false,
         "trust_reset_supported": false, "issues": [], "recovery": {"status": "action_required", "reason": "x"}}
        """)
        XCTAssertEqual(ComputerFix.offered(unusable), [.checkAgain, .start, .setUp])

        let ready = try host("""
        {"key": "mbp", "available": true, "setup_supported": true, "setup_requires_password": false,
         "trust_reset_supported": false, "issues": [], "recovery": {"status": "ready"}}
        """)
        XCTAssertEqual(ComputerFix.offered(ready), [])

        let changed = try host("""
        {"key": "studio", "available": true, "setup_supported": true, "setup_requires_password": true,
         "trust_reset_supported": true, "issues": [{"code": "x"}], "recovery": null}
        """)
        XCTAssertEqual(ComputerFix.offered(changed), [.setUp, .trustAgain])
    }

    func testEachStateLeadsWithItsOwnFix() {
        XCTAssertEqual(ComputerFix.primary(for: "password_needed"), .setUp)
        XCTAssertEqual(ComputerFix.primary(for: "identity_changed"), .trustAgain)
        XCTAssertEqual(ComputerFix.primary(for: "remote_login_off"), .checkAgain)
        XCTAssertNil(ComputerFix.primary(for: "ready"))
        XCTAssertEqual(Words.computerAction(.setUp, needsPassword: true), "Set Up")
        XCTAssertEqual(Words.atOnce(2), "Jobs at once: 2")
    }
}
