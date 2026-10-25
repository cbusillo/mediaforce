import AppKit
import SwiftUI

struct SettingsWindow: View {
    let client: MediaforceClient

    var body: some View {
        TabView {
            LibrarySettingsTab(client: client)
                .tabItem { Label(Words.librariesTab, systemImage: "books.vertical") }
            CompressionSettingsTab(client: client)
                .tabItem { Label(Words.compressionTab, systemImage: "arrow.down.right.and.arrow.up.left") }
            ComputerSettingsTab(client: client)
                .tabItem { Label(Words.computersTab, systemImage: "desktopcomputer") }
            MetadataSettingsTab(client: client)
                .tabItem { Label(Words.metadataTab, systemImage: "network") }
        }
        .frame(width: 760)
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
    @State private var model: LibrarySettingsModel

    init(client: MediaforceClient) {
        _model = State(initialValue: LibrarySettingsModel(client: client))
    }

    var body: some View {
        Group {
            if let saved = model.saved, model.draft != nil {
                LibrarySettingsForm(model: model, kinds: saved.types)
            } else if model.loadFailed {
                ServerStatusView(state: .unreachable) {
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
                Section(Words.librariesSection) {
                    if model.draft?.rows.isEmpty ?? true {
                        Text(Words.noLibraries)
                            .foregroundStyle(.secondary)
                    }
                    ForEach(rows) { $row in
                        LibraryRowEditor(row: $row, kinds: kinds, model: model)
                    }
                    Button(Words.addLibrary, systemImage: "plus") {
                        model.draft?.addLibrary(type: kinds.last ?? "other")
                    }
                }
                Section(Words.workingSpaceSection) {
                    FolderField(title: Words.workingFolder, path: workingFolder)
                    Text(Words.workingFolderHelp)
                        .font(.caption)
                        .foregroundStyle(.secondary)
                    HStack {
                        TextField(Words.lookForNewFiles, value: refreshHours, format: .number)
                            .multilineTextAlignment(.trailing)
                            .frame(maxWidth: 260)
                        Text(Words.hours)
                    }
                    Text(Words.lookForNewFilesHelp)
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
        VStack(alignment: .leading, spacing: 8) {
            HStack {
                TextField(Words.libraryName, text: $row.library.name)
                Picker(Words.libraryType, selection: kind) {
                    ForEach(kinds, id: \.self) { code in
                        Text(Words.libraryKind(code)).tag(code)
                    }
                }
                .frame(maxWidth: 220)
                Button(Words.removeLibrary, systemImage: "minus.circle") { model.pendingRemoval = row }
                    .labelStyle(.iconOnly)
                    .buttonStyle(.borderless)
                    .help(Words.removeLibrary)
            }
            FolderField(title: Words.libraryFolder, path: $row.library.folder)
            DisclosureGroup(Words.plexPathSummary(row.library.plexPath)) {
                TextField(Words.plexPath, text: $row.library.plexPath)
                    .font(.body.monospaced())
                Text(Words.plexPathHelp)
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
        }
        .padding(.vertical, 4)
    }

    private var kind: Binding<String> {
        let id = row.id
        return Binding(get: { row.library.type }, set: { chosen in
            Task { await model.chooseKind(chosen, for: id) }
        })
    }
}

struct FolderField: View {
    let title: String
    @Binding var path: String

    var body: some View {
        HStack {
            TextField(title, text: $path)
                .font(.body.monospaced())
            Button(Words.chooseFolder) { choose() }
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

    init(client: MediaforceClient) {
        self.client = client
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
                if let settings = result.settings { show(settings) } else { await load() }
                justSaved = true
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
    @State private var model: ComputerSettingsModel

    init(client: MediaforceClient) {
        _model = State(initialValue: ComputerSettingsModel(client: client))
    }

    var body: some View {
        Group {
            if let saved = model.saved, model.draft != nil {
                ComputerSettingsForm(model: model, facts: saved)
            } else if model.loadFailed {
                ServerStatusView(state: .unreachable) {
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

struct ComputerSettingsForm: View {
    @Bindable var model: ComputerSettingsModel
    let facts: ComputerSettingsFacts

    var body: some View {
        VStack(spacing: 0) {
            Form {
                if model.draft?.computers.isEmpty ?? true {
                    Section(Words.computersSection) {
                        Text(Words.noComputers)
                            .foregroundStyle(.secondary)
                    }
                }
                ForEach(computers) { $row in
                    Section(row.computer.name.isEmpty ? Words.unnamedComputer : row.computer.name) {
                        ComputerEditor(
                            computer: $row.computer,
                            facts: facts,
                            schedules: model.draft?.schedules.map(\.schedule) ?? []
                        )
                        Button(Words.removeComputer, systemImage: "minus.circle", role: .destructive) {
                            model.pendingComputerRemoval = row
                        }
                    }
                }
                Section {
                    Button(Words.addComputer, systemImage: "plus") {
                        model.draft?.addComputer(abilities: facts.abilities, mediaAccess: facts.mediaAccessOptions.first ?? "mounted")
                    }
                }
                Section(Words.workWindowsSection) {
                    if model.draft?.schedules.isEmpty ?? true {
                        Text(Words.noWorkWindows)
                            .foregroundStyle(.secondary)
                    }
                    ForEach(schedules) { $row in
                        WorkWindowEditor(schedule: $row.schedule, days: facts.days) {
                            model.pendingScheduleRemoval = row
                        }
                    }
                    Button(Words.addWorkWindow, systemImage: "plus") {
                        model.draft?.addSchedule(days: facts.days)
                    }
                    Text(Words.workWindowHelp)
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
        .frame(minHeight: 560)
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

    private var computers: Binding<[ComputerSettingsDraft.ComputerRow]> {
        Binding(get: { model.draft?.computers ?? [] }, set: { model.draft?.computers = $0 })
    }

    private var schedules: Binding<[ComputerSettingsDraft.ScheduleRow]> {
        Binding(get: { model.draft?.schedules ?? [] }, set: { model.draft?.schedules = $0 })
    }
}

struct ComputerEditor: View {
    @Binding var computer: ComputerSettingsFacts.Computer
    let facts: ComputerSettingsFacts
    let schedules: [ComputerSettingsFacts.Schedule]

    var body: some View {
        TextField(Words.computerName, text: $computer.name)
        VStack(alignment: .leading, spacing: 4) {
            TextField(Words.computerAddress, text: $computer.address)
                .font(.body.monospaced())
            Text(Words.computerAddressHelp)
                .font(.caption)
                .foregroundStyle(.secondary)
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
        LabeledContent(Words.computerLibraries) {
            VStack(alignment: .leading) {
                ForEach(facts.libraries, id: \.key) { library in
                    Toggle(library.name, isOn: libraryBinding(library.key))
                        .disabled(libraryLocked(library.key))
                }
                Text(Words.computerLibrariesHelp)
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
        }
        Stepper(Words.atOnce(computer.atOnce), value: $computer.atOnce, in: 1...16)
        VStack(alignment: .leading, spacing: 4) {
            Stepper("\(Words.computerPriority): \(computer.priority.formatted())", value: $computer.priority, in: -100...100)
            Text(Words.computerPriorityHelp)
                .font(.caption)
                .foregroundStyle(.secondary)
        }
        VStack(alignment: .leading, spacing: 4) {
            TextField(Words.wakeAddress, text: $computer.wakeAddress, prompt: Text(Words.wakeAddressPrompt))
                .font(.body.monospaced())
            Text(Words.wakeAddressHelp)
                .font(.caption)
                .foregroundStyle(.secondary)
        }
        DisclosureGroup(Words.connectionDetails) {
            Picker(Words.mediaAccess, selection: $computer.mediaAccess) {
                ForEach(facts.mediaAccessOptions, id: \.self) { code in
                    Text(Words.mediaAccessChoice(code)).tag(code)
                }
            }
            TextField(Words.mediaforceFolder, text: $computer.mediaforceFolder, prompt: Text(Words.optionalField))
                .font(.body.monospaced())
            TextField(Words.computerWorkingFolder, text: $computer.workingFolder, prompt: Text(Words.computerWorkingFolderPrompt))
                .font(.body.monospaced())
            VStack(alignment: .leading, spacing: 4) {
                TextField(Words.scratchFolder, text: $computer.scratchFolder, prompt: Text(Words.scratchFolderPrompt))
                    .font(.body.monospaced())
                Text(Words.scratchFolderHelp)
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
            ForEach(facts.libraries, id: \.key) { library in
                TextField(
                    Words.libraryFolderOnComputer(library.name),
                    text: libraryFolderBinding(library.key),
                    prompt: Text(Words.optionalField)
                )
                .font(.body.monospaced())
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
            TextField(Words.startCommand, text: $computer.startCommand, prompt: Text(Words.optionalField))
                .font(.body.monospaced())
            TextField(Words.stopCommand, text: $computer.stopCommand, prompt: Text(Words.optionalField))
                .font(.body.monospaced())
            HStack {
                TextField(Words.startWait, value: $computer.startWaitSeconds, format: .number)
                    .multilineTextAlignment(.trailing)
                    .frame(maxWidth: 260)
                Text(Words.seconds)
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
        VStack(alignment: .leading, spacing: 8) {
            HStack {
                TextField(Words.workWindowName, text: $schedule.name)
                Button(Words.removeWorkWindow, systemImage: "minus.circle", action: remove)
                    .labelStyle(.iconOnly)
                    .buttonStyle(.borderless)
                    .help(Words.removeWorkWindow)
            }
            dayRow(Words.worksOn, allDay: false)
            dayRow(Words.allDayOn, allDay: true)
            HStack {
                Picker(Words.startsAt, selection: $schedule.startHour) {
                    ForEach(0..<24, id: \.self) { hour in Text(Words.hour(hour)).tag(hour) }
                }
                Picker(Words.endsAt, selection: $schedule.endHour) {
                    ForEach(0..<24, id: \.self) { hour in Text(Words.hour(hour)).tag(hour) }
                }
            }
        }
        .padding(.vertical, 4)
    }

    private func dayRow(_ title: String, allDay: Bool) -> some View {
        HStack {
            Text(title)
                .frame(width: 60, alignment: .leading)
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

    init(client: MediaforceClient) {
        self.client = client
    }

    var hasChanges: Bool {
        guard let draft, let saved else { return false }
        return draft.saveBody != MetadataSettingsDraft(saved).saveBody
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
    @State private var model: MetadataSettingsModel

    init(client: MediaforceClient) {
        _model = State(initialValue: MetadataSettingsModel(client: client))
    }

    var body: some View {
        Group {
            if let saved = model.saved, model.draft != nil {
                MetadataSettingsForm(model: model, facts: saved)
            } else if model.loadFailed {
                ServerStatusView(state: .unreachable) {
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

struct MetadataSettingsForm: View {
    @Bindable var model: MetadataSettingsModel
    let facts: MetadataSettingsFacts

    var body: some View {
        VStack(spacing: 0) {
            Form {
                Section(Words.plexSection) {
                    VStack(alignment: .leading, spacing: 4) {
                        Toggle(Words.usePlex, isOn: plexEnabled)
                        Text(Words.usePlexHelp)
                            .font(.caption)
                            .foregroundStyle(.secondary)
                    }
                    TextField(Words.plexAddress, text: plexAddress, prompt: Text(Words.plexAddressPrompt))
                        .font(.body.monospaced())
                    AccessKeyStatus(name: facts.plex.keyName, found: facts.plex.keyFound)
                }
                Section(Words.tmdbSection) {
                    VStack(alignment: .leading, spacing: 4) {
                        Toggle(Words.useTMDB, isOn: tmdbEnabled)
                        Text(Words.useTMDBHelp)
                            .font(.caption)
                            .foregroundStyle(.secondary)
                    }
                    AccessKeyStatus(name: facts.tmdb.keyName, found: facts.tmdb.keyFound)
                }
                Section {
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
    @State private var model: CompressionSettingsModel

    init(client: MediaforceClient) {
        _model = State(initialValue: CompressionSettingsModel(client: client))
    }

    var body: some View {
        Group {
            if let saved = model.saved, model.draft != nil {
                CompressionSettingsForm(model: model, facts: saved)
            } else if model.loadFailed {
                ServerStatusView(state: .unreachable) {
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
                HStack {
                    TextField(Words.sizeGoal, value: values.sizeGoalMb, format: .number)
                        .multilineTextAlignment(.trailing)
                        .frame(maxWidth: 220)
                    Text(Words.megabytes)
                    Text(Words.forAnEpisodeOf)
                    TextField(Words.minutes, value: values.sizeGoalMinutes, format: .number)
                        .labelsHidden()
                        .multilineTextAlignment(.trailing)
                        .frame(maxWidth: 60)
                    Text(Words.minutes)
                }
                caption(Words.sizeGoalScaling(facts.sizeGoalMode))
                numberRow(Words.sizeLimit, value: values.sizeLimitPercent, unit: Words.percent)
                caption(Words.sizeLimitHelp)
                numberRow(Words.largestSize, value: values.largestPercent, unit: Words.percentOfOriginal)
                caption(Words.largestSizeHelp)
            }
            Section(Words.pictureSection) {
                HStack {
                    TextField(Words.tallestPicture, value: values.tallestPicture, format: .number)
                        .multilineTextAlignment(.trailing)
                        .frame(maxWidth: 260)
                    Text(Words.pixelsTall)
                }
                caption("\(Words.tallestPictureSummary(values.wrappedValue.tallestPicture)). \(Words.tallestPictureHelp)")
                TextField(Words.grain, value: values.grain, format: .number)
                    .multilineTextAlignment(.trailing)
                    .frame(maxWidth: 260)
                caption(Words.grainHelp)
            }
            Section {
                DisclosureGroup(Words.advancedCompression, isExpanded: $showsAdvanced) {
                    Picker(Words.measurementPicker, selection: values.measurement) {
                        ForEach(choices(facts.measurements, current: values.wrappedValue.measurement), id: \.self) { code in
                            Text(Words.qualityMeasurement(code)).tag(code)
                        }
                    }
                    numberRow(Words.appearanceTarget, value: values.appearanceTarget, unit: nil)
                    numberRow(Words.appearanceMinimum, value: values.appearanceMinimum, unit: nil)
                    numberRow(Words.detailTarget, value: values.detailTarget, unit: nil)
                    numberRow(Words.detailMinimum, value: values.detailMinimum, unit: nil)
                    caption(Words.scoresHelp)
                    Picker(Words.samplePicker, selection: values.sampleMethod) {
                        ForEach(choices(facts.sampleMethods, current: values.wrappedValue.sampleMethod), id: \.self) { code in
                            Text(Words.sampleMethod(code)).tag(code)
                        }
                    }
                }
            }
        }
    }

    private func numberRow(_ title: String, value: Binding<Double>, unit: String?) -> some View {
        HStack {
            TextField(title, value: value, format: .number)
                .multilineTextAlignment(.trailing)
                .frame(maxWidth: 260)
            if let unit { Text(unit) }
        }
    }

    private func caption(_ text: String) -> some View {
        Text(text)
            .font(.caption)
            .foregroundStyle(.secondary)
    }

    /// A saved choice the server no longer offers stays shown, so the picker never appears empty.
    private func choices(_ offered: [String], current: String) -> [String] {
        offered.contains(current) ? offered : offered + [current]
    }
}

/// Says only whether the key is set and where it is read from; the key itself never reaches the app.
struct AccessKeyStatus: View {
    let name: String
    let found: Bool

    var body: some View {
        LabeledContent(Words.accessKey) {
            if found {
                Label(Words.accessKeyFound, systemImage: "checkmark.circle")
                    .foregroundStyle(.green)
            } else {
                Text(Words.accessKeyNeeded(name))
                    .foregroundStyle(.orange)
            }
        }
        DisclosureGroup(Words.accessKeyDetails) {
            LabeledContent(Words.accessKeyReadFrom) {
                Text(name)
                    .font(.body.monospaced())
                    .textSelection(.enabled)
            }
            Text(Words.accessKeyHelp)
                .font(.caption)
                .foregroundStyle(.secondary)
        }
    }
}
