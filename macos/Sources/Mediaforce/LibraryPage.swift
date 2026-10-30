import SwiftUI

struct LibraryPage: View {
    let model: MainWindowModel
    var kind = MediaKind.everything
    let open: (Place) -> Void

    private var mediaPlace: Place { kind == .everything ? .media : .mediaKind(kind) }

    var body: some View {
        RefreshingPage(title: kind == .everything ? Words.library : Words.mediaKind(kind),
                       subtitle: model.facts.map(Words.workHeadline) ?? "",
                       load: { try await MediaRow.load(model.client, kind: kind) }) { (loaded: [MediaRow]) in
            let rows = kind.rows(in: loaded)
            if let facts = model.facts {
                if let explanation = Words.workExplanation(facts) {
                    Text(explanation).foregroundStyle(.secondary)
                }
                StorageBar(facts: facts, kind: kind, open: open)
            }
            HStack {
                Text(Words.count(rows.count, "item")).foregroundStyle(.secondary)
                Spacer()
                Button("Browse all", action: { open(mediaPlace) }).buttonStyle(QuietLinkStyle())
            }
            if rows.contains(where: \.listCutShort) {
                Text(Words.otherMediaTruncated).font(.callout).foregroundStyle(.secondary)
            }
            if rows.isEmpty {
                Text(Words.noMatchingMedia).foregroundStyle(.secondary)
            } else {
                MediaShelves(rows: rows, posters: model.posters, open: { open(.item($0)) },
                             showOnly: { open(.mediaKind(kind, $0)) })
                VStack(alignment: .leading, spacing: 10) {
                    Text("\(min(rows.count, 9)) biggest items").font(.headline)
                    MediaTable(rows: Array(rows.sorted { $0.bytes > $1.bytes }.prefix(9)),
                               posters: model.posters, open: { open(.item($0)) },
                               sortOrder: [KeyPathComparator(\MediaRow.bytes, order: .reverse)])
                        .scrollDisabled(true)
                        .frame(height: CGFloat(min(rows.count, 9)) * 46 + 32)
                }
            }
            if let facts = model.facts {
                Computers(computers: facts.computers) { open(.computer($0)) }
            }
        }
    }
}
