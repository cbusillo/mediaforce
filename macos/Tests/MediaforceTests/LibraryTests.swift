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
    func testOneLibrarysListTotalsItsOwnEstimatesAndSaysWhenSomeAreMissing() async throws {
        let (client, session) = Self.client()
        defer { session.invalidateAndCancel(); PartialLibraryServer.mode = .populated }
        let listing = MediaListing()

        PartialLibraryServer.mode = .complete
        await listing.refresh(client, kind: .movies)
        XCTAssertEqual(listing.estimatedSavedBytes, 30)
        XCTAssertFalse(listing.estimateIsPartial, "Every movie has an estimate, so the Movies total is complete")

        PartialLibraryServer.mode = .unmeasured
        await listing.refresh(client, kind: .movies)
        XCTAssertNil(listing.estimatedSavedBytes, "A movie still to compress at 0 has not been measured")
        XCTAssertTrue(listing.estimateIsPartial)
        XCTAssertEqual(Words.canSave(try XCTUnwrap(listing.rows.first)), Words.notKnownYet)
    }

    @MainActor
    func testVRAnd3DIsNeitherWaitingWorkNorAMissingEstimate() async throws {
        let (client, session) = Self.client()
        defer { session.invalidateAndCancel(); PartialLibraryServer.mode = .populated }
        PartialLibraryServer.mode = .viewOnlyOther
        let listing = MediaListing()
        await listing.refresh(client, kind: .everything)
        XCTAssertEqual(listing.rows.filter { $0.kind == .other }.map(\.state), [.cannotCompressYet])
        XCTAssertFalse(listing.rows.contains { $0.kind == .other && $0.state == .readyToCompress })
        XCTAssertFalse(listing.estimateIsPartial, "Files Mediaforce can't compress yet need no estimate")
        XCTAssertEqual(listing.estimatedTotalSavedBytes(savedSoFar: 10), 140)

        let vr = MediaListing()
        await vr.refresh(client, kind: .other)
        XCTAssertFalse(vr.canCompressAny, "VR & 3D shows no Can save total rather than a zero")
        XCTAssertEqual(Words.canSave(try XCTUnwrap(vr.rows.first)), "")
    }

    @MainActor
    private static func client() -> (MediaforceClient, URLSession) {
        let config = URLSessionConfiguration.ephemeral
        config.protocolClasses = [PartialLibraryServer.self]
        let session = URLSession(configuration: config)
        return (MediaforceClient(baseURL: URL(string: "http://mediaforce.invalid")!, session: session), session)
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
    enum Mode { case populated, failing, empty, complete, partial, truncated, unmeasured, viewOnlyOther }
    nonisolated(unsafe) static var mode = Mode.populated
    override class func canInit(with request: URLRequest) -> Bool { true }
    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }

    override func startLoading() {
        let body: String
        switch request.url?.path {
        case "/api/app/tv-shows" where Self.mode == .empty:
            body = "{\"shows\": []}"
        case "/api/app/tv-shows" where [.complete, .partial, .truncated, .viewOnlyOther].contains(Self.mode):
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
            let estimate = Self.mode == .populated ? "null" : Self.mode == .unmeasured ? "0" : "30"
            body = """
            {"movies": [{"prefix": "films/Example", "title": "A movie", "file_count": 1,
                         "bytes": 70, "state": "ready", "estimated_saved_bytes": \(estimate)}]}
            """
        case "/api/app/other" where [.complete, .partial].contains(Self.mode):
            body = """
            {"items": [{"prefix": "other/Example", "title": "A folder", "file_count": 1,
                        "bytes": 50, "state": "ready", "estimated_saved_bytes": \(Self.mode == .partial ? "null" : "20")}], "truncated": false}
            """
        case "/api/app/other" where Self.mode == .truncated: body = "{\"items\": [], \"truncated\": true}"
        case "/api/app/other" where Self.mode == .viewOnlyOther:
            body = """
            {"items": [{"prefix": "other/VR", "title": "A VR folder", "file_count": 3,
                        "bytes": 90, "state": "view_only", "estimated_saved_bytes": null}], "truncated": false}
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
