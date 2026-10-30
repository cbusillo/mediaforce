import Foundation
import XCTest
@testable import Mediaforce

final class LibraryTests: XCTestCase {
    @MainActor
    func testAMovieLibraryLoadsWhenTheOtherLibrariesDoNotAnswer() async throws {
        let config = URLSessionConfiguration.ephemeral
        config.protocolClasses = [StubServer.self]
        let session = URLSession(configuration: config)
        defer { session.invalidateAndCancel(); StubServer.respond = nil }
        StubServer.body = Data("{\"movies\": []}".utf8)
        StubServer.respond = { request in
            guard request.url?.path == "/api/app/movies" else { throw URLError(.cannotConnectToHost) }
            return HTTPURLResponse(url: request.url!, statusCode: 200, httpVersion: nil, headerFields: nil)!
        }
        let client = MediaforceClient(baseURL: URL(string: "http://mediaforce.invalid")!, session: session)
        let rows = try await MediaRow.load(client, kind: .movies)
        XCTAssertTrue(rows.isEmpty)
    }

    func testCombinedLibraryIncludesEveryKindAndScopesUseTheRowsKind() throws {
        let rows = [
            MediaRow(id: "series/Example", name: "A show", kind: .tv, detail: "", bytes: 40, saved: nil, state: .needsYou),
            MediaRow(id: "films/Example", name: "A movie", kind: .movies, detail: "", bytes: 70, saved: nil, state: .readyToCompress),
            MediaRow(id: "home/Trip", name: "A trip", kind: .other, detail: "", bytes: 20, saved: nil, state: .noActiveWork)
        ]
        XCTAssertEqual(MediaKind.everything.rows(in: rows).map(\.id), rows.map(\.id))
        for kind in [MediaKind.tv, .movies, .other] {
            XCTAssertEqual(kind.rows(in: rows).map(\.id), rows.filter { $0.kind == kind }.map(\.id))
        }
        let facts = try JSONDecoder.mediaforce.decode(MainWindowFacts.self, from: Data(MainWindowTests.facts.utf8))
        XCTAssertEqual(MediaKind.everything.originalBytes(in: facts.library),
                       facts.library.tv.originalBytes + facts.library.movies.originalBytes + facts.library.other.originalBytes)
        XCTAssertEqual(MediaKind.movies.totalBytes(in: facts.library), facts.library.movies.totalBytes)
        XCTAssertFalse(MediaKind.libraries(in: facts.library).contains(.other))
        let library = MainWindowFacts.Library(tv: facts.library.tv, movies: facts.library.movies,
                                             other: .init(itemCount: 1, totalBytes: 20, originalBytes: 20))
        XCTAssertTrue(MediaKind.libraries(in: library).contains(.other))
        XCTAssertEqual(MediaKind.everything.originalBytes(in: library),
                       MediaKind.tv.originalBytes(in: library) + MediaKind.movies.originalBytes(in: library) + MediaKind.other.originalBytes(in: library))
    }
}
