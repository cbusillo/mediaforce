import Foundation
import XCTest
@testable import Mediaforce

final class StubServer: URLProtocol, @unchecked Sendable {
    nonisolated(unsafe) static var respond: (@Sendable (URLRequest) throws -> HTTPURLResponse)?
    nonisolated(unsafe) static var body = Data("{}".utf8)

    override class func canInit(with request: URLRequest) -> Bool { true }
    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }

    override func startLoading() {
        do {
            guard let respond = Self.respond else { throw URLError(.cannotConnectToHost) }
            client?.urlProtocol(self, didReceive: try respond(request), cacheStoragePolicy: .notAllowed)
            client?.urlProtocol(self, didLoad: Self.body)
            client?.urlProtocolDidFinishLoading(self)
        } catch {
            client?.urlProtocol(self, didFailWithError: error)
        }
    }

    override func stopLoading() {}
}


final class MainWindowTests: XCTestCase {
    static let facts = """
    {
      "schema_version": 1, "observed_at": "2026-10-24T12:00:00+00:00",
      "work": {"state": "stopped", "paused_by_owner": false, "stop_reasons": ["storage_unavailable"],
               "running_count": 0, "waiting_count": 3, "storage": []},
      "library": {"tv": {"show_count": 2, "season_count": 3, "episode_count": 30, "total_bytes": 9000, "original_bytes": 8000},
                  "movies": {"title_count": 1, "total_bytes": 10, "original_bytes": 10},
                  "other": {"item_count": 0, "total_bytes": 0, "original_bytes": 0}},
      "space_saved": {"bytes": 1680000000000, "item_count": 180, "episode_count": 180, "season_count": 54},
      "recently_finished_seasons": [{"prefix": "tv/Futurama/Season 3", "show_prefix": "tv/Futurama", "show": "Futurama",
                                     "season_folder": "Season 3", "season": 3, "episode_count": 15, "kept_original_count": 0,
                                     "bytes_saved": 4234111127, "finished_at": "2026-10-22T04:14:18+00:00"}],
      "biggest_not_started": [{"show_prefix": "tv/ER", "show": "ER", "bytes": 324683591396, "episode_count": 331,
                               "season_count": 15, "waiting_sample_prefix": null}],
      "samples": {"waiting_on_you": [{"prefix": "tv/Lucifer/Season 2", "show_prefix": "tv/Lucifer", "show": "Lucifer",
                                      "season_folder": "Season 2", "season": 2, "state": "needs_help",
                                      "action": "make_sample", "failure": "missing_clips", "needs_you": true}],
                  "approved_not_started": []},
      "computers": [{"key": "mini", "name": "M1 mini", "platform": "macos", "state": "password_needed", "needs_you": true,
                     "running_count": 0, "schedule_opens_at": null, "cooldown_until": null}],
      "events": [{"id": 7, "kind": "promotion_completed", "at": "2026-10-22T04:14:18+00:00",
                  "rel_path": "tv/Futurama/Season 3/Futurama.S03E08.mkv",
                  "season": {"prefix": "tv/Futurama/Season 3", "show_prefix": "tv/Futurama", "show": "Futurama",
                             "season_folder": "Season 3", "season": 3},
                  "bytes_saved": 661000000}],
      "last_change": null,
      "original_backups": {"count": 46, "bytes": 20273378835},
      "kept_original": {"kept": [], "kept_item_count": 2, "held": []}
    }
    """

    func testFactsDecodeFromServerShape() throws {
        let facts = try JSONDecoder.mediaforce.decode(MainWindowFacts.self, from: Data(Self.facts.utf8))
        XCTAssertEqual(facts.work.stopReasons, ["storage_unavailable"])
        XCTAssertEqual(facts.recentlyFinishedSeasons.first?.season, 3)
        XCTAssertEqual(facts.samples.waitingOnYou.first?.action, "make_sample")
        XCTAssertEqual(facts.keptOriginal.keptItemCount, 2)
        XCTAssertNil(facts.lastChange)
    }

    func testSizesUseDecimalUnitsAtTheDesignsPrecision() {
        XCTAssertEqual(Words.size(1_680_000_000_000), "1.68 TB")
        XCTAssertEqual(Words.size(324_683_591_396), "325 GB")
        XCTAssertEqual(Words.size(661_000_000), "661 MB")
    }

    func testOriginalCheckWindowWaitIsShownWithOtherWorkReasons() throws {
        var payload = try XCTUnwrap(JSONSerialization.jsonObject(with: Data(Self.facts.utf8)) as? [String: Any])
        var work = try XCTUnwrap(payload["work"] as? [String: Any])
        let reason = "New originals wait for the named computer."
        work["original_check_wait_reasons"] = [reason]
        payload["work"] = work
        let facts = try JSONDecoder.mediaforce.decode(MainWindowFacts.self, from: JSONSerialization.data(withJSONObject: payload))
        XCTAssertEqual(facts.work.originalCheckWaitReasons, [reason])
        XCTAssertTrue(try XCTUnwrap(Words.workExplanation(facts)).contains(reason))
    }

    func testEveryReasonWorkIsStoppedIsSaid() throws {
        let json = MainWindowTests.facts.replacingOccurrences(of: #""paused_by_owner": false"#, with: #""paused_by_owner": true"#)
        let facts = try JSONDecoder.mediaforce.decode(MainWindowFacts.self, from: Data(json.utf8))
        let explanation = try XCTUnwrap(Words.workExplanation(facts))
        XCTAssertTrue(explanation.contains("resume"))
        XCTAssertTrue(explanation.contains("storage"))
    }

    func testSizeFailuresExplainThemselves() {
        XCTAssertNotNil(Words.sampleFailure("bound_exhausted"))
        XCTAssertNil(Words.sampleFailure("a code the app does not know"))
    }

    func testEpisodeEventsNameTheEpisodeOnce() throws {
        let facts = try JSONDecoder.mediaforce.decode(MainWindowFacts.self, from: Data(Self.facts.utf8))
        XCTAssertEqual(Words.eventSentence(facts.events[0]), "Futurama S03E08 replaced. Saved 661 MB.")
    }
}

final class MediaforceClientActionTests: XCTestCase {
    nonisolated(unsafe) static var requests: [URLRequest] = []

    private func client() -> MediaforceClient {
        let configuration = URLSessionConfiguration.ephemeral
        configuration.protocolClasses = [StubServer.self]
        StubServer.respond = { request in
            MediaforceClientActionTests.requests.append(request)
            return HTTPURLResponse(url: request.url!, statusCode: 200, httpVersion: nil, headerFields: nil)!
        }
        return MediaforceClient(baseURL: ServerConnection.localServer, session: URLSession(configuration: configuration))
    }

    override func setUp() {
        super.setUp()
        Self.requests = []
    }

    override func tearDown() {
        StubServer.respond = nil
        super.tearDown()
    }

    func testEachActionIsItsOwnRequestAndDeletingSendsItsJSON() async throws {
        let client = client()
        try await client.pause()
        try await client.resume()
        StubServer.body = Data(#"{"ok": true, "removed_count": 3, "removed_size_bytes": 3000000000, "preserved_count": 1}"#.utf8)
        let deleted = try await client.deleteCleanupFolder()
        StubServer.body = Data("{}".utf8)

        XCTAssertEqual(Self.requests.map(\.httpMethod), ["POST", "POST", "POST"])
        XCTAssertEqual(Set(Self.requests.compactMap(\.url)).count, 3)
        XCTAssertEqual(Self.requests[2].value(forHTTPHeaderField: "Content-Type"), "application/json")
        XCTAssertEqual(deleted.removedCount, 3)
        XCTAssertEqual(ActionNotice.deleted(deleted).title, "Deleted 3 original backups")
    }

    @MainActor
    func testADeleteRefusedWhilePublishingSaysNothingWasDeleted() async {
        let configuration = URLSessionConfiguration.ephemeral
        configuration.protocolClasses = [StubServer.self]
        StubServer.respond = { request in
            HTTPURLResponse(url: request.url!, statusCode: 200, httpVersion: nil, headerFields: nil)!
        }
        StubServer.body = Data(#"{"ok": false, "removed_count": 0, "removed_size_bytes": 0}"#.utf8)
        let model = MainWindowModel(client: MediaforceClient(baseURL: ServerConnection.localServer, session: URLSession(configuration: configuration)))
        await model.deleteCleanupFolder()
        StubServer.body = Data("{}".utf8)
        XCTAssertEqual(model.notice?.title, ActionNotice.publishing.title)
    }

    func testAFailedAnswerThrows() async {
        let client = client()
        StubServer.respond = { request in
            HTTPURLResponse(url: request.url!, statusCode: 500, httpVersion: nil, headerFields: nil)!
        }
        do {
            try await client.pause()
            XCTFail("a failed answer must throw")
        } catch {}
    }

    func testAMissingPosterIsNoImage() async {
        let configuration = URLSessionConfiguration.ephemeral
        configuration.protocolClasses = [StubServer.self]
        StubServer.respond = { request in
            HTTPURLResponse(url: request.url!, statusCode: 404, httpVersion: nil, headerFields: nil)!
        }
        let client = MediaforceClient(baseURL: ServerConnection.localServer, session: URLSession(configuration: configuration))
        let image = await client.poster(titlePrefix: "tv/Unknown")
        XCTAssertNil(image)
    }
}

@MainActor
final class MainWindowModelTests: XCTestCase {
    private func model(_ respond: @escaping @Sendable (URLRequest) throws -> (Int, Data)) -> MainWindowModel {
        StubServer.respond = { request in
            let (status, body) = try respond(request)
            StubServer.body = body
            return HTTPURLResponse(url: request.url!, statusCode: status, httpVersion: nil, headerFields: nil)!
        }
        let configuration = URLSessionConfiguration.ephemeral
        configuration.protocolClasses = [StubServer.self]
        return MainWindowModel(client: MediaforceClient(baseURL: ServerConnection.localServer, session: URLSession(configuration: configuration)))
    }

    override func tearDown() {
        StubServer.respond = nil
        StubServer.body = Data("{}".utf8)
        super.tearDown()
    }

    func testFactsMakeTheServerReachableAndReachTheListener() async {
        var heard: MainWindowFacts?
        let model = model { _ in (200, Data(MainWindowTests.facts.utf8)) }
        model.onFacts = { heard = $0 }
        await model.refresh()
        XCTAssertEqual(model.connection, .reachable)
        XCTAssertNotNil(model.facts)
        XCTAssertEqual(heard?.originalBackups.count, 46)
    }

    func testServerErrorsAndUnreadableRepliesAreFailing() async {
        let erroring = model { _ in (500, Data()) }
        await erroring.refresh()
        XCTAssertEqual(erroring.connection, .failing)
        XCTAssertEqual(erroring.problem?.kind, .errorReply)
        let garbled = model { _ in (200, Data("not facts".utf8)) }
        await garbled.refresh()
        XCTAssertEqual(garbled.connection, .failing)
        XCTAssertEqual(garbled.problem?.kind, .unreadableReply)
    }

    func testAFailedActionTellsTheOwner() async {
        let model = model { _ in (500, Data()) }
        await model.perform { try await $0.pause() }
        XCTAssertEqual(model.notice?.title, ActionNotice.unanswered.title)
    }

    func testARefusedConnectionIsUnreachable() async {
        let model = model { _ in throw URLError(.cannotConnectToHost) }
        await model.refresh()
        XCTAssertEqual(model.connection, .unreachable)
        XCTAssertEqual(model.problem?.kind, .noAnswer)
        XCTAssertNil(model.facts)
    }
}
