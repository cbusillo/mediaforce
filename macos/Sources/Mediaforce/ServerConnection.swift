import Foundation
import Observation

enum ServerState: Equatable {
    case checking
    case reachable
    case unreachable
    case failing
}

struct ServerConnection: Sendable {
    static let localServer = URL(string: "http://127.0.0.1:8777")!
    static let requestTimeout: TimeInterval = 5

    let baseURL: URL
    var session: URLSession = .shared

    func check() async -> ServerState {
        var request = URLRequest(url: baseURL.appending(path: "api/dashboard/scan-job"))
        request.timeoutInterval = Self.requestTimeout
        guard let (_, response) = try? await session.data(for: request) else {
            return .unreachable
        }
        guard let status = (response as? HTTPURLResponse)?.statusCode, (200..<300).contains(status) else {
            return .failing
        }
        return .reachable
    }
}

@MainActor
@Observable
final class ServerMonitor {
    static let checkInterval: Duration = .seconds(5)

    private(set) var state: ServerState = .checking
    private let connection: ServerConnection

    init(connection: ServerConnection) {
        self.connection = connection
    }

    func refresh() async {
        state = await connection.check()
    }

    func watch() async {
        while !Task.isCancelled {
            await refresh()
            try? await Task.sleep(for: Self.checkInterval)
        }
    }
}
