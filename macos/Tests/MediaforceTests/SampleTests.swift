import Foundation
import XCTest
@testable import Mediaforce

final class SampleTests: XCTestCase {
    static func facts(state: String = "ready_to_compare", action: String? = "review_sample", choices: String = #"[{"key": "smaller", "target_bytes": 220300000}]"#) -> String {
        """
        {"prefix": "tv/Lucifer/Season 2", "show_prefix": "tv/Lucifer", "show": "Lucifer", "season_folder": "Season 2",
         "season": 2, "state": "\(state)", "action": \(action.map { "\"\($0)\"" } ?? "null"), "failure": null,
         "moments": [{"at_seconds": 600.0, "original": {"url": "/review-media/Lucifer S2/source.mp4", "size_bytes": 9000000},
                      "sample": {"url": "/review-media/Lucifer S2/preview.mp4", "size_bytes": 700000}}],
         "episode_bytes": 3853264012, "estimated_episode_bytes": 296440462, "season_bytes": 60000000000,
         "season_episode_count": 16, "goal_bytes": 293736333, "size_status": "over_target", "draft_hash": "draft-1",
         "size_choices": \(choices)}
        """
    }

    private func decode(_ json: String) throws -> SampleFacts {
        try JSONDecoder.mediaforce.decode(SampleFacts.self, from: Data(json.utf8))
    }

    func testFactsDecodeFromServerShape() throws {
        let facts = try decode(Self.facts())
        XCTAssertEqual(facts.moments.first?.sample.sizeBytes, 700_000)
        XCTAssertEqual(facts.sizeChoices.map(\.key), ["smaller"])
        XCTAssertEqual(facts.estimatedSavedBytes, 60_000_000_000 - 296_440_462 * 16)
        XCTAssertFalse(facts.isInProgress)
    }

    func testEachStateShowsOnlyItsActions() throws {
        XCTAssertEqual(try decode(Self.facts()).actions, [.approve, .makeAgain, .tryAnotherSize])
        XCTAssertEqual(try decode(Self.facts(state: "needs_help", action: "retry_sample", choices: "[]")).actions, [.makeSample])
        XCTAssertEqual(try decode(Self.facts(state: "needs_help", action: "choose_size")).actions, [.chooseSize])
        XCTAssertEqual(try decode(Self.facts(state: "making_test", action: nil)).actions, [])
        XCTAssertTrue(try decode(Self.facts(state: "sample_waiting", action: nil)).isInProgress)
        XCTAssertTrue(try decode(Self.facts(state: "making_season", action: nil)).isInProgress)
        XCTAssertFalse(try decode(Self.facts(state: "finished", action: nil)).isInProgress)
        XCTAssertEqual(try decode(Self.facts(state: "ready_to_make", action: "queue_encode")).actions, [.startCompressing])
        let laterStages: [(String, String?)] = [("needs_help", "retry_encode"), ("needs_help", "review_files"), ("ready_to_check", "validate_outputs"),
                           ("ready_to_finish", "promote_outputs"), ("motion_held", "decide_motion"), ("finish_blocked", nil),
                           ("ready_to_check", nil), ("ready_to_finish", nil)]
        for (state, action) in laterStages {
            let facts = try decode(Self.facts(state: state, action: action))
            XCTAssertEqual(facts.actions, [.openItem], state)
            XCTAssertNotEqual(Words.sampleHeadline(facts), Words.sampleHeadline(try decode(Self.facts(state: "needs_help", action: "make_sample"))), state)
        }
    }

    func testAnApprovalHoldsOnlyForTheSampleTheOwnerWatched() throws {
        let watched = try decode(Self.facts())
        let approval = SampleApproval(reviewedDraftHash: try XCTUnwrap(watched.draftHash))
        XCTAssertTrue(approval.stillReviewed(watched))
        let replaced = try decode(Self.facts().replacingOccurrences(of: #""draft-1""#, with: #""draft-2""#))
        XCTAssertFalse(approval.stillReviewed(replaced))
        XCTAssertFalse(approval.stillReviewed(nil))
    }

    func testEveryStateHasItsOwnHeadlineAndOnlyKnownGuardsAsk() throws {
        let states = ["making_test", "sample_waiting", "ready_to_compare", "needs_test", "ready_to_make", "making_season", "finished"]
        let headlines = try states.map { Words.sampleHeadline(try decode(Self.facts(state: $0))) }
        XCTAssertEqual(Set(headlines).count, states.count)
        XCTAssertFalse(headlines.contains(Words.sampleHeadline(try decode(Self.facts(state: "a state the app does not know")))))
        XCTAssertNotNil(Words.sampleConfirmation("confirm_high_impact"))
        XCTAssertNotNil(Words.sampleConfirmation("confirm_size_tradeoff"))
        XCTAssertNil(Words.sampleConfirmation("refused"))
    }

    func testMomentTimesReadAsClockTimes() {
        XCTAssertEqual(Words.momentTime(605), "10:05")
    }
}

final class SampleClientTests: XCTestCase {
    nonisolated(unsafe) static var requests: [URLRequest] = []

    private func client(status: Int, body: String) -> MediaforceClient {
        StubServer.body = Data(body.utf8)
        StubServer.respond = { request in
            SampleClientTests.requests.append(request)
            return HTTPURLResponse(url: request.url!, statusCode: status, httpVersion: nil, headerFields: nil)!
        }
        let configuration = URLSessionConfiguration.ephemeral
        configuration.protocolClasses = [StubServer.self]
        return MediaforceClient(baseURL: ServerConnection.localServer, session: URLSession(configuration: configuration))
    }

    override func setUp() {
        super.setUp()
        Self.requests = []
    }

    override func tearDown() {
        StubServer.respond = nil
        StubServer.body = Data("{}".utf8)
        super.tearDown()
    }

    func testARefusedApprovalIsAnAnswerNotAFailure() async throws {
        let result = try await client(status: 409, body: #"{"ok": false, "code": "confirm_size_tradeoff"}"#)
            .approveSample("tv/Lucifer/Season 2", confirm: ["high_impact"], draftHash: "draft-1")
        XCTAssertEqual(result.code, "confirm_size_tradeoff")
        let request = try XCTUnwrap(Self.requests.first)
        XCTAssertEqual(request.httpMethod, "POST")
        XCTAssertEqual(request.url?.path(percentEncoded: false), "/api/app/samples/tv/Lucifer/Season 2/approve")
        let sent = try XCTUnwrap(StubServerBody.json(request))
        XCTAssertEqual(sent["confirm"] as? [String], ["high_impact"])
        XCTAssertEqual(sent["reviewed_draft_hash"] as? String, "draft-1")
    }

    func testOnlyAChoiceTheBrowserAsksForSendsTheOwnerThere() async throws {
        let needsChoice = try await client(status: 200, body: #"{"ok": true, "compressing": false, "reason": "confirm_compression_goal"}"#)
            .compressSeason("tv/Lucifer/Season 2")
        XCTAssertTrue(needsChoice.needsBrowser)
        let waiting = try await client(status: 200, body: #"{"ok": true, "compressing": false, "reason": "free_space_reserve"}"#)
            .compressSeason("tv/Lucifer/Season 2")
        XCTAssertFalse(waiting.needsBrowser)
    }

    func testAServerErrorOnAnActionThrows() async {
        do {
            _ = try await client(status: 500, body: "").makeSample("tv/Lucifer/Season 2", size: nil)
            XCTFail("a server error must throw")
        } catch {}
    }

    func testReviewClipsLoadFromTheServer() {
        let url = client(status: 200, body: "{}").reviewMedia("/review-media/Lucifer S2/preview.mp4")
        XCTAssertEqual(url.absoluteString, "http://127.0.0.1:8777/review-media/Lucifer%20S2/preview.mp4")
    }
}

enum StubServerBody {
    static func json(_ request: URLRequest) -> [String: Any]? {
        guard let data = request.httpBody ?? request.httpBodyStream.map(read) else { return nil }
        return (try? JSONSerialization.jsonObject(with: data)) as? [String: Any]
    }

    private static func read(_ stream: InputStream) -> Data {
        stream.open()
        defer { stream.close() }
        var data = Data()
        var buffer = [UInt8](repeating: 0, count: 4096)
        while stream.hasBytesAvailable {
            let count = stream.read(&buffer, maxLength: buffer.count)
            guard count > 0 else { break }
            data.append(buffer, count: count)
        }
        return data
    }
}
