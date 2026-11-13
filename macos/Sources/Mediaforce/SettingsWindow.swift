import AppKit
import SwiftUI

struct SettingsWindow: View {
    enum Tab: Hashable {
        case libraries, compression, computers, metadata
    }

    let model: MainWindowModel
    @State private var tab = Tab.libraries

    var body: some View {
        TabView(selection: $tab) {
            LibrarySettingsTab(main: model)
                .tabItem { Label(Words.librariesTab, systemImage: "books.vertical") }
                .tag(Tab.libraries)
            CompressionSettingsTab(main: model)
                .tabItem { Label(Words.compressionTab, systemImage: "arrow.down.right.and.arrow.up.left") }
                .tag(Tab.compression)
            ComputerSettingsTab(main: model)
                .tabItem { Label(Words.computersTab, systemImage: "desktopcomputer") }
                .tag(Tab.computers)
            MetadataSettingsTab(main: model)
                .tabItem { Label(Words.metadataTab, systemImage: "network") }
                .tag(Tab.metadata)
        }
        .frame(width: 760)
        // Asked to show a computer, or the Computers tab, from a notification, the main window or the menu bar.
        .onChange(of: model.settingsRequest, initial: true) { _, request in
            guard let request else { return }
            tab = .computers
            model.requestedComputer = request.computer
            model.settingsRequest = nil
        }
    }
}

@MainActor
@Observable
final class LibrarySettingsModel {
    struct PendingKind: Identifiable {
        let id: LibrarySettingsDraft.Row.ID
        let preview: LibraryTypePreview
        let generation: Int
    }

    let client: MediaforceClient
    private(set) var saved: LibrarySettingsFacts?
    private(set) var loadFailed = false
    private(set) var saving = false
    private(set) var justSaved = false
    var draft: LibrarySettingsDraft?
    var notice: ActionNotice?
    var pendingKind: PendingKind?
    var pendingRemoval: LibrarySettingsDraft.Row?
    /// Save, Revert and every reload bump this, so a kind preview asked for earlier can't edit the new draft.
    private var draftGeneration = 0

    init(client: MediaforceClient) {
        self.client = client
    }

    var hasChanges: Bool {
        guard let draft, let saved else { return false }
        return draft.saveBody != LibrarySettingsDraft(saved).saveBody
    }

    func load() async {
        do {
            let facts = try await client.librarySettings()
            show(facts)
        } catch {
            loadFailed = true
        }
    }

    func revert() {
        if let saved { show(saved) }
    }

    func chooseKind(_ kind: String, for id: LibrarySettingsDraft.Row.ID) async {
        guard !saving, let saved,
              case .needsPreview(let key)? = draft?.chooseType(kind, for: id, saved: saved) else { return }
        let generation = draftGeneration
        do {
            let preview = try await client.libraryTypePreview(key: key, type: kind)
            guard generation == draftGeneration, !saving else { return }
            pendingKind = PendingKind(id: id, preview: preview, generation: generation)
        } catch {
            if generation == draftGeneration { notice = .previewUnavailable }
        }
    }

    func confirmKind(_ pending: PendingKind) {
        pendingKind = nil
        guard !saving, pending.generation == draftGeneration else { return }
        draft?.confirmType(pending.preview, for: pending.id)
    }

    func confirmRemoval(_ row: LibrarySettingsDraft.Row) {
        pendingRemoval = nil
        guard !saving else { return }
        draft?.remove(row.id)
    }

    func save() async {
        guard let draft, !saving else { return }
        discardPendingEdits()
        saving = true
        defer { saving = false }
        do {
            let result = try await client.saveLibrarySettings(draft.saveBody)
            if result.ok {
                if let settings = result.settings { show(settings) } else { await load() }
                justSaved = true
            } else if result.code == "settings_changed" {
                notice = .settingsChangedElsewhere
                await load()
            } else {
                notice = .settingsNotSaved(result.code)
            }
        } catch {
            notice = .unanswered
        }
    }

    func edited() {
        if hasChanges { justSaved = false }
    }

    private func discardPendingEdits() {
        draftGeneration += 1
        pendingKind = nil
        pendingRemoval = nil
    }

    private func show(_ facts: LibrarySettingsFacts) {
        discardPendingEdits()
        saved = facts
        draft = LibrarySettingsDraft(facts)
        loadFailed = false
    }
}

struct LibrarySettingsTab: View {
    let main: MainWindowModel
    @State private var model: LibrarySettingsModel

    init(main: MainWindowModel) {
        self.main = main
        _model = State(initialValue: LibrarySettingsModel(client: main.client))
    }

    var body: some View {
        Group {
            if let saved = model.saved, model.draft != nil {
                LibrarySettingsForm(model: model, kinds: saved.types)
            } else if model.loadFailed {
                SettingsLoadFailure(main: main) {
                    Task { await model.load() }
                }
            } else {
                ProgressView()
                    .frame(maxWidth: .infinity, minHeight: 240)
            }
        }
        .task { await model.load() }
        .alert(item: $model.notice) { notice in
            Alert(title: Text(notice.title), message: Text(notice.message))
        }
    }
}

struct LibrarySettingsForm: View {
    @Bindable var model: LibrarySettingsModel
    let kinds: [String]

    var body: some View {
        VStack(spacing: 0) {
            Form {
                ForEach(rows) { $row in
                    Section(row.library.name.isEmpty ? Words.unnamedLibrary : row.library.name) {
                        LibraryRowEditor(row: $row, kinds: kinds, model: model)
                    }
                }
                Section {
                    if model.draft?.rows.isEmpty ?? true {
                        Text(Words.noLibraries)
                            .foregroundStyle(.secondary)
                    }
                    Button(Words.addLibrary, systemImage: "plus") {
                        model.draft?.addLibrary(type: kinds.last ?? "other")
                    }
                }
                Section(Words.workingSpaceSection) {
                    FolderField(title: Words.workingFolder, help: Words.workingFolderHelp, path: workingFolder)
                    SettingsRow(Words.lookForNewFiles, help: Words.lookForNewFilesHelp) {
                        AmountField(title: Words.lookForNewFiles, value: refreshHours, format: .number, unit: Words.hours)
                    }
                }
            }
            .formStyle(.grouped)
            .disabled(model.saving)
            .onChange(of: model.draft) { model.edited() }
            HStack {
                if model.justSaved {
                    Text(Words.settingsSaved)
                        .foregroundStyle(.secondary)
                }
                Spacer()
                Button(Words.revertSettings) { model.revert() }
                    .disabled(!model.hasChanges || model.saving)
                Button(Words.saveSettings) { Task { await model.save() } }
                    .keyboardShortcut(.defaultAction)
                    .disabled(!model.hasChanges || model.saving)
            }
            .padding(16)
        }
        .frame(minHeight: 460)
        .confirmationDialog(
            model.pendingKind.map { Words.changeKindTitle($0.preview) } ?? "",
            isPresented: Binding(get: { model.pendingKind != nil }, set: { if !$0 { model.pendingKind = nil } }),
            presenting: model.pendingKind
        ) { pending in
            Button(Words.changeKind) { model.confirmKind(pending) }
            Button(Words.keepCurrentKind, role: .cancel) { model.pendingKind = nil }
        } message: { pending in
            Text(Words.changeKindMessage(pending.preview))
        }
        .confirmationDialog(
            Words.removeLibraryTitle(model.pendingRemoval?.library.name ?? ""),
            isPresented: Binding(get: { model.pendingRemoval != nil }, set: { if !$0 { model.pendingRemoval = nil } }),
            presenting: model.pendingRemoval
        ) { row in
            Button(Words.removeFromMediaforce, role: .destructive) { model.confirmRemoval(row) }
            Button(Words.keepLibrary, role: .cancel) { model.pendingRemoval = nil }
        } message: { _ in
            Text(Words.removeLibraryMessage)
        }
    }

    private var rows: Binding<[LibrarySettingsDraft.Row]> {
        Binding(get: { model.draft?.rows ?? [] }, set: { model.draft?.rows = $0 })
    }

    private var workingFolder: Binding<String> {
        Binding(get: { model.draft?.workingFolder ?? "" }, set: { model.draft?.workingFolder = $0 })
    }

    private var refreshHours: Binding<Double> {
        Binding(get: { model.draft?.catalogRefreshHours ?? 0 }, set: { model.draft?.catalogRefreshHours = $0 })
    }
}

struct LibraryRowEditor: View {
    @Binding var row: LibrarySettingsDraft.Row
    let kinds: [String]
    let model: LibrarySettingsModel

    var body: some View {
        TextField(Words.libraryName, text: $row.library.name)
        Picker(Words.libraryType, selection: kind) {
            ForEach(kinds, id: \.self) { code in
                Text(Words.libraryKind(code)).tag(code)
            }
        }
        FolderField(title: Words.libraryFolder, path: $row.library.folder)
        DisclosureGroup(Words.plexPathSummary(row.library.plexPath)) {
            MonospacedField(title: Words.plexPath, help: Words.plexPathHelp, text: $row.library.plexPath)
        }
        Button(Words.removeLibrary, systemImage: "minus.circle", role: .destructive) { model.pendingRemoval = row }
    }

    private var kind: Binding<String> {
        let id = row.id
        return Binding(get: { row.library.type }, set: { chosen in
            Task { await model.chooseKind(chosen, for: id) }
        })
    }
}

/// A Settings row in the standard macOS layout: a one-line label with any help under it, and the control on the right.
struct SettingsRow<Control: View>: View {
    let title: String
    let help: String?
    let control: Control

    init(_ title: String, help: String? = nil, @ViewBuilder control: () -> Control) {
        self.title = title
        self.help = help
        self.control = control()
    }

    var body: some View {
        LabeledContent {
            control
        } label: {
            Text(title)
            if let help {
                Text(help)
            }
        }
    }
}

/// A small number box with its unit after it, for the control side of a `SettingsRow`.
struct AmountField<Format: ParseableFormatStyle>: View where Format.FormatOutput == String {
    let title: String
    @Binding var value: Format.FormatInput
    let format: Format
    var unit: String?

    var body: some View {
        HStack(spacing: 6) {
            TextField(title, value: $value, format: format)
                .labelsHidden()
                .textFieldStyle(.roundedBorder)
                .multilineTextAlignment(.trailing)
                .frame(width: 72)
            if let unit { Text(unit) }
        }
        .fixedSize()
    }
}

/// A whole number with its stepper after it, for the control side of a `SettingsRow`.
struct CountStepper: View {
    let title: String
    @Binding var value: Int
    let range: ClosedRange<Int>

    var body: some View {
        HStack(spacing: 6) {
            Text(value.formatted())
                .monospacedDigit()
            Stepper(title, value: $value, in: range)
                .labelsHidden()
        }
    }
}

/// A path, address or command: the label stays in the body font and only the value is monospaced.
struct MonospacedField: View {
    let title: String
    var help: String?
    @Binding var text: String
    var prompt: String?

    var body: some View {
        SettingsRow(title, help: help) {
            TextField(title, text: $text, prompt: prompt.map { Text($0) })
                .labelsHidden()
                .font(.body.monospaced())
                .multilineTextAlignment(.trailing)
        }
    }
}

struct FolderField: View {
    let title: String
    var help: String?
    @Binding var path: String

    var body: some View {
        SettingsRow(title, help: help) {
            HStack {
                TextField(title, text: $path)
                    .labelsHidden()
                    .font(.body.monospaced())
                    .multilineTextAlignment(.trailing)
                Button(Words.chooseFolder) { choose() }
            }
        }
    }

    private func choose() {
        let panel = NSOpenPanel()
        panel.canChooseDirectories = true
        panel.canChooseFiles = false
        panel.allowsMultipleSelection = false
        if !path.isEmpty {
            panel.directoryURL = URL(filePath: path, directoryHint: .isDirectory)
        }
        if panel.runModal() == .OK, let url = panel.url {
            path = url.path(percentEncoded: false)
        }
    }
}

@MainActor
@Observable
final class ComputerSettingsModel {
    let client: MediaforceClient
    private(set) var saved: ComputerSettingsFacts?
    private(set) var loadFailed = false
    private(set) var saving = false
    private(set) var justSaved = false
    var draft: ComputerSettingsDraft?
    var notice: ActionNotice?
    var pendingComputerRemoval: ComputerSettingsDraft.ComputerRow?
    var pendingScheduleRemoval: ComputerSettingsDraft.ScheduleRow?
    /// Each computer's setup facts from the server, by address; empty until they arrive.
    private(set) var hosts: [String: HostsFacts.Host] = [:]

    init(client: MediaforceClient) {
        self.client = client
    }

    func loadHosts() async {
        if let facts = try? await client.hosts() {
            hosts = Dictionary(facts.hosts.map { ($0.key, $0) }, uniquingKeysWith: { first, _ in first })
        }
    }

    /// Runs one fix, then reads the computers' state again so the page shows what changed.
    func act(_ action: ComputerAction, _ address: String, password: String?, main: MainWindowModel) async -> HostActionResult? {
        let result = try? await client.computerAction(action, address, password: password)
        await loadHosts()
        await main.refresh()
        return result ?? HostActionResult(ok: false, message: ActionNotice.unanswered.message)
    }

    var hasChanges: Bool {
        guard let draft, let saved else { return false }
        return draft.saveBody != ComputerSettingsDraft(saved).saveBody
    }

    func load() async {
        do {
            let facts = try await client.computerSettings()
            show(facts)
        } catch {
            loadFailed = true
        }
        await loadHosts()
    }

    func revert() {
        if let saved { show(saved) }
    }

    func confirmRemoval(_ row: ComputerSettingsDraft.ComputerRow) {
        pendingComputerRemoval = nil
        guard !saving else { return }
        draft?.removeComputer(row.id)
    }

    func confirmRemoval(_ row: ComputerSettingsDraft.ScheduleRow) {
        pendingScheduleRemoval = nil
        guard !saving else { return }
        draft?.removeSchedule(row.id)
    }

    func save() async {
        guard let draft, !saving else { return }
        discardPendingEdits()
        saving = true
        defer { saving = false }
        do {
            let result = try await client.saveComputerSettings(draft.saveBody)
            if result.ok {
                // Said before the rows are rebuilt, so the list keeps the chosen computer by its place.
                justSaved = true
                if let settings = result.settings { show(settings) } else { await load() }
                // A new or readdressed computer has its own setup facts.
                await loadHosts()
            } else if result.code == "settings_changed" {
                notice = .settingsChangedElsewhere
                await load()
            } else {
                notice = .computerSettingsNotSaved(result.code)
            }
        } catch {
            notice = .unanswered
        }
    }

    func edited() {
        if hasChanges { justSaved = false }
    }

    private func discardPendingEdits() {
        pendingComputerRemoval = nil
        pendingScheduleRemoval = nil
    }

    private func show(_ facts: ComputerSettingsFacts) {
        discardPendingEdits()
        saved = facts
        draft = ComputerSettingsDraft(facts)
        loadFailed = false
    }
}

struct ComputerSettingsTab: View {
    let main: MainWindowModel
    @State private var model: ComputerSettingsModel

    init(main: MainWindowModel) {
        self.main = main
        _model = State(initialValue: ComputerSettingsModel(client: main.client))
    }

    var body: some View {
        Group {
            if let saved = model.saved, model.draft != nil {
                ComputerSettingsForm(model: model, facts: saved, main: main)
            } else if model.loadFailed {
                SettingsLoadFailure(main: main) {
                    Task { await model.load() }
                }
            } else {
                ProgressView()
                    .frame(maxWidth: .infinity, minHeight: 240)
            }
        }
        .task { await model.load() }
        // Recovery runs by itself in the background, so the fixes on offer can change while the tab is open.
        .task {
            while !Task.isCancelled {
                try? await Task.sleep(for: MainWindowModel.refreshInterval)
                await model.loadHosts()
            }
        }
        .alert(item: $model.notice) { notice in
            Alert(title: Text(notice.title), message: Text(notice.message))
        }
    }
}

/// Settings › Computers as a list and its detail: each computer with its state on the left, and the chosen one's
/// state, fixes and settings on the right. Work windows are the last item, since every computer can use them.
struct ComputerSettingsForm: View {
    enum Pane: Hashable {
        case computer(ComputerSettingsDraft.ComputerRow.ID)
        case workWindows
    }

    @Bindable var model: ComputerSettingsModel
    let facts: ComputerSettingsFacts
    let main: MainWindowModel
    @State private var selection: Pane?
    // The chosen computer's stored id and its place in the list, so rebuilding the rows keeps it chosen, even when
    // its address was edited. A save stores the list in the draft's order and numbers it afresh, so after a save the
    // place is what holds; after a revert or reload the stored ids are unchanged, so the id is.
    @State private var chosenID: String?
    @State private var chosenIndex: Int?

    var body: some View {
        VStack(spacing: 0) {
            HStack(spacing: 0) {
                List(selection: $selection) {
                    ForEach(computers.wrappedValue) { row in
                        ComputerListRow(name: row.computer.name, state: computer(row)?.state, needsYou: computer(row)?.needsYou ?? false)
                            .tag(Pane.computer(row.id))
                    }
                    Label(Words.workWindowsItem, systemImage: "calendar")
                        .tag(Pane.workWindows)
                }
                .listStyle(.sidebar)
                .safeAreaInset(edge: .bottom) {
                    Button(Words.addComputer, systemImage: "plus") {
                        model.draft?.addComputer(abilities: facts.abilities, mediaAccess: facts.mediaAccessOptions.first ?? "mounted")
                        if let added = model.draft?.computers.last { selection = .computer(added.id) }
                    }
                    .buttonStyle(.borderless)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .padding(10)
                }
                .frame(width: 220)
                Divider()
                detail
                    .frame(maxWidth: .infinity)
            }
            .disabled(model.saving)
            .onChange(of: model.draft) { model.edited() }
            Divider()
            HStack {
                if model.justSaved {
                    Text(Words.settingsSaved)
                        .foregroundStyle(.secondary)
                }
                Spacer()
                Button(Words.revertSettings) { model.revert() }
                    .disabled(!model.hasChanges || model.saving)
                Button(Words.saveSettings) { Task { await model.save() } }
                    .keyboardShortcut(.defaultAction)
                    .disabled(!model.hasChanges || model.saving)
            }
            .padding(16)
        }
        .frame(minHeight: 560)
        .onChange(of: computers.wrappedValue.map(\.id), initial: true) { keepSelection() }
        .onChange(of: selection) { _, chosen in
            if case .computer(let id) = chosen {
                chosenIndex = computers.wrappedValue.firstIndex { $0.id == id }
                chosenID = chosenIndex.map { computers.wrappedValue[$0].computer.id }
            }
        }
        // A notification or the menu bar asked for one computer.
        .onChange(of: main.requestedComputer, initial: true) { _, requested in
            guard let requested else { return }
            if let row = computers.wrappedValue.first(where: { savedAddress($0) == requested }) {
                selection = .computer(row.id)
            }
            main.requestedComputer = nil
        }
        .confirmationDialog(
            Words.removeComputerTitle(model.pendingComputerRemoval?.computer.name ?? ""),
            isPresented: Binding(
                get: { model.pendingComputerRemoval != nil },
                set: { if !$0 { model.pendingComputerRemoval = nil } }
            ),
            presenting: model.pendingComputerRemoval
        ) { row in
            Button(Words.removeFromMediaforce, role: .destructive) { model.confirmRemoval(row) }
            Button(Words.keepComputer, role: .cancel) { model.pendingComputerRemoval = nil }
        } message: { _ in
            Text(Words.removeComputerMessage)
        }
        .confirmationDialog(
            Words.removeWorkWindowTitle(model.pendingScheduleRemoval?.schedule.name ?? ""),
            isPresented: Binding(
                get: { model.pendingScheduleRemoval != nil },
                set: { if !$0 { model.pendingScheduleRemoval = nil } }
            ),
            presenting: model.pendingScheduleRemoval
        ) { row in
            Button(Words.removeWorkWindow, role: .destructive) { model.confirmRemoval(row) }
            Button(Words.keepWorkWindow, role: .cancel) { model.pendingScheduleRemoval = nil }
        } message: { row in
            Text(Words.removeWorkWindowMessage(usedBy: model.draft?.computersUsing(row.schedule) ?? 0))
        }
    }

    @ViewBuilder
    private var detail: some View {
        switch selection {
        case .computer(let id):
            if let row = computers.first(where: { $0.id == id }) {
                Form {
                    Section {
                        ComputerStatus(
                            name: row.wrappedValue.computer.name,
                            state: computer(row.wrappedValue)?.state,
                            needsYou: computer(row.wrappedValue)?.needsYou ?? false,
                            host: model.hosts[savedAddress(row.wrappedValue)]
                        ) { action, password in
                            await model.act(action, savedAddress(row.wrappedValue), password: password, main: main)
                        }
                        // A different computer starts with no password typed and no earlier result.
                        .id(row.wrappedValue.id)
                    }
                    Section(row.wrappedValue.computer.name.isEmpty ? Words.unnamedComputer : row.wrappedValue.computer.name) {
                        ComputerEditor(
                            computer: row.computer,
                            facts: facts,
                            schedules: model.draft?.schedules.map(\.schedule) ?? []
                        )
                        Button(Words.removeComputer, systemImage: "minus.circle", role: .destructive) {
                            model.pendingComputerRemoval = row.wrappedValue
                        }
                    }
                }
                .formStyle(.grouped)
            }
        case .workWindows:
            Form {
                ForEach(schedules) { $row in
                    Section(row.schedule.name.isEmpty ? Words.unnamedWorkWindow : row.schedule.name) {
                        WorkWindowEditor(schedule: $row.schedule, days: facts.days) {
                            model.pendingScheduleRemoval = row
                        }
                    }
                }
                Section {
                    if model.draft?.schedules.isEmpty ?? true {
                        Text(Words.noWorkWindows)
                            .foregroundStyle(.secondary)
                    }
                    Button(Words.addWorkWindow, systemImage: "plus") {
                        model.draft?.addSchedule(days: facts.days)
                    }
                } footer: {
                    Text(Words.workWindowHelp)
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }
            }
            .formStyle(.grouped)
        case nil:
            Text(Words.noComputers)
                .foregroundStyle(.secondary)
                .frame(maxWidth: .infinity, maxHeight: .infinity)
        }
    }

    /// The address the server knows this computer by: the saved one, while an edit to it is unsaved.
    private func savedAddress(_ row: ComputerSettingsDraft.ComputerRow) -> String {
        guard !row.computer.id.isEmpty else { return row.computer.address }
        return facts.computers.first { $0.id == row.computer.id }?.address ?? row.computer.address
    }

    /// The state the main window's facts give this computer, matched by the address the server knows.
    private func computer(_ row: ComputerSettingsDraft.ComputerRow) -> MainWindowFacts.Computer? {
        main.facts?.computers.first { $0.key == savedAddress(row) }
    }

    /// Keeps the chosen computer chosen when a load or save rebuilds the rows, else chooses the first.
    private func keepSelection() {
        let rows = computers.wrappedValue
        if case .computer(let id) = selection, rows.contains(where: { $0.id == id }) { return }
        if selection == .workWindows { return }
        let byPlace = chosenIndex.flatMap { rows.indices.contains($0) ? rows[$0] : nil }
        let byID = chosenID.flatMap { id in id.isEmpty ? nil : rows.first { $0.computer.id == id } }
        let kept = (model.justSaved ? byPlace ?? byID : byID ?? byPlace) ?? rows.first
        selection = kept.map { .computer($0.id) } ?? .workWindows
    }

    private var computers: Binding<[ComputerSettingsDraft.ComputerRow]> {
        Binding(get: { model.draft?.computers ?? [] }, set: { model.draft?.computers = $0 })
    }

    private var schedules: Binding<[ComputerSettingsDraft.ScheduleRow]> {
        Binding(get: { model.draft?.schedules ?? [] }, set: { model.draft?.schedules = $0 })
    }
}

/// A computer in the list: its name, and its state when the server has said.
struct ComputerListRow: View {
    let name: String
    let state: String?
    let needsYou: Bool

    var body: some View {
        HStack(spacing: 8) {
            WaitingMark()
                .opacity(needsYou ? 1 : 0)
                .accessibilityHidden(!needsYou)
            VStack(alignment: .leading, spacing: 1) {
                Text(name.isEmpty ? Words.unnamedComputer : name)
                Text(state.map(Words.computerState) ?? Words.computerNotHeardFrom)
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .lineLimit(1)
            }
        }
        .padding(.vertical, 3)
    }
}

struct ComputerEditor: View {
    @Binding var computer: ComputerSettingsFacts.Computer
    let facts: ComputerSettingsFacts
    let schedules: [ComputerSettingsFacts.Schedule]

    var body: some View {
        TextField(Words.computerName, text: $computer.name)
        SettingsRow(Words.computerAddress, help: Words.computerAddressHelp) {
            TextField(Words.computerAddress, text: $computer.address)
                .labelsHidden()
                .multilineTextAlignment(.trailing)
        }
        LabeledContent(Words.computerCanDo) {
            VStack(alignment: .leading) {
                ForEach(facts.abilities, id: \.self) { ability in
                    Toggle(Words.computerAbility(ability), isOn: abilityBinding(ability))
                        .disabled(computer.abilities == [ability])
                }
            }
        }
        Picker(Words.workWindow, selection: $computer.schedule) {
            ForEach(facts.builtInSchedules, id: \.self) { key in
                Text(Words.builtInWorkWindow(key)).tag(key)
            }
            ForEach(schedules, id: \.key) { schedule in
                Text(schedule.name.isEmpty ? Words.unnamedWorkWindow : schedule.name).tag(schedule.key)
            }
        }
        SettingsRow(Words.computerLibraries, help: Words.computerLibrariesHelp) {
            VStack(alignment: .leading) {
                ForEach(facts.libraries, id: \.key) { library in
                    Toggle(library.name, isOn: libraryBinding(library.key))
                        .disabled(libraryLocked(library.key))
                }
            }
        }
        SettingsRow(Words.jobsAtOnce) {
            CountStepper(title: Words.jobsAtOnce, value: $computer.atOnce, range: 1...16)
        }
        SettingsRow(Words.computerPriority, help: Words.computerPriorityHelp) {
            CountStepper(title: Words.computerPriority, value: $computer.priority, range: -100...100)
        }
        SettingsRow(Words.wakeAddress, help: Words.wakeAddressHelp) {
            TextField(Words.wakeAddress, text: $computer.wakeAddress, prompt: Text(Words.wakeAddressPrompt))
                .labelsHidden()
                .multilineTextAlignment(.trailing)
        }
        DisclosureGroup(Words.connectionDetails) {
            Picker(Words.mediaAccess, selection: $computer.mediaAccess) {
                ForEach(facts.mediaAccessOptions, id: \.self) { code in
                    Text(Words.mediaAccessChoice(code)).tag(code)
                }
            }
            MonospacedField(title: Words.mediaforceFolder, text: $computer.mediaforceFolder, prompt: Words.optionalField)
            MonospacedField(
                title: Words.computerWorkingFolder, text: $computer.workingFolder, prompt: Words.computerWorkingFolderPrompt
            )
            MonospacedField(
                title: Words.scratchFolder, help: Words.scratchFolderHelp, text: $computer.scratchFolder,
                prompt: Words.scratchFolderPrompt
            )
            ForEach(facts.libraries, id: \.key) { library in
                MonospacedField(
                    title: Words.libraryFolderOnComputer(library.name),
                    text: libraryFolderBinding(library.key),
                    prompt: Words.optionalField
                )
            }
            ForEach(removedLibraryFolders, id: \.library) { folder in
                LabeledContent(Words.removedLibraryFolder) {
                    HStack {
                        Text(folder.folder)
                            .font(.body.monospaced())
                        Button(Words.clearFolder) {
                            computer.libraryFolders.removeAll { $0.library == folder.library }
                        }
                    }
                }
            }
            MonospacedField(title: Words.startCommand, text: $computer.startCommand, prompt: Words.optionalField)
            MonospacedField(title: Words.stopCommand, text: $computer.stopCommand, prompt: Words.optionalField)
            SettingsRow(Words.startWait) {
                AmountField(title: Words.startWait, value: $computer.startWaitSeconds, format: .number, unit: Words.seconds)
            }
        }
    }

    private var removedLibraryFolders: [ComputerSettingsFacts.LibraryFolder] {
        let known = Set(facts.libraries.map(\.key))
        return computer.libraryFolders.filter { !known.contains($0.library) }
    }

    private func abilityBinding(_ ability: String) -> Binding<Bool> {
        Binding(get: { computer.abilities.contains(ability) }, set: { on in
            if on, !computer.abilities.contains(ability) {
                computer.abilities.append(ability)
            } else if !on, computer.abilities.count > 1 {
                computer.abilities.removeAll { $0 == ability }
            }
        })
    }

    private func libraryLocked(_ key: String) -> Bool {
        let all = facts.libraries.map(\.key)
        return ComputerSettingsDraft.togglingLibrary(key, in: computer.libraries, all: all) == computer.libraries
    }

    private func libraryBinding(_ key: String) -> Binding<Bool> {
        let all = facts.libraries.map(\.key)
        return Binding(get: { ComputerSettingsDraft.allows(computer, library: key) }, set: { _ in
            computer.libraries = ComputerSettingsDraft.togglingLibrary(key, in: computer.libraries, all: all)
        })
    }

    private func libraryFolderBinding(_ key: String) -> Binding<String> {
        Binding(get: { computer.libraryFolders.first { $0.library == key }?.folder ?? "" }, set: { folder in
            computer.libraryFolders.removeAll { $0.library == key }
            if !folder.isEmpty {
                computer.libraryFolders.append(.init(library: key, folder: folder))
            }
        })
    }
}

struct WorkWindowEditor: View {
    @Binding var schedule: ComputerSettingsFacts.Schedule
    let days: [String]
    let remove: () -> Void

    var body: some View {
        TextField(Words.workWindowName, text: $schedule.name)
        dayRow(Words.worksOn, allDay: false)
        dayRow(Words.allDayOn, allDay: true)
        Picker(Words.startsAt, selection: $schedule.startHour) {
            ForEach(0..<24, id: \.self) { hour in Text(Words.hour(hour)).tag(hour) }
        }
        Picker(Words.endsAt, selection: $schedule.endHour) {
            ForEach(0..<24, id: \.self) { hour in Text(Words.hour(hour)).tag(hour) }
        }
        Toggle(isOn: Binding(
            get: { schedule.finishCurrentFile == true },
            set: { schedule.finishCurrentFile = $0 }
        )) {
            Text(Words.finishCurrentFile)
            Text(Words.finishCurrentFileHelp)
        }
        Button(Words.removeWorkWindow, systemImage: "minus.circle", role: .destructive, action: remove)
    }

    private func dayRow(_ title: String, allDay: Bool) -> some View {
        SettingsRow(title) {
            HStack(spacing: 4) {
                ForEach(days, id: \.self) { day in
                    Toggle(Words.day(day), isOn: Binding(
                        get: { (allDay ? schedule.allDayDays : schedule.days).contains(day) },
                        set: { _ in
                            schedule = ComputerSettingsDraft.togglingDay(day, allDay: allDay, in: schedule, order: days)
                        }
                    ))
                    .toggleStyle(.button)
                }
            }
        }
    }
}

@MainActor
@Observable
final class MetadataSettingsModel {
    let client: MediaforceClient
    private(set) var saved: MetadataSettingsFacts?
    private(set) var loadFailed = false
    private(set) var saving = false
    private(set) var justSaved = false
    var draft: MetadataSettingsDraft?
    var notice: ActionNotice?
    private(set) var connections: MetadataConnections?
    private(set) var checking = false
    private(set) var savingKey = false

    init(client: MediaforceClient) {
        self.client = client
    }

    var hasChanges: Bool {
        guard let draft, let saved else { return false }
        return draft.saveBody != MetadataSettingsDraft(saved).saveBody
    }

    func keyState(_ service: String) -> ServiceKeyState {
        let keySet = service == "plex" ? saved?.plex.keySet : saved?.tmdb.keySet
        let checked = service == "plex" ? connections?.plex : connections?.tmdb
        return ServiceKeyState(keySet: keySet ?? false, checking: checking, checked: checked)
    }

    /// Asks the server to reach Plex and TMDB with the saved keys; until it answers, a set key reads as saved.
    func checkConnections() async {
        checking = true
        defer { checking = false }
        connections = try? await client.metadataConnections()
    }

    /// Sends a key once for the server to keep in the Keychain. Unsaved edits to the rest of the tab stay as typed.
    func saveKey(_ key: String, for service: String) async -> Bool {
        guard !savingKey else { return false }
        savingKey = true
        defer { savingKey = false }
        do {
            let result = try await client.saveServiceKey(.init(service: service, key: key))
            guard result.ok else {
                notice = .serviceKeyNotSaved(result.code)
                return false
            }
            if let settings = result.settings { saved = settings }
        } catch {
            notice = .unanswered
            return false
        }
        await checkConnections()
        return true
    }

    func load() async {
        do {
            let facts = try await client.metadataSettings()
            show(facts)
        } catch {
            loadFailed = true
        }
    }

    func revert() {
        if let saved { show(saved) }
    }

    func save() async {
        guard let draft, !saving else { return }
        saving = true
        defer { saving = false }
        do {
            let result = try await client.saveMetadataSettings(draft.saveBody)
            if result.ok {
                if let settings = result.settings { show(settings) } else { await load() }
                justSaved = true
                await checkConnections()
            } else if result.code == "settings_changed" {
                notice = .settingsChangedElsewhere
                await load()
            } else {
                notice = .metadataSettingsNotSaved(result.code)
            }
        } catch {
            notice = .unanswered
        }
    }

    func edited() {
        if hasChanges { justSaved = false }
    }

    private func show(_ facts: MetadataSettingsFacts) {
        saved = facts
        draft = MetadataSettingsDraft(facts)
        loadFailed = false
    }
}

struct MetadataSettingsTab: View {
    let main: MainWindowModel
    @State private var model: MetadataSettingsModel

    init(main: MainWindowModel) {
        self.main = main
        _model = State(initialValue: MetadataSettingsModel(client: main.client))
    }

    var body: some View {
        Group {
            if model.saved != nil, model.draft != nil {
                MetadataSettingsForm(model: model)
            } else if model.loadFailed {
                SettingsLoadFailure(main: main) {
                    Task { await model.load() }
                }
            } else {
                ProgressView()
                    .frame(maxWidth: .infinity, minHeight: 240)
            }
        }
        .task {
            await model.load()
            await model.checkConnections()
        }
        .alert(item: $model.notice) { notice in
            Alert(title: Text(notice.title), message: Text(notice.message))
        }
    }
}

struct MetadataSettingsForm: View {
    @Bindable var model: MetadataSettingsModel

    var body: some View {
        VStack(spacing: 0) {
            Form {
                Section(Words.plexSection) {
                    Toggle(isOn: plexEnabled) {
                        Text(Words.usePlex)
                        Text(Words.usePlexHelp)
                    }
                    MonospacedField(title: Words.plexAddress, text: plexAddress, prompt: Words.plexAddressPrompt)
                    ServiceKeyRows(name: Words.plexSection, state: model.keyState("plex"), saving: model.savingKey) {
                        await model.saveKey($0, for: "plex")
                    }
                }
                Section {
                    Toggle(isOn: tmdbEnabled) {
                        Text(Words.useTMDB)
                        Text(Words.useTMDBHelp)
                    }
                    ServiceKeyRows(name: Words.tmdbSection, state: model.keyState("tmdb"), saving: model.savingKey) {
                        await model.saveKey($0, for: "tmdb")
                    }
                } header: {
                    Text(Words.tmdbSection)
                } footer: {
                    Text(Words.metadataHelp)
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }
            }
            .formStyle(.grouped)
            .disabled(model.saving)
            .onChange(of: model.draft) { model.edited() }
            HStack {
                if model.justSaved {
                    Text(Words.settingsSaved)
                        .foregroundStyle(.secondary)
                }
                Spacer()
                Button(Words.revertSettings) { model.revert() }
                    .disabled(!model.hasChanges || model.saving)
                Button(Words.saveSettings) { Task { await model.save() } }
                    .keyboardShortcut(.defaultAction)
                    .disabled(!model.hasChanges || model.saving)
            }
            .padding(16)
        }
        .frame(minHeight: 460)
    }

    private var plexEnabled: Binding<Bool> {
        Binding(get: { model.draft?.plex.enabled ?? false }, set: { model.draft?.plex.enabled = $0 })
    }

    private var plexAddress: Binding<String> {
        Binding(get: { model.draft?.plex.address ?? "" }, set: { model.draft?.plex.address = $0 })
    }

    private var tmdbEnabled: Binding<Bool> {
        Binding(get: { model.draft?.tmdb.enabled ?? false }, set: { model.draft?.tmdb.enabled = $0 })
    }
}

@MainActor
@Observable
final class CompressionSettingsModel {
    let client: MediaforceClient
    private(set) var saved: CompressionSettingsFacts?
    private(set) var loadFailed = false
    private(set) var saving = false
    private(set) var justSaved = false
    var draft: CompressionSettingsDraft?
    var notice: ActionNotice?

    init(client: MediaforceClient) {
        self.client = client
    }

    var hasChanges: Bool {
        guard let draft, let saved else { return false }
        return draft.saveBody != CompressionSettingsDraft(saved).saveBody
    }

    func load() async {
        do {
            let facts = try await client.compressionSettings()
            show(facts)
        } catch {
            loadFailed = true
        }
    }

    func revert() {
        if let saved { show(saved) }
    }

    func save() async {
        guard let draft, !saving else { return }
        saving = true
        defer { saving = false }
        do {
            let result = try await client.saveCompressionSettings(draft.saveBody)
            if result.ok {
                if let settings = result.settings { show(settings) } else { await load() }
                justSaved = true
            } else if result.code == "settings_changed" {
                notice = .settingsChangedElsewhere
                await load()
            } else {
                notice = .compressionSettingsNotSaved(result.code)
            }
        } catch {
            notice = .unanswered
        }
    }

    func edited() {
        if hasChanges { justSaved = false }
    }

    private func show(_ facts: CompressionSettingsFacts) {
        saved = facts
        draft = CompressionSettingsDraft(facts)
        loadFailed = false
    }
}

struct CompressionSettingsTab: View {
    let main: MainWindowModel
    @State private var model: CompressionSettingsModel

    init(main: MainWindowModel) {
        self.main = main
        _model = State(initialValue: CompressionSettingsModel(client: main.client))
    }

    var body: some View {
        Group {
            if let saved = model.saved, model.draft != nil {
                CompressionSettingsForm(model: model, facts: saved)
            } else if model.loadFailed {
                SettingsLoadFailure(main: main) {
                    Task { await model.load() }
                }
            } else {
                ProgressView()
                    .frame(maxWidth: .infinity, minHeight: 240)
            }
        }
        .task { await model.load() }
        .alert(item: $model.notice) { notice in
            Alert(title: Text(notice.title), message: Text(notice.message))
        }
    }
}

/// Plain settings first; how picture quality is measured stays under Advanced, as on the website.
struct CompressionSettingsForm: View {
    @Bindable var model: CompressionSettingsModel
    let facts: CompressionSettingsFacts
    @State private var showsAdvanced = false

    var body: some View {
        VStack(spacing: 0) {
            if let draft = Binding($model.draft) {
                fields(draft.saveBody)
                    .formStyle(.grouped)
                    .disabled(model.saving)
            }
            HStack {
                if model.justSaved {
                    Text(Words.settingsSaved)
                        .foregroundStyle(.secondary)
                }
                Spacer()
                Button(Words.revertSettings) { model.revert() }
                    .disabled(!model.hasChanges || model.saving)
                Button(Words.saveSettings) { Task { await model.save() } }
                    .keyboardShortcut(.defaultAction)
                    .disabled(!model.hasChanges || model.saving)
            }
            .padding(16)
        }
        .onChange(of: model.draft) { model.edited() }
        .frame(minHeight: 460)
    }

    private func fields(_ values: Binding<CompressionSettingsSave>) -> some View {
        Form {
            Section(Words.aimSection) {
                Picker(Words.aimPicker, selection: values.aim) {
                    ForEach(choices(facts.aims, current: values.wrappedValue.aim), id: \.self) { code in
                        Text(Words.compressionAim(code)).tag(code)
                    }
                }
            }
            Section(Words.sizeSection) {
                SettingsRow(Words.sizeGoal, help: Words.sizeGoalScaling(facts.sizeGoalMode)) {
                    HStack(spacing: 6) {
                        AmountField(title: Words.sizeGoal, value: values.sizeGoalMb, format: .number, unit: Words.megabytes)
                        Text(Words.forAnEpisodeOf)
                        AmountField(title: Words.minutes, value: values.sizeGoalMinutes, format: .number, unit: Words.minutes)
                    }
                }
                numberRow(
                    Words.largestSize, help: Words.largestSizeHelp, value: values.largestPercent, unit: Words.percentOfOriginal
                )
            }
            Section(Words.pictureSection) {
                SettingsRow(
                    Words.tallestPicture,
                    help: "\(Words.tallestPictureSummary(values.wrappedValue.tallestPicture)). \(Words.tallestPictureHelp)"
                ) {
                    AmountField(title: Words.tallestPicture, value: values.tallestPicture, format: .number, unit: Words.pixelsTall)
                }
                SettingsRow(Words.grain, help: Words.grainHelp) {
                    AmountField(title: Words.grain, value: values.grain, format: .number)
                }
            }
            Section {
                DisclosureGroup(Words.advancedCompression, isExpanded: $showsAdvanced) {
                    Picker(selection: values.measurement) {
                        ForEach(choices(facts.measurements, current: values.wrappedValue.measurement), id: \.self) { code in
                            Text(Words.qualityMeasurement(code)).tag(code)
                        }
                    } label: {
                        Text(Words.measurementPicker)
                        Text(Words.scoresHelp)
                    }
                    numberRow(Words.appearanceTarget, value: values.appearanceTarget)
                    numberRow(Words.appearanceMinimum, value: values.appearanceMinimum)
                    numberRow(Words.detailTarget, value: values.detailTarget)
                    numberRow(Words.detailMinimum, value: values.detailMinimum)
                    Picker(Words.samplePicker, selection: values.sampleMethod) {
                        ForEach(choices(facts.sampleMethods, current: values.wrappedValue.sampleMethod), id: \.self) { code in
                            Text(Words.sampleMethod(code)).tag(code)
                        }
                    }
                }
            }
        }
    }

    private func numberRow(_ title: String, help: String? = nil, value: Binding<Double>, unit: String? = nil) -> some View {
        SettingsRow(title, help: help) {
            AmountField(title: title, value: value, format: .number, unit: unit)
        }
    }

    /// A saved choice the server no longer offers stays shown, so the picker never appears empty.
    private func choices(_ offered: [String], current: String) -> [String] {
        offered.contains(current) ? offered : offered + [current]
    }
}

extension ServiceKeyState {
    var symbol: String {
        switch self {
        case .connected: "checkmark.circle"
        case .notSet, .rejected, .unreachable: "exclamationmark.triangle"
        case .checking, .saved: "key"
        }
    }

    var color: AnyShapeStyle {
        switch self {
        case .connected: AnyShapeStyle(Color.green)
        case .notSet, .rejected, .unreachable: AnyShapeStyle(Color.orange)
        case .checking, .saved: AnyShapeStyle(.secondary)
        }
    }
}

/// A service's access key: whether it is set and accepted, and one secure field to enter or replace it. The key
/// goes to the server once and is never shown again.
struct ServiceKeyRows: View {
    let name: String
    let state: ServiceKeyState
    let saving: Bool
    let save: (String) async -> Bool
    @State private var key = ""

    var body: some View {
        LabeledContent(Words.accessKey) {
            Label(Words.serviceKeyState(state, name: name), systemImage: state.symbol)
                .foregroundStyle(state.color)
        }
        SettingsRow(state == .notSet ? Words.enterKey : Words.replaceKey, help: Words.keyKeptInKeychain) {
            HStack {
                SecureField(Words.accessKey, text: $key, prompt: Text(Words.pasteKeyPrompt))
                    .labelsHidden()
                    .textFieldStyle(.roundedBorder)
                    .frame(width: 220)
                    .onSubmit(submit)
                Button(Words.saveKey, action: submit)
                    .disabled(trimmedKey.isEmpty || saving)
            }
        }
    }

    private var trimmedKey: String {
        key.trimmingCharacters(in: .whitespacesAndNewlines)
    }

    private func submit() {
        let sent = trimmedKey
        guard !sent.isEmpty, !saving else { return }
        Task {
            if await save(sent) { key = "" }
        }
    }
}

/// What a Settings tab shows when its settings didn't load. When Mediaforce itself isn't answering, it is the main
/// window's card, from the main window's own checks; when Mediaforce answers but this tab's settings didn't load,
/// it says so with Try Again. Either way the tab loads again by itself once Mediaforce answers.
struct SettingsLoadFailure: View {
    let main: MainWindowModel
    let retry: () -> Void

    var body: some View {
        Group {
            if main.connection == .reachable {
                VStack(spacing: 12) {
                    Text(Words.settingsDidNotLoad)
                        .foregroundStyle(.secondary)
                        .multilineTextAlignment(.center)
                    Button(Words.tryAgain, action: retry)
                }
                .padding(40)
                .frame(maxWidth: .infinity, minHeight: 240)
            } else {
                ServerStatusView(state: main.connection, lastHeard: main.lastHeard, problem: main.problem) {
                    Task { await main.refresh() }
                }
                .frame(minHeight: 420)
            }
        }
        // The tab's load just failed; check Mediaforce now rather than at the next 30-second check.
        .task { await main.refresh() }
        .onChange(of: main.connection) { _, now in
            if now == .reachable { retry() }
        }
    }
}
