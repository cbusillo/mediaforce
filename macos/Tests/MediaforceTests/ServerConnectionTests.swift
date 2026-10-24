import Foundation
import XCTest
@testable import Mediaforce

final class StubServer: URLProtocol, @unchecked Sendable {
    nonisolated(unsafe) static var respond: (@Sendable (URLRequest) throws -> HTTPURLResponse)?

    override class func canInit(with request: URLRequest) -> Bool { true }
    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }

    override func startLoading() {
        do {
            guard let respond = Self.respond else { throw URLError(.cannotConnectToHost) }
            client?.urlProtocol(self, didReceive: try respond(request), cacheStoragePolicy: .notAllowed)
            client?.urlProtocol(self, didLoad: Data("{}".utf8))
            client?.urlProtocolDidFinishLoading(self)
        } catch {
            client?.urlProtocol(self, didFailWithError: error)
        }
    }

    override func stopLoading() {}
}

final class ServerConnectionTests: XCTestCase {
    private func connection() -> ServerConnection {
        let configuration = URLSessionConfiguration.ephemeral
        configuration.protocolClasses = [StubServer.self]
        return ServerConnection(baseURL: ServerConnection.localServer, session: URLSession(configuration: configuration))
    }

    private func answer(status: Int) {
        StubServer.respond = { request in
            HTTPURLResponse(url: request.url!, statusCode: status, httpVersion: nil, headerFields: nil)!
        }
    }

    override func tearDown() {
        StubServer.respond = nil
        super.tearDown()
    }

    func testAnsweringServerIsReachable() async {
        answer(status: 200)
        let state = await connection().check()
        XCTAssertEqual(state, .reachable)
    }

    func testServerErrorIsFailingNotReachable() async {
        answer(status: 500)
        let state = await connection().check()
        XCTAssertEqual(state, .failing)
    }

    func testRefusedConnectionIsUnreachable() async {
        StubServer.respond = { _ in throw URLError(.cannotConnectToHost) }
        let state = await connection().check()
        XCTAssertEqual(state, .unreachable)
    }

    func testTimeoutIsUnreachable() async {
        StubServer.respond = { _ in throw URLError(.timedOut) }
        let state = await connection().check()
        XCTAssertEqual(state, .unreachable)
    }

    @MainActor
    func testMonitorReportsLatestCheck() async {
        let monitor = ServerMonitor(connection: connection())
        XCTAssertEqual(monitor.state, .checking)
        answer(status: 200)
        await monitor.refresh()
        XCTAssertEqual(monitor.state, .reachable)
        StubServer.respond = { _ in throw URLError(.cannotConnectToHost) }
        await monitor.refresh()
        XCTAssertEqual(monitor.state, .unreachable)
    }
}
