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
    // Whether work was asked to run or pause and the server hasn't confirmed it yet; nil when nothing is pending.
    private(set) var switching: Bool?
    // Settings › Computers was asked for, from a notification, a page or the menu bar; Settings takes it and clears it.
    var settingsRequest: SettingsRequest?
    // The computer Settings › Computers should show; its list takes it and clears it.
    var requestedComputer: String?
    // Shows spot-checked on Space saved since the app opened.
    var checkedShows: Set<String> = []
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

    /// Pauses or resumes work once at a time, so a second click can't repeat a request still on its way.
    func setRunning(_ on: Bool) async {
        guard switching == nil else { return }
        switching = on
        await perform { on ? try await $0.resume() : try await $0.pause() }
        switching = nil
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

/// A request to open Settings › Computers, on one computer when it names one. Each request is new, so asking for the
/// same computer twice still opens it.
struct SettingsRequest: Equatable {
    let id = UUID()
    let computer: String?
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
    case library, spaceSaved, media
    // Settings › Computers, on one computer when it names one.
    case computer(String?)
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
        case .computer(let key?): "computer:\(key)"
        case .computer(nil): "computer"
        case .library: "library"
        default: "spaceSaved"
        }
    }

    init?(notificationValue value: String) {
        let parts = value.split(separator: ":", maxSplits: 1).map(String.init)
        switch (parts.first, parts.count == 2 ? parts[1] : nil) {
        case ("sample", let prefix?): self = .sample(prefix)
        case ("item", let prefix?): self = .item(prefix)
        case ("library", nil): self = .library
        case ("spaceSaved", nil): self = .spaceSaved
        case ("computer", let key?): self = .computer(key)
        // "computers" is what notifications sent before Computers moved into Settings carry.
        case ("computer", nil), ("computers", nil): self = .computer(nil)
        default: return nil
        }
    }
}

struct MainWindow: View {
    static let id = "main"

    let model: MainWindowModel
    @State private var place: Place? = .library
    @Environment(\.openSettings) private var openSettings

    var body: some View {
        Group {
            if let facts = model.facts {
                NavigationSplitView {
                    Sidebar(place: $place, facts: facts, model: model)
                        .navigationSplitViewColumnWidth(214)
                } detail: {
                    Group {
                        switch place {
                        case .spaceSaved:
                            SpaceSavedPage(facts: facts, model: model)
                                // Deleting backups changes what the page shows, so it reads its facts again at once.
                                .id(facts.originalBackups.count)
                        case .sample(let prefix): SamplePage(prefix: prefix, model: model) { place = .item($0) }
                        case .item(let prefix): ItemPage(prefix: prefix, model: model, openSample: { place = .sample($0) }, openItem: { place = .item($0) })
                        case .media: MediaPage(model: model) { place = .item($0) }
                        default: LibraryPage(facts: facts, posters: model.posters, openComputer: { openComputer($0) }) { place = .sample($0) }
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
        // Initial too: the menu bar may ask for a page while it opens this window.
        .onChange(of: model.requestedPlace, initial: true) { _, requested in
            guard let requested else { return }
            if case .computer(let key) = requested {
                openComputer(key)
            } else {
                place = requested
            }
            model.requestedPlace = nil
        }
        .alert(item: Bindable(model).notice) { notice in
            Alert(title: Text(notice.title), message: Text(notice.message))
        }
    }

    /// Computers live in Settings › Computers; this opens it, on one computer when it names one.
    private func openComputer(_ key: String?) {
        model.settingsRequest = SettingsRequest(computer: key)
        openSettings()
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
            Task { await model.setRunning(paused) }
        } label: {
            Label(paused ? "Resume Mediaforce" : "Pause Mediaforce", systemImage: paused ? "play.fill" : "pause.fill")
        }
        .disabled(model.switching != nil)
        .help(paused ? Words.resumeHelp : Words.pauseHelp(facts.work))
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
                // No count: the page counts shows, titles and folders, while the facts count other media by file.
                Label(Words.media, systemImage: "film.stack")
                    .tag(Place.media)
            }
            .listStyle(.sidebar)
            BackupsRow(backups: facts.originalBackups) { place = .spaceSaved }
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

/// Original backups wait in the Cleanup folder until they are deleted on Space saved, the one place that deletes them.
struct BackupsRow: View {
    let backups: MainWindowFacts.OriginalBackups
    let open: () -> Void

    var body: some View {
        VStack(spacing: 0) {
            Divider()
            Button(action: open) {
                HStack(spacing: 8) {
                    Image(systemName: backups.count > 0 ? "trash.fill" : "trash")
                        .foregroundStyle(.secondary)
                    Text(Words.originalBackups)
                    Spacer()
                    Text(backups.count > 0 ? Words.size(backups.bytes) : Words.none)
                        .foregroundStyle(.secondary)
                        .monospacedDigit()
                }
                .font(.callout)
                .padding(14)
                .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            .help(Words.originalBackupsHelp)
        }
    }
}
