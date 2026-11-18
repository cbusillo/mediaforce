import Foundation
import XCTest
@testable import Mediaforce

@MainActor
final class StopFileTests: XCTestCase {
    nonisolated(unsafe) static var requests: [URLRequest] = []

    nonisolated private static let stillRunning = ActivityTests.payload
    nonisolated private static let waitingAgain = ActivityTests.payload
        .replacingOccurrences(of: #""id":"job-4""#, with: #""id":"job-5""#)

    private func model(_ respond: @escaping @Sendable (URLRequest) -> (Int, String)) -> MainWindowModel {
        Self.requests = []
        StubServer.respond = { request in
            StopFileTests.requests.append(request)
            let (status, body) = respond(request)
            StubServer.body = Data(body.utf8)
            return HTTPURLResponse(url: request.url!, statusCode: status, httpVersion: nil, headerFields: nil)!
        }
        let configuration = URLSessionConfiguration.ephemeral
        configuration.protocolClasses = [StubServer.self]
        let model = MainWindowModel(client: MediaforceClient(baseURL: ServerConnection.localServer, session: URLSession(configuration: configuration)))
        model.stopCheckInterval = .zero
        return model
    }

    override func tearDown() {
        StubServer.respond = nil
        StubServer.body = Data("{}".utf8)
        super.tearDown()
    }

    nonisolated private static func body(_ request: URLRequest) -> [String: String]? {
        if let body = request.httpBody { return try? JSONDecoder().decode([String: String].self, from: body) }
        guard let stream = request.httpBodyStream else { return nil }
        stream.open()
        defer { stream.close() }
        var data = Data()
        var buffer = [UInt8](repeating: 0, count: 1024)
        while stream.hasBytesAvailable {
            let count = stream.read(&buffer, maxLength: buffer.count)
            guard count > 0 else { break }
            data.append(buffer, count: count)
        }
        return try? JSONDecoder().decode([String: String].self, from: data)
    }

    private var stopRequests: [URLRequest] { Self.requests.filter { $0.httpMethod == "POST" } }
    private var activityReads: Int { Self.requests.filter { $0.url?.path == "/api/app/activity" }.count }

    func testStopNamesTheOneRunAndStaysStoppingUntilActivityNoLongerShowsItRunning() async throws {
        let model = model { request in
            switch request.url?.path {
            case "/api/encode-queue/running/job-4/stop": (200, #"{"ok": true}"#)
            case "/api/app/activity": StopFileTests.requests.filter { $0.url?.path == "/api/app/activity" }.count < 2
                ? (200, StopFileTests.stillRunning) : (200, StopFileTests.waitingAgain)
            default: (200, MainWindowTests.facts)
            }
        }

        await model.stop("job-4", run: "2026-10-31T14:00:00+00:00")

        XCTAssertEqual(stopRequests.map(\.url?.path), ["/api/encode-queue/running/job-4/stop"])
        let body = try XCTUnwrap(stopRequests.first.flatMap(StopFileTests.body))
        XCTAssertEqual(body["run"], "2026-10-31T14:00:00+00:00")
        XCTAssertEqual(activityReads, 2)
        XCTAssertEqual(model.stopping, [])
        XCTAssertNil(model.notice)
        XCTAssertEqual(model.activity.facts?.computers.first?.running.map(\.id), ["job-5"])
    }

    func testEachRefusalSaysWhyNothingWasStopped() async {
        for (code, message) in [("not_running", "That file isn't running anymore."),
                                ("started_again", "That file already started again."),
                                ("finishing", "That file is already finishing.")] {
            let model = model { request in
                request.httpMethod == "POST" ? (409, #"{"ok": false, "code": "\#(code)"}"#) : (200, MainWindowTests.facts)
            }

            await model.stop("job-4", run: "2026-10-31T14:00:00+00:00")

            XCTAssertEqual(model.notice?.message, message, code)
            XCTAssertEqual(model.stopping, [])
            XCTAssertEqual(activityReads, 0)
        }
    }

    func testAStopWithNoAnswerSaysSo() async {
        let model = model { _ in (500, "") }

        await model.stop("job-4", run: "2026-10-31T14:00:00+00:00")

        XCTAssertEqual(model.notice?.title, ActionNotice.unanswered.title)
        XCTAssertEqual(model.stopping, [])
    }

    func testOnlyAFileHalfDoneOrMoreAsksFirst() {
        XCTAssertFalse(StopFileButton.asksFirst(nil))
        XCTAssertFalse(StopFileButton.asksFirst(0.49))
        XCTAssertTrue(StopFileButton.asksFirst(0.5))
    }

    func testARunningFileOnAComputerCarriesTheJobItsStopNames() throws {
        let running = #"{"id": "job-9", "run": "2026-11-13T17:00:00+00:00", "prefix": "tv/Show/Season 1", "show_prefix": "tv/Show", "show": "Show", "season": 1, "episode": 4}"#
        let finished = #"{"prefix": "tv/Show/Season 1", "show_prefix": "tv/Show", "show": "Show", "season": 1, "episode": 3}"#
        let file = try JSONDecoder.mediaforce.decode(ComputersFacts.File.self, from: Data(running.utf8))
        XCTAssertEqual([file.job, file.run], ["job-9", "2026-11-13T17:00:00+00:00"])
        XCTAssertNil(try JSONDecoder.mediaforce.decode(ComputersFacts.File.self, from: Data(finished.utf8)).job)
    }
}
