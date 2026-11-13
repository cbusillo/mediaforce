import Foundation

struct OtherMediaFacts: Decodable, Sendable {
    let items: [Item]
    let truncated: Bool

    var fileCount: Int { items.reduce(0) { $0 + $1.fileCount } }

    struct Item: Decodable, Sendable, Identifiable {
        let prefix: String
        let title: String
        let library: String?
        let fileCount: Int
        let bytes: Int64
        let estimatedSavedBytes: Int64?
        let state: String
        var id: String { prefix }

        var stateGroup: LibraryStateGroup { LibraryStateGroup(otherMediaState: state) }
        var stateRank: Int { stateGroup.rawValue }
        var estimatedSavedSortBytes: Int64 { estimatedSavedBytes ?? -1 }

        func matches(_ search: String) -> Bool {
            search.isEmpty || title.localizedStandardContains(search)
        }
    }
}

extension LibraryStateGroup {
    // Groups the server's VR & 3D state codes into the same groups as TV shows and Movies. Mediaforce can't compress
    // VR and 3D video yet (cbusillo/mediaforce#188), so a view-only item is not waiting work.
    init(otherMediaState code: String) {
        switch code {
        case "cannot_start", "needs_attention": self = .needsYou
        case "compressing": self = .inProgress
        case "ready": self = .readyToCompress
        case "view_only": self = .cannotCompressYet
        default: self = .noActiveWork
        }
    }
}
