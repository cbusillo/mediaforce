import Foundation

struct TVShowsFacts: Decodable, Sendable {
    let shows: [Show]

    struct Show: Decodable, Sendable, Identifiable {
        let showPrefix: String
        let show: String
        let seasonCount: Int
        let episodeCount: Int
        let bytes: Int64
        let estimatedSavedBytes: Int64?
        let seasons: [Season]
        // Files directly in the show's folder, which no season covers; nil when there are none.
        let looseFiles: LooseFiles?
        var id: String { showPrefix }

        var stateGroup: LibraryStateGroup { LibraryStateGroup(seasons) }
        var stateRank: Int { stateGroup.rawValue }
        var estimatedSavedSortBytes: Int64 { estimatedSavedBytes ?? -1 }

        func matches(_ search: String) -> Bool {
            search.isEmpty || show.localizedStandardContains(search)
                || seasons.contains { $0.folder.localizedStandardContains(search) }
        }
    }

    struct LooseFiles: Decodable, Sendable {
        let state: String
        let episodeCount: Int
        let bytes: Int64
    }

    struct Season: Decodable, Sendable, Identifiable {
        let prefix: String
        let season: Int?
        let state: String?
        let action: String?
        let failure: String?
        let tone: String?
        let episodeCount: Int
        let bytes: Int64
        var filesNeedYou = false
        var checksOnly = false
        var id: String { prefix }

        enum CodingKeys: CodingKey {
            case prefix, season, state, action, failure, tone, episodeCount, bytes, filesNeedYou, checksOnly
        }

        init(from decoder: Decoder) throws {
            let container = try decoder.container(keyedBy: CodingKeys.self)
            prefix = try container.decode(String.self, forKey: .prefix)
            season = try container.decodeIfPresent(Int.self, forKey: .season)
            state = try container.decodeIfPresent(String.self, forKey: .state)
            action = try container.decodeIfPresent(String.self, forKey: .action)
            failure = try container.decodeIfPresent(String.self, forKey: .failure)
            tone = try container.decodeIfPresent(String.self, forKey: .tone)
            episodeCount = try container.decode(Int.self, forKey: .episodeCount)
            bytes = try container.decode(Int64.self, forKey: .bytes)
            filesNeedYou = try container.decodeIfPresent(Bool.self, forKey: .filesNeedYou) ?? false
            checksOnly = try container.decodeIfPresent(Bool.self, forKey: .checksOnly) ?? false
        }

        static let sampleActions: Set<String> = ["make_sample", "retry_sample", "choose_size"]

        var folder: String { prefix.split(separator: "/").last.map(String.init) ?? prefix }

        var opensSample: Bool {
            ["ready_to_compare", "needs_test", "ready_to_make"].contains(state) || (state == "needs_help" && action.map(Self.sampleActions.contains) == true)
        }
    }
}

// The web library's four state groups, shared by the TV shows and Movies pages.
enum LibraryStateGroup: Int, CaseIterable, Identifiable {
    case needsYou, inProgress, readyToCompress, noActiveWork

    var id: Int { rawValue }
}

// The same grouping as the web library's TV list, so a show reads the same in both.
extension LibraryStateGroup {
    static let needsYouTones: Set<String> = ["attention", "fail", "wait"]

    init(_ seasons: [TVShowsFacts.Season]) {
        let tones = Set(seasons.compactMap(\.tone))
        let states = Set(seasons.compactMap(\.state))
        if !tones.isDisjoint(with: Self.needsYouTones) {
            self = .needsYou
        } else if tones.contains("active") || states.contains("sample_waiting") {
            self = .inProgress
        } else if tones.contains("ready") || states.contains("needs_test") {
            self = .readyToCompress
        } else {
            self = .noActiveWork
        }
    }
}
