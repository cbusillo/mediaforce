import SwiftUI

struct LibraryPage: View {
    let model: MainWindowModel
    var kind = MediaKind.everything
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
            RefreshingPage(title: kind == .everything ? Words.library : Words.mediaKind(kind), scrolls: false,
                           load: { await MediaListing.load(model.client, kind: kind) }) { (listing: MediaListing) in
                let rows = listing.rows
                ForEach(listing.unavailable) { kind in
                    Text("\(Words.mediaKind(kind)) didn't load. Retrying.").font(.callout).foregroundStyle(.secondary)
                }
                if rows.contains(where: \.listCutShort) {
                    Text(Words.otherMediaTruncated).font(.callout).foregroundStyle(.secondary)
                }
                MediaShelves(rows: rows.filter { !includesTV || $0.kind != .tv }, posters: model.posters, open: { open(.item($0)) },
                             showOnly: { open(.mediaKind(kind, $0)) })
                if let facts = model.facts {
                    Latest(events: Array(facts.events.filter { event in
                        kind == .everything || rows.contains { event.relPath == $0.id || event.relPath.hasPrefix($0.id + "/") }
                    }.prefix(4)), posters: model.posters, open: open)
                }
            }
            .padding(.horizontal, -34)
            if let facts = model.facts {
                Computers(computers: facts.computers) { open(.computer($0)) }
            }
        }
    }
}
