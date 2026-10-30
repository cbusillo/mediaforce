import Foundation
import XCTest
@testable import Mediaforce

final class LibraryTests: XCTestCase {
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
    enum Mode { case populated, failing, empty }
    nonisolated(unsafe) static var mode = Mode.populated
    override class func canInit(with request: URLRequest) -> Bool { true }
    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }

    override func startLoading() {
        let body: String
        switch request.url?.path {
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
                         "bytes": 70, "state": "ready"}]}
            """
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
