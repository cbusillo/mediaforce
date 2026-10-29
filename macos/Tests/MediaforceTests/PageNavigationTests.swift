import XCTest
@testable import Mediaforce

final class PageNavigationTests: XCTestCase {
    func testNestedPagesReturnThroughTheirActualOrigins() {
        let show = Place.item("tv/Example")
        let season = Place.item("tv/Example/Season 2")
        let sample = Place.sample("tv/Example/Season 2")
        for origin in [Place.library, .media, .spaceSaved, .kind("cartoon"), .seasonsWithoutKind] {
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
