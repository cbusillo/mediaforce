import SwiftUI

/// Everything in the library in one list: TV shows, movies and other media, with a kind filter, a state filter and
/// search, shown as a table or as poster shelves. Each row opens that show's, movie's or folder's page.
struct MediaPage: View {
    let model: MainWindowModel
    let openItem: (String) -> Void
    @AppStorage("mediaPageView") private var view = MediaView.table
    @State private var search = ""
    @State private var kind = MediaKind.everything
    @State private var stateGroup: LibraryStateGroup?

    var body: some View {
        RefreshingPage(title: Words.media, load: { try await MediaRow.load(model.client) }) { (rows: [MediaRow]) in
            let visible = visibleRows(rows)
            HStack(spacing: 12) {
                Picker(Words.kindLabel, selection: $kind) {
                    ForEach(MediaKind.allCases) { Text(Words.mediaKind($0)).tag($0) }
                }
                .pickerStyle(.segmented)
                .labelsHidden()
                .fixedSize()
                LibraryStatePicker(selection: $stateGroup)
                Spacer()
                TextField(Words.searchMedia, text: $search)
                    .textFieldStyle(.roundedBorder)
                    .frame(width: 220)
                Picker(Words.viewLabel, selection: $view) {
                    Image(systemName: "list.bullet").help(Words.tableView).tag(MediaView.table)
                    Image(systemName: "square.grid.2x2").help(Words.posterView).tag(MediaView.posters)
                }
                .pickerStyle(.segmented)
                .labelsHidden()
                .fixedSize()
            }
            Text(Words.shown(visible.count, of: rows.count, "item"))
                .font(.callout)
                .foregroundStyle(.secondary)
                .monospacedDigit()
            if visible.isEmpty {
                Text(Words.noMatchingMedia).foregroundStyle(.secondary)
            } else if view == .posters {
                MediaShelves(rows: visible, posters: model.posters, open: openItem)
            } else {
                MediaTable(rows: visible, posters: model.posters, open: openItem)
            }
        }
    }

    private func visibleRows(_ rows: [MediaRow]) -> [MediaRow] {
        rows.filter { (kind == .everything || $0.kind == kind)
            && (stateGroup == nil || $0.state == stateGroup)
            && (search.isEmpty || $0.name.localizedStandardContains(search)) }
    }
}

enum MediaView: String {
    case table, posters
}

enum MediaKind: String, CaseIterable, Identifiable {
    case everything, tv, movies, other
    var id: String { rawValue }
}

struct MediaRow: Identifiable, Sendable {
    let id: String
    let name: String
    let kind: MediaKind
    let detail: String
    let bytes: Int64
    let saved: Int64?
    let state: LibraryStateGroup

    var savedBytes: Int64 { (saved ?? 0) > 0 ? saved ?? 0 : 0 }
    var stateRank: Int { state.rawValue }

    @MainActor
    static func load(_ client: MediaforceClient) async throws -> [MediaRow] {
        async let tv = client.tvShows()
        async let movies = client.movies()
        async let other = client.otherMedia()
        let (shows, films, others) = try await (tv, movies, other)
        let rows = shows.shows.map {
            MediaRow(id: $0.showPrefix, name: $0.show, kind: .tv, detail: Words.count($0.seasonCount, "season"),
                     bytes: $0.bytes, saved: $0.estimatedSavedBytes, state: $0.stateGroup)
        } + films.movies.map {
            MediaRow(id: $0.prefix, name: $0.title, kind: .movies, detail: Words.count($0.fileCount, "file"),
                     bytes: $0.bytes, saved: $0.estimatedSavedBytes, state: $0.stateGroup)
        } + others.items.map {
            MediaRow(id: $0.prefix, name: $0.title, kind: .other, detail: Words.count($0.fileCount, "file"),
                     bytes: $0.bytes, saved: $0.estimatedSavedBytes, state: $0.stateGroup)
        }
        // What needs the Director comes first, then the biggest.
        return rows.sorted { ($0.stateRank, -$0.bytes) < ($1.stateRank, -$1.bytes) }
    }
}

struct MediaPoster: View {
    let row: MediaRow
    let posters: PosterStore
    let width: CGFloat

    var body: some View {
        Group {
            if row.kind != .other, let image = posters.image(for: row.id) {
                Image(nsImage: image).resizable().aspectRatio(contentMode: .fill)
                    .frame(width: width, height: width * 1.5)
            } else if width > 60 {
                // No artwork: the title itself, as the main window's shelves show it.
                Text(row.name)
                    .font(.system(size: PosterView.nameSize(row.name, fitting: width - 14), weight: .semibold))
                    .multilineTextAlignment(.center)
                    .lineLimit(4)
                    .padding(7)
                    .frame(width: width, height: width * 1.5)
                    .background(Color.primary.opacity(0.06))
            } else {
                Image(systemName: row.kind == .movies ? "film" : row.kind == .tv ? "tv" : "rectangle.stack")
                    .font(.system(size: width * 0.5))
                    .foregroundStyle(.secondary)
                    .frame(width: width, height: width * 1.5)
            }
        }
        .clipShape(RoundedRectangle(cornerRadius: width > 60 ? 6 : 3, style: .continuous))
    }
}

struct MediaTable: View {
    static let height: CGFloat = 560

    let rows: [MediaRow]
    let posters: PosterStore
    let open: (String) -> Void
    @State private var selected: MediaRow.ID?
    @State private var sortOrder = [KeyPathComparator(\MediaRow.stateRank), KeyPathComparator(\MediaRow.bytes, order: .reverse)]

    var body: some View {
        Table(rows.sorted(using: sortOrder), selection: $selected, sortOrder: $sortOrder) {
            TableColumn(Words.nameColumn, value: \MediaRow.name) { (row: MediaRow) in
                HStack(spacing: 10) {
                    MediaPoster(row: row, posters: posters, width: 22)
                    VStack(alignment: .leading, spacing: 1) {
                        Text(row.name).lineLimit(1)
                        Text("\(Words.mediaKind(row.kind)), \(row.detail)").font(.caption).foregroundStyle(.secondary)
                    }
                }
            }
            TableColumn(Words.sizeColumn, value: \MediaRow.bytes) { (row: MediaRow) in
                Text(Words.size(row.bytes)).monospacedDigit().frame(maxWidth: .infinity, alignment: .trailing)
            }
            .width(70)
            TableColumn(Words.estimatedSavedColumn, value: \MediaRow.savedBytes) { (row: MediaRow) in
                Text(row.savedBytes > 0 ? "About \(Words.size(row.savedBytes))" : Words.notKnownYet)
                    .monospacedDigit()
                    .foregroundStyle(row.savedBytes > 0 ? .primary : .secondary)
                    .frame(maxWidth: .infinity, alignment: .trailing)
            }
            .width(150)
            TableColumn(Words.stateColumn, value: \MediaRow.stateRank) { (row: MediaRow) in
                HStack(spacing: 5) {
                    if row.state == .needsYou { WaitingMark() }
                    Text(Words.libraryState(row.state))
                }
            }
            .width(140)
        }
        .tableStyle(.inset(alternatesRowBackgrounds: true))
        .frame(minHeight: Self.height)
        .contextMenu(forSelectionType: MediaRow.ID.self) { _ in } primaryAction: { ids in
            if let id = ids.first { open(id) }
        }
    }
}

struct MediaShelves: View {
    let rows: [MediaRow]
    let posters: PosterStore
    let open: (String) -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: 22) {
            ForEach(LibraryStateGroup.allCases) { group in
                let members = rows.filter { $0.state == group }
                if !members.isEmpty {
                    VStack(alignment: .leading, spacing: 10) {
                        HStack(spacing: 6) {
                            if group == .needsYou { WaitingMark() }
                            Text(Words.libraryState(group)).font(.headline)
                            Text("\(members.count)").foregroundStyle(.secondary).monospacedDigit()
                        }
                        ScrollView(.horizontal) {
                            LazyHStack(alignment: .top, spacing: 14) {
                                ForEach(members) { row in
                                    Button { open(row.id) } label: {
                                        VStack(alignment: .leading, spacing: 4) {
                                            MediaPoster(row: row, posters: posters, width: 96)
                                            Text(row.name).font(.callout).lineLimit(1)
                                            Text(row.savedBytes > 0 ? "Saves about \(Words.size(row.savedBytes))" : Words.size(row.bytes))
                                                .font(.caption).foregroundStyle(.secondary).monospacedDigit()
                                        }
                                        .frame(width: 96)
                                    }
                                    .buttonStyle(.plain)
                                }
                            }
                        }
                    }
                }
            }
        }
    }
}
