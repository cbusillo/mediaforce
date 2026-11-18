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

    private var stopRequests: [URLRequest] { Self.requests.filter { $0.httpMethod == "POST" } }
    private var activityReads: Int { Self.requests.filter { $0.url?.path == "/api/app/activity" }.count }

    func testStopNamesTheOneFileAndStaysStoppingUntilActivityNoLongerShowsItRunning() async {
        let model = model { request in
            switch request.url?.path {
            case "/api/encode-queue/running/job-4/stop": (200, #"{"ok": true}"#)
            case "/api/app/activity": StopFileTests.requests.filter { $0.url?.path == "/api/app/activity" }.count < 2
                ? (200, StopFileTests.stillRunning) : (200, StopFileTests.waitingAgain)
            default: (200, MainWindowTests.facts)
            }
        }

        await model.stop("job-4")

        XCTAssertEqual(stopRequests.map(\.url?.path), ["/api/encode-queue/running/job-4/stop"])
        XCTAssertEqual(activityReads, 2)
        XCTAssertEqual(model.stopping, [])
        XCTAssertNil(model.notice)
        XCTAssertEqual(model.activity.facts?.computers.first?.running.map(\.id), ["job-5"])
    }

    func testAFileThatAlreadyFinishedSaysNothingWasStopped() async {
        let model = model { request in
            request.httpMethod == "POST" ? (409, #"{"ok": false, "code": "not_running"}"#) : (200, MainWindowTests.facts)
        }

        await model.stop("job-4")

        XCTAssertEqual(model.notice?.title, ActionNotice.notRunning.title)
        XCTAssertEqual(model.stopping, [])
        XCTAssertEqual(activityReads, 0)
    }

    func testAStopWithNoAnswerSaysSo() async {
        let model = model { _ in (500, "") }

        await model.stop("job-4")

        XCTAssertEqual(model.notice?.title, ActionNotice.unanswered.title)
        XCTAssertEqual(model.stopping, [])
    }

    func testOnlyAFileHalfDoneOrMoreAsksFirst() {
        XCTAssertFalse(StopFileButton.asksFirst(nil))
        XCTAssertFalse(StopFileButton.asksFirst(0.49))
        XCTAssertTrue(StopFileButton.asksFirst(0.5))
    }

    func testARunningFileOnAComputerCarriesTheJobItsStopNames() throws {
        let running = #"{"id": "job-9", "prefix": "tv/Show/Season 1", "show_prefix": "tv/Show", "show": "Show", "season": 1, "episode": 4}"#
        let finished = #"{"prefix": "tv/Show/Season 1", "show_prefix": "tv/Show", "show": "Show", "season": 1, "episode": 3}"#
        XCTAssertEqual(try JSONDecoder.mediaforce.decode(ComputersFacts.File.self, from: Data(running.utf8)).job, "job-9")
        XCTAssertNil(try JSONDecoder.mediaforce.decode(ComputersFacts.File.self, from: Data(finished.utf8)).job)
    }
}
