import SwiftUI

/// TV shows, movies and VR & 3D together: how work is going, the sizes, what finished and what waits. Each library's
/// own list is under Library in the sidebar.
struct LibraryPage: View {
    let model: MainWindowModel
    @State private var listing = MediaListing()
    let open: (Place) -> Void

    var body: some View {
        let rows = listing.rows
        PageFrame(title: Words.library, subtitle: model.facts.map(Words.workHeadline) ?? "") {
            if let facts = model.facts {
                if Words.workExplanation(facts) != nil || facts.lastChange != nil {
                    WorkStatus(facts: facts, open: open)
                }
                StorageBar(facts: facts, kind: .everything, listing: listing, open: open)
                FinishedShelf(seasons: Array(facts.recentlyFinishedSeasons.prefix(6)), posters: model.posters, open: open)
                if !facts.samples.approvedNotStarted.isEmpty {
                    SampleShelf(facts: facts, posters: model.posters) { open(.sample($0)) }
                }
                BiggestTable(shows: Array(facts.biggestNotStarted.prefix(BiggestTable.rowLimit)),
                             samples: facts.samples.waitingOnYou, posters: model.posters, open: open)
            }
            Button("Browse all", action: { open(.media) }).buttonStyle(QuietLinkStyle())
            if listing.loading { ProgressView() }
            ForEach(listing.unavailable) { kind in
                Text(Words.libraryReadFailure(kind, hasPrevious: listing.hasLoaded(kind)))
                    .font(.callout).foregroundStyle(.secondary)
            }
            if listing.truncated {
                Text(Words.otherMediaTruncated).font(.callout).foregroundStyle(.secondary)
            }
            if rows.isEmpty && listing.hasLoaded && listing.unavailable.isEmpty {
                Text(Words.noMatchingMedia).foregroundStyle(.secondary)
            }
            MediaShelves(rows: rows, posters: model.posters, open: { open(.item($0)) },
                         showOnly: { open(.mediaKind(.everything, $0)) })
            if let events = model.facts.map({ Array($0.events.prefix(4)) }), !events.isEmpty {
                Latest(events: events, posters: model.posters, open: open)
            }
        }
        .task { await listing.watch(model.client, kind: .everything) }
    }
}
