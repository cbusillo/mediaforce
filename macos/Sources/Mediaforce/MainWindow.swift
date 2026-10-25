import SwiftUI

@MainActor
@Observable
final class MainWindowModel {
    static let refreshInterval: Duration = .seconds(30)

    private(set) var facts: MainWindowFacts?
    private(set) var connection: ServerState = .checking
    private(set) var lastHeard: Date?
    var notice: ActionNotice?
    // A page a notification asked to open; the window takes it and clears it.
    var requestedPlace: Place?
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
            lastHeard = .now
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
        } catch {
            notice = .unanswered
        }
        await refresh()
    }

    func deleteCleanupFolder() async {
        do {
            let result = try await client.deleteCleanupFolder()
            notice = result.ok ? .deleted(result) : .publishing
        } catch {
            notice = .unanswered
        }
        await refresh()
    }
}

struct ActionNotice: Identifiable, Equatable {
    let id = UUID()
    let title: String
    let message: String

    static let unanswered = ActionNotice(
        title: "Mediaforce didn't confirm that",
        message: "It didn't answer properly, so the change may not have happened. The window shows what Mediaforce reports next."
    )

    static let publishing = ActionNotice(
        title: "Nothing was deleted",
        message: "Files are being published right now. Try again when they finish."
    )

    static let emptyCleanupFolder = ActionNotice(
        title: "Nothing to delete",
        message: "The Cleanup folder is empty."
    )

    static func deleted(_ result: CleanupResult) -> ActionNotice {
        let kept = (result.preservedCount ?? 0) > 0
            ? " Kept \(Words.count(result.preservedCount ?? 0, "original")) that files still being finished need."
            : ""
        return ActionNotice(
            title: "Deleted \(Words.count(result.removedCount, "original backup"))",
            message: "\(Words.size(result.removedSizeBytes)) freed.\(kept)"
        )
    }

    static func == (lhs: ActionNotice, rhs: ActionNotice) -> Bool { lhs.id == rhs.id }
}

enum Place: Hashable {
    case library, spaceSaved, tv, movies, otherMedia, computers
    case sample(String)
    case item(String)

    static let notificationKey = "opens"

    var opensFromList: Bool {
        switch self {
        case .sample, .item: true
        default: false
        }
    }

    var notificationValue: String {
        switch self {
        case .sample(let prefix): "sample:\(prefix)"
        case .item(let prefix): "item:\(prefix)"
        case .computers: "computers"
        default: "spaceSaved"
        }
    }

    init?(notificationValue value: String) {
        let parts = value.split(separator: ":", maxSplits: 1).map(String.init)
        switch (parts.first, parts.count == 2 ? parts[1] : nil) {
        case ("sample", let prefix?): self = .sample(prefix)
        case ("item", let prefix?): self = .item(prefix)
        case ("spaceSaved", nil): self = .spaceSaved
        case ("computers", nil): self = .computers
        default: return nil
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
                    Group {
                        switch place {
                        case .spaceSaved: SpaceSavedPage(facts: facts)
                        case .computers: ComputersPage(computers: facts.computers)
                        case .sample(let prefix): SamplePage(prefix: prefix, model: model) { place = .item($0) }
                        case .item(let prefix): ItemPage(prefix: prefix, model: model)
                        case .tv: TVShowsPage(model: model, openSample: { place = .sample($0) }, openItem: { place = .item($0) })
                        case .movies: MoviesPage(model: model) { place = .item($0) }
                        case .otherMedia: OtherMediaPage(model: model) { place = .item($0) }
                        default: LibraryPage(facts: facts, posters: model.posters) { place = .sample($0) }
                        }
                    }
                    .safeAreaInset(edge: .top, spacing: 0) {
                        if model.connection != .reachable {
                            StaleNotice(connection: model.connection, lastHeard: model.lastHeard)
                        }
                    }
                }
                .toolbar {
                    if place?.opensFromList == true {
                        ToolbarItem(placement: .navigation) {
                            Button("Library", systemImage: "chevron.backward") { place = .library }
                        }
                    }
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
        .onChange(of: model.requestedPlace) { _, requested in
            guard let requested else { return }
            place = requested
            model.requestedPlace = nil
        }
        .alert(item: Bindable(model).notice) { notice in
            Alert(title: Text(notice.title), message: Text(notice.message))
        }
    }
}

struct StaleNotice: View {
    let connection: ServerState
    let lastHeard: Date?

    var body: some View {
        HStack(spacing: 8) {
            Image(systemName: "exclamationmark.triangle")
            Text(Words.staleNotice(connection, lastHeard: lastHeard))
            Spacer(minLength: 0)
        }
        .font(.callout)
        .padding(.horizontal, 34)
        .padding(.vertical, 8)
        .background(.yellow.opacity(0.18))
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
    @State private var folder: CleanupFolder?
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
            Button("Delete original backups…") {
                Task {
                    do {
                        let found = try await model.client.cleanupFolder()
                        if found.fileCount > 0 {
                            folder = found
                            confirming = true
                        } else {
                            model.notice = .emptyCleanupFolder
                        }
                    } catch {
                        model.notice = .unanswered
                    }
                }
            }
            .controlSize(.small)
            .help("Delete everything in the Cleanup folder. Mediaforce shows what is there and asks first.")
            .padding(.leading, 50)
            .confirmationDialog(
                "Delete everything in the Cleanup folder?",
                isPresented: $confirming,
                presenting: folder
            ) { _ in
                Button("Delete all original backups", role: .destructive) {
                    Task { await model.deleteCleanupFolder() }
                }
            } message: { folder in
                Text("\(Words.count(folder.fileCount, "file")), \(Words.size(folder.totalSizeBytes)). Mediaforce keeps any original a file still being finished needs. The replaced files stay in your library. This cannot be undone.")
            }
        }
        .padding(.bottom, 14)
    }
}
