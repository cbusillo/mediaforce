import Foundation

/// Each kind of content, what it covers and how it is decided, from `GET /api/app/kinds`. The server sends facts
/// and codes; the app words them.
struct KindsFacts: Decodable, Sendable, Equatable {
    let kinds: [Kind]
    let leftOut: [LeftOutSeason]

    struct Kind: Decodable, Sendable, Identifiable, Equatable {
        let kind: String
        let oneTime: Bool
        let seasons: Int
        let waitingBytes: Int64
        // The seasons this kind covers, by show; a show with its own approval follows it and is never here.
        let shows: [Show]
        // Shows already approved on a season of this kind, whose setting the kind could use.
        let settings: [Setting]
        let approval: Approval?
        var id: String { kind }

        /// The setting the page plays first: the decided one, else the first with an episode to watch.
        var firstSetting: Setting? {
            if let approval {
                return settings.first { $0.fromPrefix == approval.fromPrefix } ?? approval.setting
            }
            return settings.first { $0.libraryItemId != nil } ?? settings.first
        }
    }

    struct Show: Decodable, Sendable, Identifiable, Equatable {
        let prefix: String
        let seasons: Int
        let waitingBytes: Int64
        var id: String { prefix }
        var name: String { String(prefix.split(separator: "/").last ?? "") }
    }

    struct Setting: Decodable, Sendable, Identifiable, Equatable {
        let show: String
        let fromPrefix: String
        let sizeGoalMode: String?
        let sizeGoalMb: Double?
        // A published episode made under it whose original backup is kept, to compare; nil when none is.
        let libraryItemId: Int?
        var id: String { show }
        var name: String { String(show.split(separator: "/").last ?? "") }
    }

    struct Approval: Decodable, Sendable, Equatable {
        let fromPrefix: String
        let approvedAt: String?
        let sizeGoalMode: String?
        let sizeGoalMb: Double?
        let libraryItemId: Int?

        var showPrefix: String { fromPrefix.split(separator: "/").prefix(2).joined(separator: "/") }
        var setting: Setting {
            Setting(show: showPrefix, fromPrefix: fromPrefix, sizeGoalMode: sizeGoalMode, sizeGoalMb: sizeGoalMb, libraryItemId: libraryItemId)
        }
    }

    struct LeftOutSeason: Decodable, Sendable, Equatable {
        let season: String
        // "mixed" when its episodes measure as different kinds, "unmeasured" before its originals are measured.
        let kind: String
        let waitingBytes: Int64
    }
}

struct KindActionResult: Decodable, Sendable {
    let ok: Bool
    let code: String?
}

extension MediaforceClient {
    func kinds() async throws -> KindsFacts {
        var request = URLRequest(url: baseURL.appending(path: "api/app/kinds"))
        request.timeoutInterval = Self.factsTimeout
        let (data, response) = try await session.data(for: request)
        guard (response as? HTTPURLResponse)?.statusCode == 200 else { throw URLError(.badServerResponse) }
        return try JSONDecoder.mediaforce.decode(KindsFacts.self, from: data)
    }

    /// Uses a show's own approval for every season of the kind; a refusal answers 409 with a code the app words.
    func approveKind(_ kind: String, from prefix: String) async throws -> KindActionResult {
        try await kindAction(kind, "approve", body: ["from_prefix": prefix])
    }

    func stopKind(_ kind: String) async throws -> KindActionResult {
        try await kindAction(kind, "stop", body: [:])
    }

    private func kindAction(_ kind: String, _ action: String, body: [String: String]) async throws -> KindActionResult {
        var request = URLRequest(url: baseURL.appending(path: "api/app/kinds").appending(path: kind).appending(path: action))
        request.httpMethod = "POST"
        request.httpBody = try JSONEncoder().encode(body)
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        let (data, response) = try await session.data(for: request)
        guard [200, 409].contains((response as? HTTPURLResponse)?.statusCode) else { throw URLError(.badServerResponse) }
        return try JSONDecoder.mediaforce.decode(KindActionResult.self, from: data)
    }
}
