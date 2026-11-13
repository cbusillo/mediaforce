import XCTest
@testable import Mediaforce

final class PageNavigationTests: XCTestCase {
    func testNestedPagesReturnThroughTheirActualOrigins() {
        let show = Place.item("tv/Example")
        let season = Place.item("tv/Example/Season 2")
        let sample = Place.sample("tv/Example/Season 2")
        for origin in [Place.library, .mediaKind(.movies), .mediaKind(.other), .media, .spaceSaved, .computer(nil), .computer("mini"),
                       .kind("cartoon"), .seasonsWithoutKind] {
            var navigation = PageNavigation()
            navigation.select(origin)
            navigation.open(show)
            navigation.open(season)
            navigation.open(sample)
            for previous in [season, show, origin] {
                XCTAssertEqual(navigation.back, previous)
                navigation.goBack()
                XCTAssertEqual(navigation.place, previous)
            }
            XCTAssertNil(navigation.back)
            navigation.goBack()
            XCTAssertEqual(navigation.place, origin)
        }
    }

    func testSidebarSelectionStartsANewPathAndReopeningDoesNotAddAStep() {
        var navigation = PageNavigation()
        let item = Place.item("movies/Example")
        navigation.open(item)
        navigation.open(item)
        navigation.goBack()
        XCTAssertEqual(navigation.place, .library)
        XCTAssertNil(navigation.back)

        navigation.open(item)
        navigation.select(.spaceSaved)
        XCTAssertNil(navigation.back)
        navigation.open(item)
        navigation.goBack()
        XCTAssertEqual(navigation.place, .spaceSaved)
    }

    func testEveryNotificationAboutAComputerOpensAPlaceTheSidebarShows() throws {
        let library = try JSONDecoder.mediaforce.decode(MainWindowFacts.Library.self, from: Data("""
        {"tv": {"show_count": 1, "season_count": 1, "episode_count": 1, "total_bytes": 1, "original_bytes": 1},
         "movies": {"title_count": 1, "total_bytes": 1, "original_bytes": 1},
         "other": {"item_count": 0, "total_bytes": 0, "original_bytes": 0}}
        """.utf8))
        let shown = Sidebar.top + Sidebar.libraries(in: library)
        XCTAssertEqual(Set(shown).count, shown.count)
        XCTAssertTrue(shown.contains(try XCTUnwrap(Place(notificationValue: Place.computer(nil).notificationValue))))
        // The library lists are the lists that show what each can save, not a second overview.
        XCTAssertTrue(shown.contains(.mediaKind(.tv)))
        XCTAssertFalse(shown.contains(.mediaKind(.other)), "VR & 3D appears only once it holds something")
    }

    func testBackUsesTheTitleThePageDisplayedInsteadOfItsFolder() {
        var navigation = PageNavigation()
        let season = Place.item("tv/Example/S02")
        let sample = Place.sample("tv/Example/S02")
        let title = "Season 2"
        navigation.open(season)
        navigation.name(season, title)
        navigation.open(sample)
        XCTAssertEqual(navigation.backTitle, title)
        let sampleTitle = Words.sampleTitle(title)
        navigation.name(sample, sampleTitle)
        navigation.open(.spaceSaved)
        XCTAssertEqual(navigation.back, sample)
        XCTAssertEqual(navigation.backTitle, sampleTitle)
        XCTAssertNotEqual(navigation.backTitle, title)
        navigation.goBack()
        XCTAssertEqual(navigation.place, sample)
    }
}
