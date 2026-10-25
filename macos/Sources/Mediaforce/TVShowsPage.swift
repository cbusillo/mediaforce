import SwiftUI

struct TVShowsPage: View {
    let model: MainWindowModel
    let openSample: (String) -> Void
    @State private var facts: TVShowsFacts?
    @State private var unreadable = false
    @State private var search = ""
    @State private var stateGroup: TVShowsFacts.StateGroup?
    @State private var selected: TVShowsFacts.Show.ID?
    @State private var sortOrder = [KeyPathComparator(\TVShowsFacts.Show.bytes, order: .reverse)]
    @Environment(\.openURL) private var openURL

    var body: some View {
        PageFrame(title: Words.tvShows) {
            if let facts {
                let shows = visibleShows(facts)
                HStack(spacing: 12) {
                    TextField(Words.searchShows, text: $search)
                        .textFieldStyle(.roundedBorder)
                        .frame(width: 260)
                    Picker(Words.stateColumn, selection: $stateGroup) {
                        Text(Words.allStates).tag(TVShowsFacts.StateGroup?.none)
                        ForEach(TVShowsFacts.StateGroup.allCases) { choice in
                            Text(Words.tvShowState(choice)).tag(Optional(choice))
                        }
                    }
                    .fixedSize()
                    Spacer()
                    Text(Words.showsShown(shows.count, of: facts.shows.count))
                        .foregroundStyle(.secondary)
                        .monospacedDigit()
                }
                TVShowsTable(shows: shows, selected: $selected, sortOrder: $sortOrder)
                if shows.isEmpty {
                    Text(Words.noMatchingShows)
                        .foregroundStyle(.secondary)
                } else if let show = shows.first(where: { $0.id == selected }) {
                    TVSeasonList(show: show, openSample: openSample) { openURL(model.client.seasonPage($0)) }
                } else {
                    Text(Words.chooseShow)
                        .foregroundStyle(.secondary)
                }
            } else if unreadable {
                Text(Words.staleNotice(.failing, lastHeard: nil))
                    .foregroundStyle(.secondary)
            } else {
                ProgressView()
            }
        }
        .task { await watch() }
    }

    private func visibleShows(_ facts: TVShowsFacts) -> [TVShowsFacts.Show] {
        facts.shows
            .filter { $0.matches(search) && (stateGroup == nil || $0.stateGroup == stateGroup) }
            .sorted(using: sortOrder)
    }

    private func watch() async {
        while !Task.isCancelled {
            do {
                facts = try await model.client.tvShows()
                unreadable = false
            } catch {
                unreadable = facts == nil
            }
            try? await Task.sleep(for: MainWindowModel.refreshInterval)
        }
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
                Text(Words.tvShowState(show.stateGroup))
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
    let openInBrowser: (String) -> Void

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
                        Text(Words.seasonState(season.state, season.action))
                            .help(Words.sampleFailure(season.failure) ?? "")
                        Button(Words.seasonButton(season)) {
                            if season.opensSample {
                                openSample(season.prefix)
                            } else {
                                openInBrowser(season.prefix)
                            }
                        }
                        .controlSize(.small)
                    }
                }
            }
        }
    }
}
