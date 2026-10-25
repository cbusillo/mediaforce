import SwiftUI

struct TVShowsPage: View {
    let model: MainWindowModel
    let openSample: (String) -> Void
    let openItem: (String) -> Void
    @State private var search = ""
    @State private var stateGroup: LibraryStateGroup?
    @State private var selected: TVShowsFacts.Show.ID?
    @State private var sortOrder = [KeyPathComparator(\TVShowsFacts.Show.bytes, order: .reverse)]

    var body: some View {
        RefreshingPage(title: Words.tvShows, load: { try await model.client.tvShows() }) { (facts: TVShowsFacts) in
            let shows = visibleShows(facts)
            HStack(spacing: 12) {
                TextField(Words.searchShows, text: $search)
                    .textFieldStyle(.roundedBorder)
                    .frame(width: 260)
                LibraryStatePicker(selection: $stateGroup)
                Spacer()
                Text(Words.shown(shows.count, of: facts.shows.count, "show"))
                    .foregroundStyle(.secondary)
                    .monospacedDigit()
            }
            TVShowsTable(shows: shows, selected: $selected, sortOrder: $sortOrder)
            if shows.isEmpty {
                Text(Words.noMatchingShows)
                    .foregroundStyle(.secondary)
            } else if let show = shows.first(where: { $0.id == selected }) {
                TVSeasonList(show: show, openSample: openSample, openItem: openItem)
            } else {
                Text(Words.chooseShow)
                    .foregroundStyle(.secondary)
            }
        }
    }

    private func visibleShows(_ facts: TVShowsFacts) -> [TVShowsFacts.Show] {
        facts.shows
            .filter { $0.matches(search) && (stateGroup == nil || $0.stateGroup == stateGroup) }
            .sorted(using: sortOrder)
    }
}

struct TVShowsTable: View {
    static let height: CGFloat = 400

    let shows: [TVShowsFacts.Show]
    @Binding var selected: TVShowsFacts.Show.ID?
    @Binding var sortOrder: [KeyPathComparator<TVShowsFacts.Show>]

    var body: some View {
        Table(shows, selection: $selected, sortOrder: $sortOrder) {
            TableColumn(Words.showColumn, value: \TVShowsFacts.Show.show) { (show: TVShowsFacts.Show) in
                Text(show.show)
            }
            TableColumn(Words.seasonsColumn, value: \TVShowsFacts.Show.seasonCount) { (show: TVShowsFacts.Show) in
                Text("\(show.seasonCount)")
                    .monospacedDigit()
                    .frame(maxWidth: .infinity, alignment: .trailing)
            }
            .width(60)
            TableColumn(Words.sizeColumn, value: \TVShowsFacts.Show.bytes) { (show: TVShowsFacts.Show) in
                Text(Words.size(show.bytes))
                    .monospacedDigit()
                    .frame(maxWidth: .infinity, alignment: .trailing)
            }
            .width(80)
            TableColumn(Words.estimatedSavedColumn, value: \TVShowsFacts.Show.estimatedSavedSortBytes) { (show: TVShowsFacts.Show) in
                Text(show.estimatedSavedBytes.map(Words.size) ?? Words.notKnownYet)
                    .monospacedDigit()
                    .foregroundStyle(show.estimatedSavedBytes == nil ? HierarchicalShapeStyle.secondary : HierarchicalShapeStyle.primary)
                    .frame(maxWidth: .infinity, alignment: .trailing)
            }
            .width(150)
            TableColumn(Words.stateColumn, value: \TVShowsFacts.Show.stateRank) { (show: TVShowsFacts.Show) in
                Text(Words.libraryState(show.stateGroup))
            }
            .width(120)
        }
        .tableStyle(.inset(alternatesRowBackgrounds: true))
        .frame(height: Self.height)
    }
}

struct TVSeasonList: View {
    let show: TVShowsFacts.Show
    let openSample: (String) -> Void
    let openItem: (String) -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            Text(show.show)
                .font(.headline)
            Grid(alignment: .leadingFirstTextBaseline, horizontalSpacing: 18, verticalSpacing: 8) {
                ForEach(show.seasons) { season in
                    GridRow {
                        Text(Words.seasonName(season))
                        Text(Words.count(season.episodeCount, "episode"))
                            .foregroundStyle(.secondary)
                            .monospacedDigit()
                        Text(Words.size(season.bytes))
                            .monospacedDigit()
                            .gridColumnAlignment(.trailing)
                        Text(Words.tvSeasonState(season))
                            .help(Words.sampleFailure(season.failure) ?? "")
                        Button(Words.seasonButton(season)) {
                            if season.opensSample {
                                openSample(season.prefix)
                            } else {
                                openItem(season.prefix)
                            }
                        }
                        .controlSize(.small)
                    }
                }
            }
            if let looseFiles = show.looseFiles {
                Text(Words.looseFiles(looseFiles))
                    .foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
    }
}
