import Foundation

enum ServerState: Equatable {
    case checking
    case reachable
    case unreachable
    case failing
}

/// What went wrong when the app last asked Mediaforce for its facts, and when.
struct ServerProblem: Equatable, Sendable {
    enum Kind: Equatable, Sendable {
        case noAnswer, errorReply, unreadableReply
    }

    let kind: Kind
    let at: Date
}

enum ServerConnection {
    static let localServer = URL(string: "http://127.0.0.1:8777")!
}
