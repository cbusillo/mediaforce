import SwiftUI

/// The setup fields `/api/hosts` and `/api/app/computers` share: whether a computer can be used now and which of its
/// fixes the server offers. The buttons follow the same rules the web page uses.
enum HostsFacts {
    struct Host: Decodable, Sendable {
        let key: String
        let available: Bool
        let setupSupported: Bool
        let setupRequiresPassword: Bool
        let trustResetSupported: Bool
        let issues: [Ignored]
        let recovery: Recovery?

        struct Recovery: Decodable, Sendable {
            let status: String
            let reason: String?
        }

        /// Offered while the server is recovering a computer it can't use.
        var canCheckAgain: Bool { !available && (recovery.map { $0.status != "ready" } ?? false) }
        var canStart: Bool { !available && setupSupported }
        /// Setting up again is offered unless the computer is ready with nothing to fix.
        var canSetUp: Bool { setupSupported && !(available && !setupRequiresPassword && issues.isEmpty) }
    }

    /// An issue the server lists; only how many there are matters here.
    struct Ignored: Decodable, Sendable {
        init(from decoder: Decoder) {}
    }
}

struct HostActionResult: Decodable, Sendable {
    let ok: Bool
    let message: String?
}

enum ComputerAction: CaseIterable {
    case checkAgain, start, setUp, trustAgain
}

/// The one fix a computer's state calls for, shown first and prominent when the server offers it.
enum ComputerFix {
    static func primary(for state: String) -> ComputerAction? {
        switch state {
        case "unreachable", "new_name", "not_found", "remote_login_off": .checkAgain
        case "storage_unreachable", "unreadable_reply", "python_missing", "password_needed": .setUp
        case "identity_changed": .trustAgain
        default: nil
        }
    }

    static func offered(_ host: HostsFacts.Host) -> [ComputerAction] {
        ComputerAction.allCases.filter { action in
            switch action {
            case .checkAgain: host.canCheckAgain
            case .start: host.canStart
            case .setUp: host.canSetUp
            case .trustAgain: host.trustResetSupported
            }
        }
    }

    /// Whether a computer's row leads with its trouble and fixes instead of its work.
    static func inTrouble(_ computer: MainWindowFacts.Computer) -> Bool {
        computer.needsYou || primary(for: computer.state) != nil
    }
}

/// A computer's state as one plain line, with its fixes beside it. The password is used once and never kept.
struct ComputerStatus: View {
    let name: String
    let state: String?
    let needsYou: Bool
    let host: HostsFacts.Host?
    let act: (ComputerAction, String?) async -> HostActionResult?
    @State private var password = ""
    @State private var working: ComputerAction?
    @State private var result: HostActionResult?

    var body: some View {
        let actions = host.map(ComputerFix.offered) ?? []
        let primary = state.flatMap(ComputerFix.primary(for:)).flatMap { actions.contains($0) ? $0 : nil }
        VStack(alignment: .leading, spacing: 8) {
            // The decision: the state as one plain line, the fixes beside it, and the password a fix needs just before it.
            DecisionLine {
                HStack(alignment: .firstTextBaseline, spacing: 8) {
                    if needsYou { WaitingMark() }
                    Text(state.map(Words.computerState) ?? Words.computerNotHeardFrom).fontWeight(.medium)
                }
            } buttons: {
                if working != nil { ProgressView().controlSize(.small) }
                if host?.setupRequiresPassword == true, actions.contains(.setUp) {
                    SecureField(Words.accountPassword, text: $password, prompt: Text(Words.accountPasswordPrompt(name)))
                        .textFieldStyle(.roundedBorder)
                        .labelsHidden()
                        .frame(width: 180)
                        .onSubmit { if !password.isEmpty, working == nil { Task { await run(.setUp) } } }
                }
                ForEach(actions, id: \.self) { action in
                    let button = Button(Words.computerAction(action, needsPassword: host?.setupRequiresPassword == true)) {
                        Task { await run(action) }
                    }
                    .disabled(working != nil || (action == .setUp && host?.setupRequiresPassword == true && password.isEmpty))
                    if action == primary {
                        button.buttonStyle(.borderedProminent)
                    } else {
                        button
                    }
                }
            }
            if host?.recovery?.status == "action_required", let reason = host?.recovery?.reason {
                Text(reason)
                    .foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            }
            if let help = state.flatMap(Words.computerFixHelp) {
                Text(help)
                    .foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            }
            if let result, let message = result.message, !message.isEmpty {
                Label(message, systemImage: result.ok ? "checkmark" : "exclamationmark.triangle")
                    .foregroundStyle(result.ok ? AnyShapeStyle(.secondary) : AnyShapeStyle(Color.orange))
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
    }

    private func run(_ action: ComputerAction) async {
        working = action
        let sent = action == .setUp && !password.isEmpty ? password : nil
        password = ""
        result = await act(action, sent)
        working = nil
    }
}

/// Every computer as one row of a register: what it is, what it is doing, when it works and what it has done. A
/// computer's name opens its own page; adding, removing and changing computers is setup, in Settings › Computers.
struct ComputersPage: View {
    let facts: MainWindowFacts
    let model: MainWindowModel
    let open: (Place) -> Void
    @State private var store = ComputersStore()
    @State private var width: CGFloat = 0

    var body: some View {
        PageFrame(title: Words.computers, subtitle: Words.computersSummary(facts.computers)) {
            if facts.computers.isEmpty {
                Text(Words.noComputers).foregroundStyle(.secondary)
            } else if let machines = store.facts {
                let wide = width >= ComputerRow.wideWidth
                VStack(alignment: .leading, spacing: 0) {
                    if store.failed { ComputerFactsStale(lastHeard: store.lastHeard) }
                    if wide { ComputerRow.header }
                    ForEach(facts.computers) { computer in
                        Divider()
                        ComputerRow(computer: computer, machine: machines.machine(computer.key), now: machines.now, wide: wide,
                                    posters: model.posters, open: open) { action, password in
                            await store.act(action, computer.key, password: password, main: model)
                        }
                    }
                    Divider()
                }
            } else if store.failed {
                ComputerFactsStale(lastHeard: nil)
            } else {
                ProgressView().frame(maxWidth: .infinity, alignment: .leading)
            }
            SettingsLink(computer: nil, model: model)
        }
        .onGeometryChange(for: CGFloat.self) { $0.size.width } action: { width = $0 }
        .task { await store.watch(model.client) }
    }
}

/// One computer in the register. Wide, its facts sit in four columns under the register's header; narrow, they stack
/// under its name with the same order.
private struct ComputerRow: View {
    let computer: MainWindowFacts.Computer
    let machine: ComputersFacts.Machine?
    let now: Date
    let wide: Bool
    let posters: PosterStore
    let open: (Place) -> Void
    let act: (ComputerAction, String?) async -> HostActionResult?

    static let wideWidth: CGFloat = 1170
    private static let identityWidth: CGFloat = 250
    private static let windowWidth: CGFloat = 210
    private static let doneWidth: CGFloat = 250
    private static let spacing: CGFloat = 28

    static var header: some View {
        HStack(spacing: spacing) {
            Text(Words.computerColumn).frame(width: identityWidth, alignment: .leading)
            Text(Words.nowColumn).frame(maxWidth: .infinity, alignment: .leading)
            Text(Words.workWindowColumn).frame(width: windowWidth, alignment: .leading)
            Text(Words.doneColumn).frame(width: doneWidth, alignment: .leading)
        }
        .font(.subheadline)
        .foregroundStyle(.secondary)
        .padding(.bottom, 8)
    }

    var body: some View {
        Group {
            if wide {
                HStack(alignment: .top, spacing: Self.spacing) {
                    identity.frame(width: Self.identityWidth, alignment: .leading)
                    work.frame(maxWidth: .infinity, alignment: .leading)
                    window.frame(width: Self.windowWidth, alignment: .leading)
                    done.frame(width: Self.doneWidth, alignment: .leading)
                }
            } else {
                // Narrow, the name keeps its column and the rest stacks beside it in the same order.
                HStack(alignment: .top, spacing: Self.spacing) {
                    identity.frame(width: Self.identityWidth, alignment: .leading)
                    VStack(alignment: .leading, spacing: 16) {
                        work
                        ViewThatFits(in: .horizontal) {
                            HStack(alignment: .top, spacing: Self.spacing) {
                                window.frame(width: Self.windowWidth, alignment: .leading)
                                done.frame(width: Self.doneWidth, alignment: .leading)
                            }
                            VStack(alignment: .leading, spacing: 16) { window; done }
                        }
                    }
                    .frame(maxWidth: .infinity, alignment: .leading)
                }
            }
        }
        .padding(.vertical, 18)
    }

    private var name: String { computer.name ?? computer.key }

    private var identity: some View {
        VStack(alignment: .leading, spacing: 10) {
            Button { open(.computer(computer.key)) } label: {
                HStack(alignment: .top, spacing: 12) {
                    ComputerSymbol(symbol: Words.computerSymbol(computer, model: machine?.model), size: 30)
                    VStack(alignment: .leading, spacing: 2) {
                        Text(name).font(.title3.weight(.semibold))
                        if let machine {
                            Text(Words.computerKind(machine)).foregroundStyle(.secondary)
                            if !machine.thisMac {
                                Text(Words.computerAnswered(machine, now: now)).font(.callout).foregroundStyle(.tertiary)
                                    .fixedSize(horizontal: false, vertical: true)
                            }
                        }
                    }
                }
            }
            .buttonStyle(QuietLinkStyle(primary: true))
            .help(Words.waitingAction(.computer(computer.key)))
            if let machine { ComputerAbilities(machine: machine).padding(.leading, 56) }
        }
    }

    @ViewBuilder
    private var work: some View {
        if ComputerFix.inTrouble(computer) {
            ComputerStatus(name: name, state: computer.state, needsYou: computer.needsYou, host: machine?.host, act: act)
        } else if let running = machine?.running, !running.isEmpty {
            VStack(alignment: .leading, spacing: 12) {
                ForEach(running) { file in
                    RunningFile(file: file, posters: posters, posterWidth: 40, open: open)
                }
            }
        } else {
            VStack(alignment: .leading, spacing: 12) {
                ComputerStateLine(computer: computer)
                if let last = machine?.recent.first {
                    LastFinished(file: last, now: now, posters: posters, open: open).padding(.leading, 17)
                }
            }
        }
    }

    @ViewBuilder
    private var window: some View {
        if let machine {
            VStack(alignment: .leading, spacing: 4) {
                Text(Words.workWindowLine(machine.window, now: now))
                    .fixedSize(horizontal: false, vertical: true)
                if !machine.window.name.isEmpty {
                    Text(machine.window.name).font(.callout).foregroundStyle(.secondary)
                }
                WeekRibbon(week: machine.window.week, now: now, height: 8, showsDays: false)
                    .padding(.top, 6)
            }
        }
    }

    @ViewBuilder
    private var done: some View {
        if let machine {
            VStack(alignment: .leading, spacing: 8) {
                TallyGrid(rows: [(Words.today, machine.done.today), (Words.pastWeek, machine.done.week)])
                Text(Words.hourTakes(machine.hourTakesSeconds))
                    .font(.callout)
                    .foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
    }
}

/// One computer's own page: the register row's facts with room to breathe, its week of work windows, everything it has
/// done and recently finished, and how it is set up.
struct ComputerPage: View {
    let key: String
    let facts: MainWindowFacts
    let model: MainWindowModel
    let open: (Place) -> Void
    var namePage: (String) -> Void = { _ in }
    @State private var store = ComputersStore()
    @State private var width: CGFloat = 0

    var body: some View {
        let computer = facts.computers.first { $0.key == key }
        let machine = store.facts?.machine(key)
        let name = computer?.name ?? machine?.name ?? key
        let now = store.facts?.now ?? .now
        PageFrame(title: name, subtitle: computer.map { Words.computerState($0.state) } ?? "") {
            if store.failed { ComputerFactsStale(lastHeard: store.lastHeard).padding(.horizontal, -34) }
            header(name: name, computer: computer, machine: machine, now: now)
            if let computer, ComputerFix.inTrouble(computer) {
                ComputerStatus(name: name, state: computer.state, needsYou: computer.needsYou, host: machine?.host) { action, password in
                    await store.act(action, key, password: password, main: model)
                }
                .padding(14)
                .background(Color.orange.opacity(0.08), in: RoundedRectangle(cornerRadius: 6))
            }
            if let machine {
                if width >= 900 {
                    HStack(alignment: .top, spacing: 44) {
                        VStack(alignment: .leading, spacing: 28) { nowSection(computer, machine); recentSection(machine) }
                            .frame(maxWidth: .infinity, alignment: .leading)
                        VStack(alignment: .leading, spacing: 28) {
                            windowSection(machine, now: now); doneSection(machine); setupSection(machine)
                        }
                        .frame(width: 330, alignment: .leading)
                    }
                } else {
                    nowSection(computer, machine)
                    windowSection(machine, now: now)
                    doneSection(machine)
                    recentSection(machine)
                    setupSection(machine)
                }
            } else if !store.failed {
                ProgressView()
            }
        }
        .onGeometryChange(for: CGFloat.self) { $0.size.width } action: { width = $0 }
        .onAppear { namePage(name) }
        // Recovery runs by itself in the background, so the fixes on offer can change while the page is open.
        .task { await store.watch(model.client) }
    }

    private func header(name: String, computer: MainWindowFacts.Computer?, machine: ComputersFacts.Machine?, now: Date) -> some View {
        HStack(alignment: .center, spacing: 16) {
            ComputerSymbol(symbol: computer.map { Words.computerSymbol($0, model: machine?.model) } ?? "desktopcomputer", size: 44)
            VStack(alignment: .leading, spacing: 3) {
                Text(name).font(.title2.weight(.semibold))
                if let machine {
                    Text(Words.computerKind(machine)).foregroundStyle(.secondary)
                    if !machine.thisMac { Text(Words.computerAnswered(machine, now: now)).foregroundStyle(.tertiary) }
                }
            }
        }
    }

    private func nowSection(_ computer: MainWindowFacts.Computer?, _ machine: ComputersFacts.Machine) -> some View {
        PageSection(Words.nowColumn) {
            if !machine.running.isEmpty {
                ForEach(machine.running) { file in
                    RunningFile(file: file, posters: model.posters, posterWidth: 64, open: open)
                }
            } else if let computer, !ComputerFix.inTrouble(computer) {
                ComputerStateLine(computer: computer)
            } else {
                Text(Words.nothingCompressing).foregroundStyle(.secondary)
            }
        }
    }

    private func windowSection(_ machine: ComputersFacts.Machine, now: Date) -> some View {
        PageSection(Words.workWindowColumn) {
            Text(Words.workWindowLine(machine.window, now: now)).fixedSize(horizontal: false, vertical: true)
            if !machine.window.name.isEmpty {
                Text(machine.window.name).font(.callout).foregroundStyle(.secondary)
            }
            WeekRibbon(week: machine.window.week, now: now, height: 16, showsDays: true).padding(.top, 4)
        }
    }

    private func doneSection(_ machine: ComputersFacts.Machine) -> some View {
        PageSection(Words.doneColumn) {
            TallyGrid(rows: [(Words.today, machine.done.today), (Words.pastWeek, machine.done.week),
                             (Words.since(machine.done.since), machine.done.all)])
            Text(Words.hourTakes(machine.hourTakesSeconds))
                .foregroundStyle(.secondary)
                .fixedSize(horizontal: false, vertical: true)
        }
    }

    private func recentSection(_ machine: ComputersFacts.Machine) -> some View {
        PageSection(Words.recentlyFinished) {
            if machine.recent.isEmpty {
                Text(Words.nothingFinished).foregroundStyle(.secondary)
            }
            VStack(spacing: 0) {
                ForEach(machine.recent) { file in
                    FinishedFile(file: file, posters: model.posters, open: open)
                    Divider()
                }
            }
        }
    }

    private func setupSection(_ machine: ComputersFacts.Machine) -> some View {
        PageSection(Words.setUpHeading) {
            ComputerAbilities(machine: machine)
            Text(Words.computerAddressLine(machine)).font(.callout).foregroundStyle(.secondary).textSelection(.enabled)
            SettingsLink(computer: key, model: model).padding(.top, 4)
        }
    }
}

private struct PageSection<Content: View>: View {
    let title: String
    @ViewBuilder let content: Content

    init(_ title: String, @ViewBuilder content: () -> Content) {
        self.title = title
        self.content = content()
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            Text(title).font(.headline)
            content
        }
    }
}

private struct ComputerSymbol: View {
    let symbol: String
    let size: CGFloat

    var body: some View {
        Image(systemName: symbol)
            .font(.system(size: size, weight: .thin))
            .foregroundStyle(.secondary)
            .frame(width: size * 1.45, height: size * 1.1)
            .accessibilityHidden(true)
    }
}

/// The computer's state as one line with a mark for what kind of state it is: working, waiting, or in trouble.
private struct ComputerStateLine: View {
    let computer: MainWindowFacts.Computer

    var body: some View {
        HStack(alignment: .firstTextBaseline, spacing: 8) {
            let working = computer.state == "busy" || computer.state == "finishing_file"
            Circle()
                .strokeBorder(working || computer.state == "ready" ? Color.green : Color.secondary.opacity(0.6), lineWidth: 1.5)
                .background(Circle().fill(working ? Color.green : Color.clear))
                .frame(width: 9, height: 9)
                .accessibilityHidden(true)
            Text(Words.computerState(computer.state))
                .fixedSize(horizontal: false, vertical: true)
        }
    }
}

/// A file compressing now: its show's poster, its name in plain words, how far along it is and when it finishes.
private struct RunningFile: View {
    let file: ComputersFacts.File
    let posters: PosterStore
    let posterWidth: CGFloat
    let open: (Place) -> Void

    var body: some View {
        Button { open(.item(file.prefix)) } label: {
            HStack(alignment: .center, spacing: 12) {
                MediaPoster(titlePrefix: file.showPrefix, name: file.show, kind: file.mediaKind, posters: posters, width: posterWidth)
                VStack(alignment: .leading, spacing: 5) {
                    Text(Words.fileTitle(file)).fontWeight(.medium).foregroundStyle(.primary)
                        .fixedSize(horizontal: false, vertical: true)
                    if let progress = file.progress {
                        ProgressView(value: progress).accessibilityLabel(Words.fileTitle(file))
                    } else {
                        ProgressView().progressViewStyle(.linear).controlSize(.small).accessibilityLabel(Words.runningStage(file))
                    }
                    HStack {
                        Text(Words.runningStage(file))
                        Spacer(minLength: 8)
                        Text(Words.finishesAbout(file.finishesAt))
                    }
                    .font(.callout)
                    .foregroundStyle(.secondary)
                    .monospacedDigit()
                }
            }
        }
        .buttonStyle(QuietLinkStyle(primary: true))
    }
}

/// The file a resting computer finished last, so its row still says what it has been doing.
private struct LastFinished: View {
    let file: ComputersFacts.File
    let now: Date
    let posters: PosterStore
    let open: (Place) -> Void

    var body: some View {
        Button { open(.item(file.prefix)) } label: {
            HStack(spacing: 10) {
                MediaPoster(titlePrefix: file.showPrefix, name: file.show, kind: file.mediaKind, posters: posters, width: 24)
                VStack(alignment: .leading, spacing: 1) {
                    Text(Words.fileTitle(file)).foregroundStyle(.primary)
                    Text(Words.lastFinished(file, now: now)).foregroundStyle(.secondary)
                }
                .font(.callout)
            }
        }
        .buttonStyle(QuietLinkStyle(primary: true))
    }
}

private struct FinishedFile: View {
    let file: ComputersFacts.File
    let posters: PosterStore
    let open: (Place) -> Void

    var body: some View {
        Button { open(.item(file.prefix)) } label: {
            HStack(spacing: 10) {
                MediaPoster(titlePrefix: file.showPrefix, name: file.show, kind: file.mediaKind, posters: posters, width: 22)
                Text(Words.fileTitle(file)).frame(maxWidth: .infinity, alignment: .leading).lineLimit(1)
                Text(Words.finishedSaving(file)).foregroundStyle(.secondary)
                Text(Words.day(file.finishedAt)).foregroundStyle(.tertiary).frame(width: 52, alignment: .trailing)
            }
            .monospacedDigit()
            .padding(.vertical, 6)
        }
        .buttonStyle(QuietLinkStyle(primary: true))
    }
}

private struct TallyGrid: View {
    let rows: [(String, ComputersFacts.Tally)]

    var body: some View {
        Grid(alignment: .leadingFirstTextBaseline, horizontalSpacing: 14, verticalSpacing: 4) {
            ForEach(rows, id: \.0) { label, tally in
                GridRow {
                    Text(label).foregroundStyle(.secondary)
                    Text(Words.count(tally.files, "file")).gridColumnAlignment(.trailing)
                    Text(tally.files == 0 ? "" : Words.saved(tally.bytesSaved)).foregroundStyle(.secondary)
                }
            }
        }
        .monospacedDigit()
    }
}

/// What the computer does, which libraries it takes, and how it reaches the media, each as one short line.
private struct ComputerAbilities: View {
    let machine: ComputersFacts.Machine

    var body: some View {
        VStack(alignment: .leading, spacing: 3) {
            Text(Words.computerDoes(machine))
            Text(Words.computerLibraries(machine))
            Text(Words.computerStorage(machine))
                .foregroundStyle(machine.storage == "missing" ? AnyShapeStyle(Color.orange) : AnyShapeStyle(.secondary))
        }
        .font(.callout)
        .foregroundStyle(.secondary)
    }
}

/// The next seven days, today first, with the hours the computer may work filled in and a mark at now.
private struct WeekRibbon: View {
    let week: [ComputersFacts.Span]
    let now: Date
    let height: CGFloat
    let showsDays: Bool

    private var start: Date { Calendar.current.startOfDay(for: now) }
    private static let length: TimeInterval = 7 * 24 * 3600

    private func fraction(_ date: Date) -> CGFloat {
        CGFloat(min(1, max(0, date.timeIntervalSince(start) / Self.length)))
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            GeometryReader { geometry in
                let width = geometry.size.width
                ZStack(alignment: .topLeading) {
                    Rectangle().fill(Color.primary.opacity(0.07)).frame(height: height)
                    ForEach(Array(week.enumerated()), id: \.offset) { _, span in
                        if let from = ActivityFacts.date(span.startsAt), let to = ActivityFacts.date(span.endsAt) {
                            Rectangle().fill(Color.green.opacity(0.55))
                                .frame(width: max(1, width * (fraction(to) - fraction(from))), height: height)
                                .offset(x: width * fraction(from))
                        }
                    }
                    ForEach(0..<8) { day in
                        Rectangle().fill(Color.primary.opacity(0.35)).frame(width: 1, height: 4)
                            .offset(x: min(width - 1, width * CGFloat(day) / 7), y: height + 1)
                    }
                    Rectangle().fill(Color.primary).frame(width: 2, height: height + 6)
                        .offset(x: width * fraction(now) - 1, y: -3)
                }
            }
            .frame(height: height + 5)
            if showsDays {
                HStack(spacing: 0) {
                    ForEach(0..<7) { day in
                        Text(Words.ribbonDay(Calendar.current.date(byAdding: .day, value: day, to: start) ?? start, first: day == 0))
                            .frame(maxWidth: .infinity, alignment: .leading)
                    }
                }
                .font(.caption)
                .foregroundStyle(.secondary)
            }
        }
        .accessibilityElement(children: .ignore)
        .accessibilityLabel(Words.workWindowColumn)
    }
}

private struct SettingsLink: View {
    let computer: String?
    let model: MainWindowModel
    @Environment(\.openSettings) private var openSettings

    var body: some View {
        Button(Words.openInSettings) {
            model.settingsRequest = SettingsRequest(computer: computer)
            openSettings()
        }
        .buttonStyle(QuietLinkStyle())
    }
}

private struct ComputerFactsStale: View {
    let lastHeard: Date?

    var body: some View {
        Label(Words.computerFactsStale(lastHeard), systemImage: "exclamationmark.triangle")
            .font(.callout)
            .padding(.vertical, 8)
            .frame(maxWidth: .infinity, alignment: .leading)
    }
}

/// Each computer's facts from the server, read again on the main window's interval and after a fix runs.
@MainActor
@Observable
final class ComputersStore {
    private(set) var facts: ComputersFacts?
    private(set) var failed = false
    private(set) var lastHeard: Date?

    func load(_ client: MediaforceClient) async {
        do {
            facts = try await client.computers()
            failed = false
            lastHeard = .now
        } catch {
            failed = true
        }
    }

    func watch(_ client: MediaforceClient) async {
        while !Task.isCancelled {
            await load(client)
            try? await Task.sleep(for: MainWindowModel.refreshInterval)
        }
    }

    /// Runs one fix, then reads the computers' facts again so the page shows what changed.
    func act(_ action: ComputerAction, _ address: String, password: String?, main: MainWindowModel) async -> HostActionResult? {
        let result = try? await main.client.computerAction(action, address, password: password)
        await load(main.client)
        await main.refresh()
        return result ?? HostActionResult(ok: false, message: ActionNotice.unanswered.message)
    }
}

extension ComputersFacts {
    func machine(_ key: String) -> Machine? { computers.first { $0.key == key } }
    var now: Date { ActivityFacts.date(observedAt) ?? .now }
}

extension Words {
    static let computerColumn = "Computer"
    static let nowColumn = "Now"
    static let workWindowColumn = "Work window"
    static let doneColumn = "Done"
    static let today = "Today"
    static let pastWeek = "Past 7 days"
    static let recentlyFinished = "Recently finished"
    static let setUpHeading = "Set up"
    static let nothingCompressing = "Not compressing anything."
    static let nothingFinished = "Nothing finished yet."

    static func computersSummary(_ computers: [MainWindowFacts.Computer]) -> String {
        let compressing = computers.filter { $0.state == "busy" || $0.state == "finishing_file" }.count
        let trouble = computers.filter(ComputerFix.inTrouble).count
        let waiting = computers.filter { $0.state == "outside_schedule" }.count
        var parts: [String] = []
        if compressing > 0 { parts.append("\(compressing) compressing") }
        if trouble > 0 { parts.append("\(trouble) \(trouble == 1 ? "needs" : "need") you") }
        if waiting > 0 { parts.append("\(waiting) waiting for \(waiting == 1 ? "its" : "their") work window") }
        return parts.isEmpty ? count(computers.count, "computer") : parts.joined(separator: ", ")
    }

    /// The kind of computer in plain words, such as "MacBook Pro"; the Mac Mediaforce runs on says so.
    static func computerKind(_ machine: ComputersFacts.Machine) -> String {
        let kind = machine.model ?? (machine.platform == "linux" ? "Linux computer" : machine.platform == "macos" ? "Mac" : "Computer")
        return machine.thisMac ? "\(kind), this Mac" : kind
    }

    static func computerSymbol(_ computer: MainWindowFacts.Computer, model: String?) -> String {
        switch model?.lowercased() {
        case let model? where model.contains("macbook"): "laptopcomputer"
        case let model? where model.contains("mac mini"): "macmini"
        case let model? where model.contains("mac studio"): "macstudio"
        case let model? where model.contains("mac pro"): "macpro.gen3"
        case let model? where model.contains("imac"): "desktopcomputer"
        default: computerSymbol(computer)
        }
    }

    static func computerAnswered(_ machine: ComputersFacts.Machine, now: Date) -> String {
        guard let at = machine.lastAnsweredAt.flatMap(ActivityFacts.date) else { return computerNotHeardFrom }
        if machine.host.recovery?.status != "ready" { return "Hasn't answered since \(moment(at, now: now))" }
        let seconds = now.timeIntervalSince(at)
        if seconds < 90 { return "Answered just now" }
        if seconds < 3600 { return "Answered \(Int(seconds / 60)) min ago" }
        return "Answered \(moment(at, now: now))"
    }

    static func computerDoes(_ machine: ComputersFacts.Machine) -> String {
        switch (machine.compresses, machine.makesSamples) {
        case (true, true): "Compresses and makes samples"
        case (true, false): "Compresses files"
        case (false, true): "Makes samples only"
        case (false, false): "Not given any work"
        }
    }

    static func computerLibraries(_ machine: ComputersFacts.Machine) -> String {
        let kinds = machine.libraryKinds.map { MediaKind(rawValue: $0 == "movie" ? "movies" : $0) ?? .other }
        guard !kinds.isEmpty else { return "Every library" }
        return "Only " + ListFormatter.localizedString(byJoining: kinds.map(mediaKind))
    }

    static func computerStorage(_ machine: ComputersFacts.Machine) -> String {
        switch machine.storage {
        case "sent": "Files are sent to it"
        case "missing": "Shared folders not connected"
        default: "Shared folders connected"
        }
    }

    static func computerAddressLine(_ machine: ComputersFacts.Machine) -> String {
        "Reached at \(machine.key)"
    }

    static func workWindowLine(_ window: ComputersFacts.Window, now: Date) -> String {
        if window.open {
            guard let closes = window.closesAt.flatMap(ActivityFacts.date) else { return "Open all the time" }
            return "Open until \(moment(closes, now: now))"
        }
        guard let opens = window.opensAt.flatMap(ActivityFacts.date) else { return "No work window coming up" }
        guard let closes = window.nextClosesAt.flatMap(ActivityFacts.date) else { return "Opens \(moment(opens, now: now))" }
        let until = Calendar.current.isDate(closes, inSameDayAs: opens) ? clock(closes) : moment(closes, now: now)
        return "Opens \(moment(opens, now: now)) until \(until)"
    }

    /// A time in the coming week as a person says it: "6 PM", "tomorrow 1 AM", "Mon 3 PM".
    static func moment(_ date: Date, now: Date) -> String {
        let calendar = Calendar.current
        let time = clock(date)
        if calendar.isDate(date, inSameDayAs: now) { return time }
        if let tomorrow = calendar.date(byAdding: .day, value: 1, to: now), calendar.isDate(date, inSameDayAs: tomorrow) {
            return "tomorrow \(time)"
        }
        if let yesterday = calendar.date(byAdding: .day, value: -1, to: now), calendar.isDate(date, inSameDayAs: yesterday) {
            return "yesterday \(time)"
        }
        if abs(date.timeIntervalSince(now)) < 6 * 24 * 3600 {
            return "\(date.formatted(.dateTime.weekday(.abbreviated))) \(time)"
        }
        return "\(date.formatted(.dateTime.month(.abbreviated).day())) \(time)"
    }

    static func clock(_ date: Date) -> String {
        Calendar.current.component(.minute, from: date) == 0
            ? date.formatted(.dateTime.hour())
            : date.formatted(.dateTime.hour().minute())
    }

    static func ribbonDay(_ date: Date, first: Bool) -> String {
        first ? "Today" : date.formatted(.dateTime.weekday(.abbreviated))
    }

    static func saved(_ bytes: Int64) -> String {
        bytes > 0 ? "\(size(bytes)) saved" : "none replaced yet"
    }

    static func since(_ iso: String?) -> String {
        iso == nil ? "In all" : "Since \(day(iso))"
    }

    static func hourTakes(_ seconds: Int?) -> String {
        guard let seconds else { return "Speed not known until it finishes a file" }
        return "About \(hoursAndMinutes(seconds)) per hour of HD video"
    }

    static func hoursAndMinutes(_ seconds: Int) -> String {
        let minutes = Int((Double(seconds) / 60).rounded())
        let hours = minutes / 60
        if hours == 0 { return "\(minutes) min" }
        return minutes % 60 == 0 ? "\(hours) h" : "\(hours) h \(minutes % 60) min"
    }

    /// A file as a person names it: the show, season and episode, or the movie.
    static func fileTitle(_ file: ComputersFacts.File) -> String {
        let name = file.season.map { season(file.show, $0) } ?? file.show
        let episode = file.episode.map { "\(name), Episode \($0)" } ?? name
        guard let count = file.fileCount, count > 1, file.episode == nil else { return episode }
        return "\(episode), \(count) files"
    }

    static func runningStage(_ file: ComputersFacts.File) -> String {
        switch file.stage {
        case "finding_size": "Finding the right size"
        case "getting_ready": "Getting ready"
        default: file.progress.map { "Compressing, \(Int(($0 * 100).rounded()))%" } ?? "Compressing"
        }
    }

    static func finishesAbout(_ iso: String?) -> String {
        guard let date = iso.flatMap(ActivityFacts.date) else { return "Finish time not known yet" }
        return "Finishes about \(moment(date, now: .now))"
    }

    static func finishedSaving(_ file: ComputersFacts.File) -> String {
        file.replaced == true ? "\(size(file.bytesSaved ?? 0)) saved" : "Not replaced yet"
    }

    static func lastFinished(_ file: ComputersFacts.File, now: Date) -> String {
        let when = file.finishedAt.flatMap(ActivityFacts.date).map { "Finished \(moment($0, now: now))" } ?? "Finished"
        return file.replaced == true ? "\(when), \(size(file.bytesSaved ?? 0)) saved" : "\(when), not replaced yet"
    }

    static func computerFactsStale(_ lastHeard: Date?) -> String {
        let since = lastHeard.map { " Showing what it said at \($0.formatted(date: .omitted, time: .shortened))." } ?? ""
        return "These computer facts didn't refresh.\(since)"
    }
}
