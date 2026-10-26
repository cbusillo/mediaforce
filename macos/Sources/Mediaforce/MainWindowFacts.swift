import Foundation

struct MainWindowFacts: Decodable, Sendable {
    let observedAt: String
    let work: Work
    let library: Library
    let spaceSaved: SpaceSaved
    let recentlyFinishedSeasons: [FinishedSeason]
    let biggestNotStarted: [NotStartedShow]
    let samples: Samples
    let computers: [Computer]
    let events: [Event]
    let lastChange: Event?
    let originalBackups: OriginalBackups
    let keptOriginal: KeptOriginal

    struct Work: Decodable, Sendable {
        let state: String
        let pausedByOwner: Bool
        let stopReasons: [String]
        let runningCount: Int
        // The seasons or titles being compressed now, each named once.
        var runningSeasons: [Season] = []
        let waitingCount: Int

        private enum CodingKeys: String, CodingKey {
            case state, pausedByOwner, stopReasons, runningCount, runningSeasons, waitingCount
        }

        init(from decoder: Decoder) throws {
            let container = try decoder.container(keyedBy: CodingKeys.self)
            state = try container.decode(String.self, forKey: .state)
            pausedByOwner = try container.decode(Bool.self, forKey: .pausedByOwner)
            stopReasons = try container.decode([String].self, forKey: .stopReasons)
            runningCount = try container.decode(Int.self, forKey: .runningCount)
            runningSeasons = try container.decodeIfPresent([Season].self, forKey: .runningSeasons) ?? []
            waitingCount = try container.decode(Int.self, forKey: .waitingCount)
        }
    }

    struct Library: Decodable, Sendable {
        let tv: TV
        let movies: Movies
        let other: Other

        struct TV: Decodable, Sendable {
            let showCount: Int
            let seasonCount: Int
            let episodeCount: Int
            let totalBytes: Int64
            let originalBytes: Int64
        }

        struct Movies: Decodable, Sendable {
            let titleCount: Int
            let totalBytes: Int64
            let originalBytes: Int64
        }

        struct Other: Decodable, Sendable {
            let itemCount: Int
            let totalBytes: Int64
            let originalBytes: Int64
        }
    }

    struct SpaceSaved: Decodable, Sendable {
        let bytes: Int64
        let itemCount: Int
        let episodeCount: Int
        let seasonCount: Int
    }

    struct Season: Decodable, Sendable, Hashable {
        let prefix: String
        let showPrefix: String
        let show: String
        let seasonFolder: String?
        let season: Int?
    }

    struct FinishedSeason: Decodable, Sendable, Identifiable {
        let prefix: String
        let showPrefix: String
        let show: String
        let season: Int?
        let episodeCount: Int
        let keptOriginalCount: Int
        let bytesSaved: Int64
        let finishedAt: String?
        var id: String { prefix }
        var keptEntirelyOriginal: Bool { episodeCount == 0 && keptOriginalCount > 0 }
    }

    struct NotStartedShow: Decodable, Sendable, Identifiable {
        let showPrefix: String
        let show: String
        let bytes: Int64
        let episodeCount: Int
        let seasonCount: Int
        let waitingSamplePrefix: String?
        var id: String { showPrefix }
    }

    struct Samples: Decodable, Sendable {
        let waitingOnYou: [Sample]
        let approvedNotStarted: [Sample]
    }

    struct Sample: Decodable, Sendable, Identifiable {
        let prefix: String
        let showPrefix: String
        let show: String
        let season: Int?
        let state: String?
        let action: String?
        let failure: String?
        var id: String { prefix }
    }

    struct Computer: Decodable, Sendable, Identifiable {
        let key: String
        let name: String?
        let platform: String?
        let state: String
        let runningCount: Int
        let scheduleOpensAt: String?
        let attentionReason: String?
        let attentionSince: String?
        var id: String { key }
    }

    struct Event: Decodable, Sendable, Identifiable {
        let id: Int
        let kind: String
        let at: String
        let relPath: String
        let season: Season?
        let bytesSaved: Int64?
    }

    struct OriginalBackups: Decodable, Sendable {
        let count: Int
        let bytes: Int64
    }

    struct KeptOriginal: Decodable, Sendable {
        let keptItemCount: Int
        var kept: [KeptSeason] = []
        var held: [HeldSeason] = []

        enum CodingKeys: CodingKey {
            case keptItemCount, kept, held
        }

        init(from decoder: Decoder) throws {
            let container = try decoder.container(keyedBy: CodingKeys.self)
            keptItemCount = try container.decode(Int.self, forKey: .keptItemCount)
            kept = try container.decodeIfPresent([KeptSeason].self, forKey: .kept) ?? []
            held = try container.decodeIfPresent([HeldSeason].self, forKey: .held) ?? []
        }
    }

    struct KeptSeason: Decodable, Sendable, Identifiable {
        let prefix: String
        let show: String
        let season: Int?
        let reason: String
        let itemCount: Int
        var id: String { "\(prefix):\(reason)" }
    }

    struct HeldSeason: Decodable, Sendable, Identifiable {
        let prefix: String
        let show: String
        let season: Int?
        let reasons: [String]
        var id: String { prefix }
    }
}

extension JSONDecoder {
    static let mediaforce: JSONDecoder = {
        let decoder = JSONDecoder()
        decoder.keyDecodingStrategy = .convertFromSnakeCase
        return decoder
    }()
}
