import SwiftUI

@MainActor
@Observable
final class MainWindowModel {
    static let refreshInterval: Duration = .seconds(30)
    // Working out the kinds reads the whole TV library, so the sidebar's sizes refresh less often; a kind's page reads
    // them again when it opens.
    static let kindsInterval: TimeInterval = 120

    private(set) var facts: MainWindowFacts?
    // Each kind of content and how it is decided; nil until Mediaforce first answers.
    private(set) var kinds: KindsFacts?
    private var kindsAttempted: Date?
    private var kindsRequest = 0
    // The player the full-screen window shows, while it is open.
    var theater: Theater?
    private(set) var connection: ServerState = .checking
    private(set) var lastHeard: Date?
    // What went wrong at the last failed check, for the window's Details; nil once Mediaforce answers again.
    private(set) var problem: ServerProblem?
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
            problem = nil
            onFacts?(facts)
            if kindsAttempted.map({ Date.now.timeIntervalSince($0) >= Self.kindsInterval }) ?? true { await refreshKinds() }
        } catch is DecodingError {
            connection = .failing
            problem = ServerProblem(kind: .unreadableReply, at: .now)
        } catch let error as URLError where error.code == .badServerResponse {
            connection = .failing
            problem = ServerProblem(kind: .errorReply, at: .now)
        } catch {
            connection = .unreachable
            problem = ServerProblem(kind: .noAnswer, at: .now)
        }
    }

    /// Reads the kinds again; a failed read keeps what was last heard, since the window's facts say if Mediaforce is down.
    func refreshKinds() async {
        kindsRequest += 1
        let request = kindsRequest
        kindsAttempted = .now
        guard let kinds = try? await client.kinds(), request == kindsRequest else { return }
        self.kinds = kinds
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
    case library, needsYou, spaceSaved, media
    // Settings › Computers, on one computer when it names one.
    case computer(String?)
    case sample(String)
    case item(String)
    // A kind of content under "How it should look", and the seasons that fit no kind.
    case kind(String)
    case seasonsWithoutKind

    static let notificationKey = "opens"

    var notificationValue: String {
        switch self {
        case .sample(let prefix): "sample:\(prefix)"
        case .item(let prefix): "item:\(prefix)"
        case .computer(let key?): "computer:\(key)"
        case .computer(nil): "computer"
        case .library: "library"
        case .needsYou: "needsYou"
        default: "spaceSaved"
        }
    }

    init?(notificationValue value: String) {
        let parts = value.split(separator: ":", maxSplits: 1).map(String.init)
        switch (parts.first, parts.count == 2 ? parts[1] : nil) {
        case ("sample", let prefix?): self = .sample(prefix)
        case ("item", let prefix?): self = .item(prefix)
        case ("library", nil): self = .library
        case ("needsYou", nil): self = .needsYou
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
    @State private var navigation = PageNavigation()
    @Environment(\.openSettings) private var openSettings

    var body: some View {
        Group {
            if let facts = model.facts {
                NavigationSplitView {
                    Sidebar(place: Binding(get: { navigation.place }, set: { navigation.select($0) }), facts: facts, model: model)
                        .navigationSplitViewColumnWidth(214)
                } detail: {
                    Group {
                        switch navigation.place {
                        case .needsYou:
                            NeedsYouPage(facts: facts, open: go)
                        case .spaceSaved:
                            SpaceSavedPage(facts: facts, model: model) { go(.item($0)) }
                                // Deleting backups changes what the page shows, so it reads its facts again at once.
                                .id(facts.originalBackups.count)
                        // A new page for each prefix, so none shows the last one's name or facts while its own loads.
                        case .sample(let prefix):
                            SamplePage(prefix: prefix, model: model, openItem: { go(.item($0)) },
                                       namePage: { navigation.name(.sample(prefix), $0) }).id(prefix)
                        case .item(let prefix):
                            ItemPage(prefix: prefix, model: model, openSample: { go(.sample($0)) }, openItem: { go(.item($0)) },
                                     namePage: { navigation.name(.item(prefix), $0) })
                                .id(prefix)
                        case .media: MediaPage(model: model) { go(.item($0)) }
                        case .seasonsWithoutKind:
                            MediaPage(model: model, onlySeasons: model.kinds?.leftOut, showEverything: { go(.media) }) { go(.item($0)) }
                        case .kind(let code):
                            if let kind = model.kinds?.kinds.first(where: { $0.kind == code }) {
                                KindPage(kind: kind, leftOut: model.kinds?.leftOut.count ?? 0, model: model,
                                         openItem: { go(.item($0)) }, openSample: { go(.sample($0)) },
                                         openLeftOut: { go(.seasonsWithoutKind) })
                            }
                        default: LibraryPage(facts: facts, posters: model.posters) { go($0) }
                        }
                    }
                    .safeAreaInset(edge: .top, spacing: 0) {
                        VStack(spacing: 0) {
                            ForEach(TopNotice.shown(connection: model.connection, facts: facts), id: \.self) { notice in
                                switch notice {
                                case .stale: StaleNotice(connection: model.connection, lastHeard: model.lastHeard)
                                case .paused: PausedBar(model: model)
                                }
                            }
                        }
                    }
                }
                .toolbar {
                    // First beside the sidebar, so Pause never moves; a page opened from a list adds its back button after it.
                    ToolbarItem(placement: .navigation) {
                        PauseButton(facts: facts, model: model)
                    }
                    if let title = navigation.backTitle {
                        ToolbarItem(placement: .navigation) {
                            Button(title, systemImage: "chevron.backward") { navigation.goBack() }
                                .labelStyle(.titleAndIcon)
                        }
                    }
                }
            } else {
                ServerStatusView(state: model.connection, lastHeard: model.lastHeard, problem: model.problem,
                                 headlineInTitleBar: true) {
                    Task { await model.refresh() }
                }
                .navigationTitle(Words.serverHeadline(ServerSituation(model.connection, serviceInstalled: BackgroundService.isInstalled)))
            }
        }
        .frame(minWidth: 1100, minHeight: 700)
        // Initial too: the menu bar may ask for a page while it opens this window.
        .onChange(of: model.requestedPlace, initial: true) { _, requested in
            guard let requested else { return }
            go(requested)
            model.requestedPlace = nil
        }
        .alert(item: Bindable(model).notice) { notice in
            Alert(title: Text(notice.title), message: Text(notice.message))
        }
    }

    /// Opens a place from a link on a page: a page of this window, or Settings › Computers for a computer.
    private func go(_ requested: Place) {
        if case .computer(let key) = requested { openComputer(key) } else { navigation.open(requested) }
    }

    /// Computers live in Settings › Computers; this opens it, on one computer when it names one.
    private func openComputer(_ key: String?) {
        model.settingsRequest = SettingsRequest(computer: key)
        openSettings()
    }
}

/// The bars across the top of every page. A lost connection doesn't hide that Mediaforce was paused: the last facts
/// heard still say so, so both show.
enum TopNotice: Hashable {
    case stale, paused

    static func shown(connection: ServerState, facts: MainWindowFacts) -> [TopNotice] {
        let canResume = facts.work.pausedByOwner || facts.work.checksPaused
        return (connection == .reachable ? [] : [.stale]) + (canResume ? [.paused] : [])
    }
}

/// On every page while the Director has paused Mediaforce, so a pause is never only a changed icon.
struct PausedBar: View {
    let model: MainWindowModel

    var body: some View {
        HStack(spacing: 8) {
            Image(systemName: "pause.circle.fill")
                .foregroundStyle(Color.accentColor)
            Text(model.facts?.work.checksPaused == true && model.facts?.work.pausedByOwner == false
                 ? Words.checksPausedBar : Words.pausedBar)
            Spacer(minLength: 0)
            Button(Words.resume) { Task { await model.setRunning(true) } }
                .controlSize(.small)
                // Resuming needs Mediaforce to answer.
                .disabled(model.switching != nil || model.connection != .reachable)
        }
        .font(.callout)
        .padding(.horizontal, 34)
        .padding(.vertical, 8)
        .background(Color.accentColor.opacity(0.12))
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
            Label(paused ? Words.resume : Words.pause, systemImage: paused ? "play.fill" : "pause.fill")
        }
        // A toolbar shows only the icon by default; the word says what pressing it does, and that it took effect.
        .labelStyle(.titleAndIcon)
        .disabled(model.switching != nil)
        .help(paused ? Words.resumeHelp : Words.pauseHelp(facts.work))
    }
}

struct Sidebar: View {
    @Binding var place: Place?
    let facts: MainWindowFacts
    let model: MainWindowModel
    @AppStorage("kindsShown") private var kindsShown = true

    var body: some View {
        VStack(spacing: 0) {
            List(selection: $place) {
                SidebarRow(title: Words.needsYou, symbol: "hand.raised", trailing: "\(Attention.waitingOnYou(facts))")
                    .tag(Place.needsYou)
                Label(Words.library, systemImage: "books.vertical")
                    .tag(Place.library)
                SidebarRow(title: Words.spaceSaved, symbol: "arrow.down.right.and.arrow.up.left", trailing: Words.size(facts.spaceSaved.bytes))
                    .tag(Place.spaceSaved)
                // No count: the page counts shows, titles and folders, while the facts count other media by file.
                Label(Words.media, systemImage: "film.stack")
                    .tag(Place.media)
                if let kinds = model.kinds {
                    DisclosureGroup(isExpanded: $kindsShown) {
                        ForEach(kinds.kinds) { kind in
                            KindSidebarRow(kind: kind).tag(Place.kind(kind.kind))
                        }
                    } label: {
                        Label(Words.howItShouldLook, systemImage: "slider.horizontal.below.square.and.square.filled")
                    }
                }
            }
            .listStyle(.sidebar)
        }
    }
}

/// A kind under "How it should look": the space still waiting, or a check once one approval covers it.
struct KindSidebarRow: View {
    let kind: KindsFacts.Kind

    var body: some View {
        HStack {
            Text(Words.kindName(kind.kind))
            Spacer()
            if let approval = kind.approval {
                Image(systemName: "checkmark").foregroundStyle(.secondary).help(Words.kindDecided(approval))
            } else if kind.waitingBytes > 0 {
                Text(Words.size(kind.waitingBytes)).foregroundStyle(.secondary).monospacedDigit()
            }
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
