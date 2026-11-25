import SwiftUI

struct SavingsSlice: Identifiable {
    let id: String
    let name: String
    let current: Int64
    let saved: Int64
    let complete: Bool
    var before: Int64 { current + saved }
}

struct SavingsGroup: Identifiable {
    let id: String
    let name: String
    let slices: [SavingsSlice]
    var unit = "season"
    var groupedCompressedBytes: Int64?
    var isShow: Bool { unit == "season" }
    var current: Int64 { slices.reduce(0) { $0 + $1.current } }
    var before: Int64 { slices.reduce(0) { $0 + $1.before } }
    var saved: Int64 { before - current }
    var finished: Int { slices.count(where: \.complete) }
    var summary: String {
        "\(finished) of \(Words.count(slices.count, unit)) compressed · " +
            (saved > 0 ? "\(Words.size(saved)) back" : "nothing back yet")
    }
}

struct SavingsLibrary {
    let shows: [SavingsGroup]
    let groups: [SavingsGroup]

    init(folders: [String: SavingsHistory.Folder], sizes: [String: SavingsHistory.Published]) {
        let slices = Dictionary(grouping: folders, by: { $0.value.show }).mapValues { entries in
            entries.map { prefix, folder in
                SavingsSlice(id: prefix, name: folder.name, current: folder.bytes, saved: sizes[prefix]?.saved ?? 0, complete: folder.finished)
            }
        }
        let shows = slices.compactMap { prefix, seasons -> SavingsGroup? in
            guard let prefix else { return nil }
            let sorted = seasons.sorted {
                if $0.id == prefix || $1.id == prefix { return $1.id == prefix && $0.id != prefix }
                return $0.id.localizedStandardCompare($1.id) == .orderedAscending
            }
            return SavingsGroup(id: prefix, name: Words.lastFolder(prefix), slices: sorted)
        }
        let movies = (slices[nil] ?? []).sorted { $0.current == $1.current ? $0.id < $1.id : $0.current > $1.current }
        self.init(shows: shows, movies: movies, sizes: sizes)
    }

    init(rows: [MediaRow], sizes: [String: SavingsHistory.Published]) {
        var shows: [SavingsGroup] = []
        var movies: [SavingsSlice] = []
        for row in rows {
            switch row.kind {
            case .movies:
                movies.append(SavingsSlice(id: row.id, name: row.name, current: row.bytes, saved: sizes[row.id]?.saved ?? 0, complete: row.isFinished))
            case .tv:
                var slices = row.seasons.map {
                    SavingsSlice(id: $0.prefix, name: $0.folder, current: $0.bytes,
                                 saved: sizes[$0.prefix]?.saved ?? 0, complete: sizes[$0.prefix]?.finishedSeason == true)
                }
                if let loose = row.looseFiles {
                    slices.append(SavingsSlice(id: row.id, name: "Files outside seasons", current: loose.bytes,
                                              saved: sizes[row.id]?.saved ?? 0, complete: false))
                }
                shows.append(SavingsGroup(id: row.id, name: row.name, slices: slices))
            default: continue
            }
        }
        self.init(shows: shows, movies: movies, sizes: sizes)
    }

    private init(shows: [SavingsGroup], movies: [SavingsSlice], sizes: [String: SavingsHistory.Published]) {
        let shows = shows.filter { $0.before > 0 }.sorted { $0.before == $1.before ? $0.id < $1.id : $0.before > $1.before }
        self.shows = shows
        var groups = Array(shows.prefix(36))
        let rest = shows.dropFirst(groups.count)
        if !rest.isEmpty {
            let slices = rest.map {
                SavingsSlice(id: $0.id, name: $0.name, current: $0.current, saved: $0.saved,
                             complete: $0.finished == $0.slices.count)
            }
            let compressed = rest.reduce(Int64.zero) { total, show in
                total + show.slices.reduce(0) { $0 + (sizes[$1.id]?.compressedBefore ?? $1.saved) }
            }
            groups.append(SavingsGroup(id: "more", name: "\(rest.count) more shows", slices: slices, unit: "show",
                                       groupedCompressedBytes: compressed))
        }
        if !movies.isEmpty {
            groups.append(SavingsGroup(id: "movies", name: "Movies", slices: movies, unit: "movie"))
        }
        self.groups = groups
    }
}
