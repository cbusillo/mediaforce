import Foundation

enum ServerState: Equatable {
    case checking
    case reachable
    case unreachable
    case failing
}

enum ServerConnection {
    static let localServer = URL(string: "http://127.0.0.1:8777")!
}
