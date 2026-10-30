import SwiftUI

struct LibraryPage: View {
    let model: MainWindowModel
    var kind = MediaKind.everything
    @State private var listing = MediaListing()
    let open: (Place) -> Void

    private var mediaPlace: Place { kind == .everything ? .media : .mediaKind(kind) }
    private var includesTV: Bool { kind == .everything || kind == .tv }

    var body: some View {
        PageFrame(title: kind == .everything ? Words.library : Words.mediaKind(kind),
                  subtitle: model.facts.map(Words.workHeadline) ?? "") {
            if let facts = model.facts {
                WorkStatus(facts: facts, open: open)
                StorageBar(facts: facts, kind: kind, open: open)
                if includesTV {
                    FinishedShelf(seasons: Array(facts.recentlyFinishedSeasons.prefix(6)), posters: model.posters, open: open)
                    if !facts.samples.approvedNotStarted.isEmpty {
                        SampleShelf(facts: facts, posters: model.posters) { open(.sample($0)) }
                    }
                    BiggestTable(shows: Array(facts.biggestNotStarted.prefix(BiggestTable.rowLimit)),
                                 samples: facts.samples.waitingOnYou, posters: model.posters, open: open)
                }
            }
            Button("Browse all", action: { open(mediaPlace) }).buttonStyle(QuietLinkStyle())
            if listing.loading { ProgressView() }
            ForEach(listing.unavailable) { kind in
                Text(Words.libraryReadFailure(kind, hasPrevious: listing.hasLoaded(kind)))
                    .font(.callout).foregroundStyle(.secondary)
            }
            if listing.rows.contains(where: \.listCutShort) {
                Text(Words.otherMediaTruncated).font(.callout).foregroundStyle(.secondary)
            }
            MediaShelves(rows: listing.rows, posters: model.posters, open: { open(.item($0)) },
                         showOnly: { open(.mediaKind(kind, $0)) })
            if let facts = model.facts {
                let events = Array(facts.events.filter { event in
                    kind == .everything || listing.rows.contains { event.relPath == $0.id || event.relPath.hasPrefix($0.id + "/") }
                }.prefix(4))
                if !events.isEmpty { Latest(events: events, posters: model.posters, open: open) }
            }
            if let facts = model.facts {
                Computers(computers: facts.computers) { open(.computer($0)) }
            }
        }
        .task { await listing.watch(model.client, kind: kind) }
    }
}
