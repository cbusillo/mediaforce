import Foundation

/// What the server knows about each computer for the Computers pages, from `/api/app/computers`: what it is, when it
/// last answered, what it is doing, when it works, what it has done, and the fixes it offers.
struct ComputersFacts: Decodable, Sendable {
    let observedAt: String
    let computers: [Machine]

    struct Machine: Decodable, Sendable, Identifiable {
        let key: String
        let name: String?
        let model: String?
        let thisMac: Bool
        let platform: String?
        let lastAnsweredAt: String?
        let abilities: [String]
        let libraryKinds: [String]
        let storage: String
        let window: Window
        let running: [File]
        let done: Done
        let hourTakesSeconds: Int?
        let recent: [File]
        /// The same fields `/api/hosts` gives, so the fixes follow one rule.
        let host: HostsFacts.Host
        var id: String { key }

        private enum CodingKeys: String, CodingKey {
            case key, name, model, thisMac, platform, lastAnsweredAt, abilities, libraryKinds, storage, window, running, done,
                 hourTakesSeconds, recent
        }

        init(from decoder: Decoder) throws {
            let container = try decoder.container(keyedBy: CodingKeys.self)
            key = try container.decode(String.self, forKey: .key)
            name = try container.decodeIfPresent(String.self, forKey: .name)
            model = try container.decodeIfPresent(String.self, forKey: .model)
            thisMac = try container.decode(Bool.self, forKey: .thisMac)
            platform = try container.decodeIfPresent(String.self, forKey: .platform)
            lastAnsweredAt = try container.decodeIfPresent(String.self, forKey: .lastAnsweredAt)
            abilities = try container.decode([String].self, forKey: .abilities)
            libraryKinds = try container.decode([String].self, forKey: .libraryKinds)
            storage = try container.decode(String.self, forKey: .storage)
            window = try container.decode(Window.self, forKey: .window)
            running = try container.decode([File].self, forKey: .running)
            done = try container.decode(Done.self, forKey: .done)
            hourTakesSeconds = try container.decodeIfPresent(Int.self, forKey: .hourTakesSeconds)
            recent = try container.decode([File].self, forKey: .recent)
            host = try HostsFacts.Host(from: decoder)
        }

        var compresses: Bool { abilities.contains("encode_queue") }
        var makesSamples: Bool { abilities.contains("sample_calibration") }
    }

    struct Window: Decodable, Sendable {
        let name: String
        let open: Bool
        let closesAt: String?
        let opensAt: String?
        let week: [Span]

        /// When the next window that opens at `opensAt` closes again.
        var nextClosesAt: String? {
            guard let opensAt, let start = ActivityFacts.date(opensAt) else { return nil }
            return week.first { ActivityFacts.date($0.startsAt) == start }?.endsAt
        }
    }

    struct Span: Decodable, Sendable {
        let startsAt: String
        let endsAt: String
    }

    struct Done: Decodable, Sendable {
        let today: Tally
        let week: Tally
        let all: Tally
        let since: String?
    }

    struct Tally: Decodable, Sendable {
        let files: Int
        let bytesSaved: Int64
    }

    /// A file compressing now, or one finished; named by its show, season and episode rather than its file name.
    struct File: Decodable, Sendable, Identifiable {
        let prefix: String
        let showPrefix: String
        let show: String
        let season: Int?
        let episode: Int?
        var stage: String?
        var progress: Double?
        var finishesAt: String?
        var fileCount: Int?
        var finishedAt: String?
        var bytesSaved: Int64?
        var replaced: Bool?
        var id: String { "\(prefix)/\(episode ?? 0)/\(finishedAt ?? "")" }
        var mediaKind: MediaKind { MediaKind(rawValue: String(prefix.split(separator: "/").first ?? "")) ?? .other }
    }
}

extension MediaforceClient {
    func computers() async throws -> ComputersFacts {
        var request = URLRequest(url: baseURL.appending(path: "api/app/computers"))
        request.timeoutInterval = Self.factsTimeout
        return try await decode(ComputersFacts.self, from: request)
    }
}
