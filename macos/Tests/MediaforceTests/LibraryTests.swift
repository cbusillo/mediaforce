import Foundation
import XCTest
@testable import Mediaforce

final class LibraryTests: XCTestCase {
    @MainActor
    func testSavingsUseAllThreeServerEstimatesAndKeepMissingCoverageHonest() async {
        let config = URLSessionConfiguration.ephemeral
        config.protocolClasses = [PartialLibraryServer.self]
        let session = URLSession(configuration: config)
        defer { session.invalidateAndCancel(); PartialLibraryServer.mode = .populated }
        let client = MediaforceClient(baseURL: URL(string: "http://mediaforce.invalid")!, session: session)
        let listing = MediaListing()
        XCTAssertNil(listing.estimatedTotalSavedBytes(savedSoFar: 10))

        PartialLibraryServer.mode = .complete
        await listing.refresh(client, kind: .everything)
        XCTAssertEqual(listing.estimatedTotalSavedBytes(savedSoFar: 10), 160)
        XCTAssertFalse(listing.estimateIsPartial)
        let complete = Self.estimateValue(listing)

        PartialLibraryServer.mode = .partial
        await listing.refresh(client, kind: .everything)
        XCTAssertEqual(listing.estimatedTotalSavedBytes(savedSoFar: 10), 140)
        XCTAssertTrue(listing.estimateIsPartial)
        let partial = Self.estimateValue(listing)
        XCTAssertTrue(partial.contains(Words.size(140)))
        XCTAssertNotEqual(
            partial.replacingOccurrences(of: Words.size(140), with: ""),
            complete.replacingOccurrences(of: Words.size(160), with: ""),
            "A partial total must read differently from a complete one"
        )

        PartialLibraryServer.mode = .truncated
        await listing.refresh(client, kind: .everything)
        XCTAssertEqual(listing.estimatedTotalSavedBytes(savedSoFar: 10), 140)
        XCTAssertTrue(listing.estimateIsPartial)
        XCTAssertTrue(listing.truncated)

        PartialLibraryServer.mode = .failing
        await listing.refresh(client, kind: .everything)
        XCTAssertEqual(listing.estimatedTotalSavedBytes(savedSoFar: 10), 140)
        XCTAssertTrue(listing.estimateIsPartial)

        PartialLibraryServer.mode = .empty
        await listing.refresh(client, kind: .everything)
        XCTAssertEqual(listing.estimatedTotalSavedBytes(savedSoFar: 10), 10)
        XCTAssertFalse(listing.estimateIsPartial)
        XCTAssertEqual(
            Self.estimateValue(listing).replacingOccurrences(of: Words.size(10), with: ""),
            complete.replacingOccurrences(of: Words.size(160), with: "")
        )
    }

    @MainActor
    private static func estimateValue(_ listing: MediaListing) -> String {
        Words.estimatedTotalSavingsValue(listing.estimatedTotalSavedBytes(savedSoFar: 10), partial: listing.estimateIsPartial)
    }

    @MainActor
    func testCombinedLibraryKeepsMoviesWhenAnotherLibraryFails() async {
        let config = URLSessionConfiguration.ephemeral
        config.protocolClasses = [PartialLibraryServer.self]
        let session = URLSession(configuration: config)
        defer { session.invalidateAndCancel() }
        let client = MediaforceClient(baseURL: URL(string: "http://mediaforce.invalid")!, session: session)
        let listing = MediaListing()
        await listing.refresh(client, kind: .everything)
        XCTAssertEqual(listing.unavailable, [.tv])
        XCTAssertFalse(listing.hasLoaded(.tv))
        XCTAssertTrue(listing.hasLoaded(.movies))
        XCTAssertEqual(listing.rows.map(\.kind), [.movies])
        XCTAssertNil(listing.estimatedTotalSavedBytes(savedSoFar: 10))
        XCTAssertTrue(listing.estimateIsPartial)
    }

    @MainActor
    func testFailedRefreshRetainsRowsUntilASuccessfulEmptyResponse() async {
        let config = URLSessionConfiguration.ephemeral
        config.protocolClasses = [PartialLibraryServer.self]
        let session = URLSession(configuration: config)
        defer { session.invalidateAndCancel(); PartialLibraryServer.mode = .populated }
        let client = MediaforceClient(baseURL: URL(string: "http://mediaforce.invalid")!, session: session)
        let listing = MediaListing()
        await listing.refresh(client, kind: .movies)
        let previous = listing.rows.map(\.id)
        XCTAssertFalse(previous.isEmpty)
        PartialLibraryServer.mode = .failing
        await listing.refresh(client, kind: .movies)
        XCTAssertEqual(listing.rows.map(\.id), previous)
        XCTAssertEqual(listing.unavailable, [.movies])
        XCTAssertTrue(listing.hasLoaded(.movies))
        PartialLibraryServer.mode = .empty
        await listing.refresh(client, kind: .movies)
        XCTAssertTrue(listing.rows.isEmpty)
        XCTAssertTrue(listing.unavailable.isEmpty)
        XCTAssertTrue(listing.hasLoaded(.movies))
    }

}

private final class PartialLibraryServer: URLProtocol, @unchecked Sendable {
    enum Mode { case populated, failing, empty, complete, partial, truncated }
    nonisolated(unsafe) static var mode = Mode.populated
    override class func canInit(with request: URLRequest) -> Bool { true }
    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }

    override func startLoading() {
        let body: String
        switch request.url?.path {
        case "/api/app/tv-shows" where Self.mode == .empty:
            body = "{\"shows\": []}"
        case "/api/app/tv-shows" where [.complete, .partial, .truncated].contains(Self.mode):
            body = """
            {"shows": [{"show_prefix": "tv/Example", "show": "A show", "season_count": 1, "episode_count": 1,
                        "bytes": 150, "estimated_saved_bytes": 100, "seasons": []}]}
            """
        case "/api/app/movies":
            if Self.mode == .failing {
                client?.urlProtocol(self, didFailWithError: URLError(.cannotConnectToHost))
                return
            }
            if Self.mode == .empty {
                body = "{\"movies\": []}"
                break
            }
            body = """
            {"movies": [{"prefix": "films/Example", "title": "A movie", "file_count": 1,
                         "bytes": 70, "state": "ready", "estimated_saved_bytes": \(Self.mode == .populated ? "null" : "30")}]}
            """
        case "/api/app/other" where [.complete, .partial].contains(Self.mode):
            body = """
            {"items": [{"prefix": "other/Example", "title": "A folder", "file_count": 1,
                        "bytes": 50, "state": "ready", "estimated_saved_bytes": \(Self.mode == .partial ? "null" : "20")}], "truncated": false}
            """
        case "/api/app/other" where Self.mode == .truncated: body = "{\"items\": [], \"truncated\": true}"
        case "/api/app/other": body = "{\"items\": [], \"truncated\": false}"
        default:
            client?.urlProtocol(self, didFailWithError: URLError(.cannotConnectToHost))
            return
        }
        let response = HTTPURLResponse(url: request.url!, statusCode: 200, httpVersion: nil, headerFields: nil)!
        client?.urlProtocol(self, didReceive: response, cacheStoragePolicy: .notAllowed)
        client?.urlProtocol(self, didLoad: Data(body.utf8))
        client?.urlProtocolDidFinishLoading(self)
    }

    override func stopLoading() {}
}
