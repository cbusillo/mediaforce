import AppKit
import SwiftUI

struct SettingsWindow: View {
    let client: MediaforceClient

    var body: some View {
        TabView {
            LibrarySettingsTab(client: client)
                .tabItem { Label(Words.librariesTab, systemImage: "books.vertical") }
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
