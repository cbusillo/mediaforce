import SwiftUI

@MainActor
@Observable
final class MainWindowModel {
    static let refreshInterval: Duration = .seconds(30)

    private(set) var facts: MainWindowFacts?
    private(set) var connection: ServerState = .checking
    private(set) var actionFailed = false
    var onFacts: ((MainWindowFacts) -> Void)?
    let client: MediaforceClient
    let posters: PosterStore

    init(client: MediaforceClient) {
        self.client = client
        posters = PosterStore(client: client)
    }

    func refresh() async {
        do {
            let facts = try await client.facts()
            self.facts = facts
            connection = .reachable
            onFacts?(facts)
        } catch is DecodingError {
            connection = .failing
        } catch let error as URLError where error.code == .badServerResponse {
            connection = .failing
        } catch {
            connection = .unreachable
        }
    }

    func startWatching() {
        Task {
            while !Task.isCancelled {
                await refresh()
                try? await Task.sleep(for: Self.refreshInterval)
            }
        }
    }

    func perform(_ action: @Sendable (MediaforceClient) async throws -> Void) async {
        do {
            try await action(client)
            actionFailed = false
        } catch {
            actionFailed = true
        }
        await refresh()
    }
}

enum Place: Hashable {
    case library, spaceSaved, keptOriginal, tv, movies, otherMedia, computers, settings

    static func webPath(_ place: Place) -> String? {
        switch place {
        case .tv: ""
        case .movies: "movies"
        case .otherMedia: "other"
        case .settings: "settings"
        default: nil
        }
    }
}

struct MainWindow: View {
    let model: MainWindowModel
    @State private var place: Place? = .library

    var body: some View {
        Group {
            if let facts = model.facts {
                NavigationSplitView {
                    Sidebar(place: $place, facts: facts, model: model)
                        .navigationSplitViewColumnWidth(214)
                } detail: {
                    switch place {
                    case .spaceSaved: SpaceSavedPage(facts: facts)
                    case .keptOriginal: KeptOriginalPage(kept: facts.keptOriginal)
                    case .computers: ComputersPage(computers: facts.computers)
                    default: LibraryPage(facts: facts, posters: model.posters)
                    }
                }
                .onChange(of: place) { previous, chosen in
                    guard let path = chosen.flatMap(Place.webPath) else { return }
                    NSWorkspace.shared.open(model.client.baseURL.appending(path: path))
                    place = previous
                }
                .toolbar {
                    ToolbarItem(placement: .primaryAction) {
                        PauseButton(facts: facts, model: model)
                    }
                }
                .toolbar(removing: .title)
            } else {
                ServerStatusView(state: model.connection) {
                    Task { await model.refresh() }
                }
            }
        }
        .frame(minWidth: 1100, minHeight: 700)
    }
}

struct PauseButton: View {
    let facts: MainWindowFacts
    let model: MainWindowModel

    var body: some View {
        let paused = facts.work.pausedByOwner
        Button {
            Task { await model.perform { paused ? try await $0.resume() : try await $0.pause() } }
        } label: {
            Label(paused ? "Resume Mediaforce" : "Pause Mediaforce", systemImage: paused ? "play.fill" : "pause.fill")
        }
        .help(paused ? "Resume work on every computer." : Words.pauseHelp(facts.work))
    }
}

struct Sidebar: View {
    @Binding var place: Place?
    let facts: MainWindowFacts
    let model: MainWindowModel

    var body: some View {
        VStack(spacing: 0) {
            List(selection: $place) {
                Label("Library", systemImage: "books.vertical")
                    .tag(Place.library)
                SidebarRow(title: "Space saved", symbol: "arrow.down.right.and.arrow.up.left", trailing: Words.size(facts.spaceSaved.bytes))
                    .tag(Place.spaceSaved)
                SidebarRow(title: "Kept original", symbol: "hand.raised", trailing: "\(facts.keptOriginal.keptItemCount)")
                    .tag(Place.keptOriginal)
                Section("Media") {
                    SidebarRow(title: "TV shows", symbol: "tv", trailing: "\(facts.library.tv.showCount)")
                        .tag(Place.tv)
                    SidebarRow(title: "Movies", symbol: "film", trailing: "\(facts.library.movies.titleCount)")
                        .tag(Place.movies)
                    SidebarRow(title: "Other media", symbol: "rectangle.stack", trailing: "\(facts.library.other.itemCount)")
                        .tag(Place.otherMedia)
                }
                Section {
                    Label("Computers", systemImage: "desktopcomputer")
                        .tag(Place.computers)
                    Label("Settings", systemImage: "gearshape")
                        .tag(Place.settings)
                }
            }
            .listStyle(.sidebar)
            BackupBin(backups: facts.originalBackups, model: model)
        }
    }
}

struct SidebarRow: View {
    let title: String
    let symbol: String
    let trailing: String

    var body: some View {
        HStack {
            Label(title, systemImage: symbol)
            Spacer()
            Text(trailing)
                .foregroundStyle(.secondary)
                .monospacedDigit()
        }
    }
}

struct BackupBin: View {
    let backups: MainWindowFacts.OriginalBackups
    let model: MainWindowModel
    @State private var confirming = false

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            Divider()
            HStack(alignment: .top, spacing: 10) {
                Image(systemName: backups.count > 0 ? "trash.fill" : "trash")
                    .font(.system(size: 20, weight: .light))
                    .foregroundStyle(.secondary)
                    .frame(width: 24)
                VStack(alignment: .leading, spacing: 3) {
                    Text("Original backups")
                        .font(.headline)
                    Text(backups.count > 0 ? "\(backups.count) ready to delete, \(Words.size(backups.bytes))" : "Nothing to delete")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }
            }
            .help("When a season is replaced, its originals wait here until you delete them.")
            .padding(.horizontal, 16)
            Button("Delete original backups…") { confirming = true }
                .controlSize(.small)
                .disabled(backups.count == 0)
                .help(backups.count == 0 ? "Nothing to delete yet. Backups land here after a season is replaced." : "Delete every original backup in the Cleanup folder.")
                .padding(.leading, 50)
                .confirmationDialog(
                    "Delete all \(backups.count) original backups?",
                    isPresented: $confirming
                ) {
                    Button("Delete all original backups", role: .destructive) {
                        Task { await model.perform { try await $0.deleteAllOriginalBackups() } }
                    }
                } message: {
                    Text("This frees \(Words.size(backups.bytes)). The replaced files stay in your library. This cannot be undone.")
                }
        }
        .padding(.bottom, 14)
    }
}
