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

    func testBackLabelsUseTheDestinationKindAndFolderNames() {
        let kind = "cartoon"
        let folder = "Season 12"
        XCTAssertEqual(Words.backTitle(.kind(kind)), Words.kindName(kind))
        XCTAssertEqual(Words.backTitle(.media), Words.media)
        XCTAssertEqual(Words.backTitle(.seasonsWithoutKind), Words.seasonsWithoutKindTitle)
        XCTAssertEqual(Words.backTitle(.item("tv/Example/\(folder)")), folder)
        XCTAssertTrue(Words.backTitle(.sample("tv/Example/\(folder)")).contains(folder))
    }
}
