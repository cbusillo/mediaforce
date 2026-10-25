import SwiftUI

struct PageFrame<Content: View>: View {
    let title: String
    @ViewBuilder let content: Content

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 18) {
                Text(title)
                    .font(.system(size: 30, weight: .bold))
                content
            }
            .padding(.horizontal, 34)
            .padding(.top, 6)
            .padding(.bottom, 24)
            .frame(maxWidth: .infinity, alignment: .leading)
        }
        .scrollBounceBehavior(.basedOnSize)
        .background(Color(nsColor: .textBackgroundColor))
    }
}

// A page that reads its own facts while it is open, refreshing on the main window's interval and keeping what it last heard.
struct RefreshingPage<Facts: Sendable, Content: View>: View {
    let title: String
    let load: @MainActor () async throws -> Facts
    @ViewBuilder let content: (Facts) -> Content
    @State private var facts: Facts?
    @State private var refreshFailed = false
    @State private var lastHeard: Date?

    var body: some View {
        PageFrame(title: title) {
            if let facts {
                if refreshFailed {
                    StaleNotice(connection: .failing, lastHeard: lastHeard)
                        .padding(.horizontal, -34)
                }
                content(facts)
            } else if refreshFailed {
                Text(Words.staleNotice(.failing, lastHeard: nil))
                    .foregroundStyle(.secondary)
            } else {
                ProgressView()
            }
        }
        .task { await watch() }
    }

    private func watch() async {
        while !Task.isCancelled {
            do {
                facts = try await load()
                refreshFailed = false
                lastHeard = .now
            } catch {
                refreshFailed = true
            }
            try? await Task.sleep(for: MainWindowModel.refreshInterval)
        }
    }
}

struct LibraryStatePicker: View {
    @Binding var selection: LibraryStateGroup?

    var body: some View {
        Picker(Words.stateColumn, selection: $selection) {
            Text(Words.allStates).tag(LibraryStateGroup?.none)
            ForEach(LibraryStateGroup.allCases) { choice in
                Text(Words.libraryState(choice)).tag(Optional(choice))
            }
        }
        .fixedSize()
    }
}

struct SpaceSavedPage: View {
    let facts: MainWindowFacts

    var body: some View {
        PageFrame(title: "Space saved") {
            Text("\(Words.size(facts.spaceSaved.bytes)) saved from \(Words.count(facts.spaceSaved.episodeCount, "episode")) in \(Words.count(facts.spaceSaved.seasonCount, "finished season")).")
                .foregroundStyle(.secondary)
            Text("Finished lately")
                .font(.headline)
            Table(facts.recentlyFinishedSeasons) {
                TableColumn("Season") { (season: MainWindowFacts.FinishedSeason) in
                    Text(Words.season(season.show, season.season))
                }
                TableColumn("Episodes") { (season: MainWindowFacts.FinishedSeason) in
                    Text("\(season.episodeCount + season.keptOriginalCount)").monospacedDigit()
                }
                .width(70)
                TableColumn("Saved") { (season: MainWindowFacts.FinishedSeason) in
                    Text(season.keptEntirelyOriginal ? Words.keptOriginal : Words.size(season.bytesSaved)).monospacedDigit()
                }
                .width(80)
                TableColumn("Finished") { (season: MainWindowFacts.FinishedSeason) in
                    Text(Words.day(season.finishedAt)).foregroundStyle(.secondary)
                }
                .width(80)
            }
            .tableStyle(.inset(alternatesRowBackgrounds: true))
            .frame(minHeight: 360)
        }
    }
}

struct ComputersPage: View {
    let computers: [MainWindowFacts.Computer]

    var body: some View {
        PageFrame(title: "Computers") {
            Computers(computers: computers)
        }
    }
}
