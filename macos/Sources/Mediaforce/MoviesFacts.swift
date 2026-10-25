import Foundation

struct MoviesFacts: Decodable, Sendable {
    let movies: [Movie]

    struct Movie: Decodable, Sendable, Identifiable {
        let prefix: String
        let title: String
        let library: String?
        let fileCount: Int
        let bytes: Int64
        let estimatedSavedBytes: Int64?
        let state: String
        var id: String { prefix }

        var stateGroup: LibraryStateGroup { LibraryStateGroup(movieState: state) }
        var stateRank: Int { stateGroup.rawValue }
        var estimatedSavedSortBytes: Int64 { estimatedSavedBytes ?? -1 }

        func matches(_ search: String) -> Bool {
            search.isEmpty || title.localizedStandardContains(search)
        }
    }
}

extension LibraryStateGroup {
    // Groups the server's movie state codes the way the web library groups movies.
    init(movieState code: String) {
        switch code {
        case "cannot_start", "needs_file_choice", "needs_attention": self = .needsYou
        case "compressing": self = .inProgress
        case "ready": self = .readyForYou
        default: self = .noActiveWork
        }
    }
}
