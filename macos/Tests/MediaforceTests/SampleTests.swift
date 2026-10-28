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
        XCTAssertEqual(try decode(Self.facts(state: "making_test", action: nil)).actions, [.openSample])
        XCTAssertEqual(try decode(Self.facts(state: "sample_waiting", action: nil)).actions, [.openSample])
        XCTAssertEqual(try decode(Self.facts(state: "making_season", action: nil)).actions, [])
        XCTAssertTrue(try decode(Self.facts(state: "sample_waiting", action: nil)).isInProgress)
        XCTAssertTrue(try decode(Self.facts(state: "making_season", action: nil)).isInProgress)
        XCTAssertFalse(try decode(Self.facts(state: "finished", action: nil)).isInProgress)
        XCTAssertEqual(try decode(Self.facts(state: "ready_to_make", action: "queue_encode")).actions, [.startCompressing])
        let laterStages: [(String, String?, SampleFacts.Action)] = [
            ("needs_help", "retry_encode", .retryUnfinished), ("ready_to_check", "validate_outputs", .checkFiles),
            ("ready_to_finish", "promote_outputs", .replaceOriginals), ("motion_held", "decide_motion", .decideMotion),
        ]
        // A blocked file and a state with no native step still say where the season is, with no button.
        let noNativeStep: [(String, String?)] = [
            ("needs_help", "review_files"), ("finish_blocked", nil), ("ready_to_check", nil), ("ready_to_finish", nil),
        ]
        let makeSampleHeadline = Words.sampleHeadline(try decode(Self.facts(state: "needs_help", action: "make_sample")))
        for (state, action, expected) in laterStages.map({ ($0.0, $0.1, [$0.2]) }) + noNativeStep.map({ ($0.0, $0.1, []) }) {
            let facts = try decode(Self.facts(state: state, action: action))
            XCTAssertEqual(facts.actions, expected, state)
            XCTAssertNotEqual(Words.sampleHeadline(facts), makeSampleHeadline, state)
        }
    }

    func testCoverageDescribesOnlyASavedSampleWhileNotMakingAnother() throws {
        let summary = "Tested 1 of 106 files. Not yet tested: Season 3, H.264 video."
        let data = Data(Self.facts().utf8)
        var payload = try XCTUnwrap(JSONSerialization.jsonObject(with: data) as? [String: Any])
        payload["coverage_summary"] = summary
        let recorded = try JSONDecoder.mediaforce.decode(SampleFacts.self, from: JSONSerialization.data(withJSONObject: payload))
        XCTAssertEqual(Words.sampleCoverage(recorded), summary)
        XCTAssertEqual(Words.sampleCoverage(try decode(Self.facts())), Words.sampleCoverageUnknown)
        payload["coverage_summary"] = NSNull()
        let unrecorded = try JSONDecoder.mediaforce.decode(SampleFacts.self, from: JSONSerialization.data(withJSONObject: payload))
        XCTAssertEqual(Words.sampleCoverage(unrecorded), Words.sampleCoverageUnknown)
        for state in SampleFacts.sampleMakingStates {
            payload["state"] = state
            let running = try JSONDecoder.mediaforce.decode(SampleFacts.self, from: JSONSerialization.data(withJSONObject: payload))
            XCTAssertNil(Words.sampleCoverage(running))
        }
        payload["state"] = "needs_test"
        payload["draft_hash"] = NSNull()
        let notMade = try JSONDecoder.mediaforce.decode(SampleFacts.self, from: JSONSerialization.data(withJSONObject: payload))
        XCTAssertNil(Words.sampleCoverage(notMade))
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

    func testAKindsPageNamesTheSeasonAndARunningSampleSaysWhenItShouldBeReady() throws {
        let running = try decode(Self.facts(state: "making_test", action: nil).replacingOccurrences(
            of: #""size_choices""#, with: #""ready_in_seconds": 1500, "size_choices""#))
        let now = try XCTUnwrap(ISO8601DateFormatter().date(from: "2026-10-26T23:07:00Z"))
        let named = Words.sampleHeadline(running, namesSeason: true, now: now)
        XCTAssertTrue(named.contains("Lucifer") && named.contains("Season 2"))
        // Ready in 25 minutes, rounded up to the next five.
        let readyBy = try XCTUnwrap(ISO8601DateFormatter().date(from: "2026-10-26T23:35:00Z"))
        XCTAssertTrue(named.hasSuffix("\(readyBy.formatted(date: .omitted, time: .shortened))."), named)
        XCTAssertTrue(Words.sampleHeadline(running, namesSeason: false, now: now).contains(readyBy.formatted(date: .omitted, time: .shortened)))
        // Without enough finished samples to tell, there is no time.
        let unknown = try decode(Self.facts(state: "making_test", action: nil))
        XCTAssertFalse(Words.sampleHeadline(unknown, namesSeason: true, now: now).contains("Ready"))
        let ready = try decode(Self.facts())
        XCTAssertNotEqual(Words.sampleHeadline(ready, namesSeason: true, now: now), Words.sampleHeadline(ready))
        XCTAssertTrue(Words.sampleHeadline(ready, namesSeason: true, now: now).contains("Lucifer"))
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

    func testOnlyAReasonThatNeedsAChoiceIsSaidAsOne() async throws {
        let needsChoice = try await client(status: 200, body: #"{"ok": true, "compressing": false, "reason": "confirm_compression_goal"}"#)
            .compressSeason("tv/Lucifer/Season 2")
        XCTAssertTrue(needsChoice.needsChoice)
        let waiting = try await client(status: 200, body: #"{"ok": true, "compressing": false, "reason": "free_space_reserve"}"#)
            .compressSeason("tv/Lucifer/Season 2")
        XCTAssertFalse(waiting.needsChoice)
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
