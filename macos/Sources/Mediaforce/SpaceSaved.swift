import SwiftUI

struct SpaceSavedPage: View {
    let facts: MainWindowFacts
    let model: MainWindowModel
    let revealBackups: Bool
    let openItem: (String) -> Void
    @State private var highlighted = false
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @State private var listing = MediaListing()
    @State private var machines = ComputersStore()
    @State private var cleanup: CleanupFolder?
    @State private var shows: [SpaceSavedFacts.Show]?
    @State private var refreshFailed = false
    @State private var lastHeard: Date?
    // A delete was asked for and Mediaforce hasn't answered yet.
    @State private var asking = false
    static let deletionCheckInterval: Duration = .seconds(1)

    var body: some View {
        GeometryReader { viewport in
            ScrollViewReader { scroll in
                PageFrame(title: Words.spaceSaved) {
                    if refreshFailed {
                        StaleNotice(connection: .failing, lastHeard: lastHeard)
                            .padding(.horizontal, -34)
                    }
                    SavingsSummary(facts: facts, listing: listing, machines: machines, posters: model.posters,
                                   backupBytes: backupFolder.totalSizeBytes)
                    VStack(alignment: .leading, spacing: 18) {
                        DeleteBackups(status: BackupsStatus(cleanup: backupFolder, asking: asking), model: model,
                                      readNotice: cleanupReadNotice) {
                            Task { await deleteBackups() }
                        }
                        .padding(8)
                        .background(highlighted ? Color.accentColor.opacity(0.16) : .clear, in: RoundedRectangle(cornerRadius: 8))
                        .padding(.horizontal, -8)
                        .id(Place.originalBackups)
                        if let shows, !shows.isEmpty {
                            CompareEpisodes(shows: shows, model: model, openItem: openItem)
                                .padding(.top, 8)
                        } else if shows == nil && !refreshFailed {
                            ProgressView().frame(maxWidth: 760, minHeight: 200)
                        }
                    }
                    .frame(minHeight: revealBackups ? viewport.size.height : nil, alignment: .topLeading)
                }
                .task(id: revealBackups) {
                    guard revealBackups else { highlighted = false; return }
                    scroll.scrollTo(Place.originalBackups, anchor: .top)
                    highlighted = true
                    do { try await Task.sleep(for: .seconds(2)) } catch { return }
                    withAnimation(reduceMotion ? nil : .easeOut) { highlighted = false }
                }
            }
        }
        .task { await listing.watch(model.client, kind: .everything) }
        .task { await machines.watch(model.client) }
        .task(id: facts.originalBackups.count) { await watch() }
        .task(id: deleting) { await followDeletion() }
    }

    private var backupFolder: CleanupFolder {
        cleanup ?? CleanupFolder(fileCount: facts.originalBackups.count, totalSizeBytes: facts.originalBackups.bytes)
    }

    private var cleanupReadNotice: String? {
        guard cleanup == nil else { return nil }
        return refreshFailed ? "Couldn’t read the Cleanup folder. Retrying…" : "Reading the Cleanup folder…"
    }

    private var deleting: Bool {
        cleanup.map { BackupsStatus(cleanup: $0, asking: asking).deleting } ?? asking
    }

    /// Asks Mediaforce to delete, then shows what it did; the page reads the deletion's progress meanwhile.
    private func deleteBackups() async {
        asking = true
        if let after = await model.deleteBackups() { cleanup = after }
        asking = false
        if let completed = try? await model.client.completed() { shows = SpaceSavedFacts.shows(completed.folders) }
    }

    private func followDeletion() async {
        guard deleting else { return }
        while !Task.isCancelled {
            if let found = try? await model.client.cleanupFolder() { cleanup = found }
            try? await Task.sleep(for: Self.deletionCheckInterval)
        }
    }

    private func watch() async {
        while !Task.isCancelled {
            do {
                cleanup = try await model.client.cleanupFolder()
                shows = SpaceSavedFacts.shows(try await model.client.completed().folders)
                refreshFailed = false
                lastHeard = .now
            } catch {
                refreshFailed = true
            }
            try? await Task.sleep(for: MainWindowModel.refreshInterval)
        }
    }
}

enum SpaceSavedFacts {
    struct Folder: Decodable, Sendable {
        let prefix: String
        let subtitle: String
        let archivedBackupCount: Int
        let archivedBackupSizeBytes: Int64
    }

    /// A show, movie or folder whose replaced seasons still hold original backups.
    struct Show: Identifiable, Sendable {
        let prefix: String
        let name: String
        var seasonPrefixes: [String]
        var backupBytes: Int64
        var id: String { prefix }
    }

    struct Completed: Decodable, Sendable {
        let folders: [Folder]
    }

    static func shows(_ folders: [Folder]) -> [Show] {
        var shows: [Show] = []
        for folder in folders where folder.archivedBackupCount > 0 {
            let prefix = folder.prefix.split(separator: "/").prefix(2).joined(separator: "/")
            if let index = shows.firstIndex(where: { $0.prefix == prefix }) {
                shows[index].seasonPrefixes.append(folder.prefix)
                shows[index].backupBytes += folder.archivedBackupSizeBytes
            } else {
                shows.append(Show(prefix: prefix, name: folder.subtitle, seasonPrefixes: [folder.prefix], backupBytes: folder.archivedBackupSizeBytes))
            }
        }
        return shows
    }
}

/// The item page's player on an episode of the chosen show whose original backup is still there.
struct CompareEpisodes: View {
    let shows: [SpaceSavedFacts.Show]
    let model: MainWindowModel
    // Opens the season the episode on screen belongs to.
    let openItem: (String) -> Void
    @State private var showPrefix: String?
    @State private var loaded = Loaded.loading
    // Each load takes the next number; only the latest one may change what the player shows.
    @State private var generation = 0
    @State private var notice: ActionNotice?

    /// What the player has for the chosen show: an episode with its backup, or why it has none.
    enum Loaded {
        case loading
        case episode(show: String, season: String, file: ItemFacts.File)
        case noBackupEpisode, failed
    }

    private var current: SpaceSavedFacts.Show? {
        shows.first { $0.prefix == showPrefix } ?? shows.first
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            if case .episode(_, let season, let file) = loaded {
                Button(Words.compareFile(season, file)) { openItem(season) }
                    .buttonStyle(QuietLinkStyle())
            }
            switch loaded {
            case .episode(_, let season, let file):
                FilePlayer(file: file, model: model) {
                    Task { await putOriginalBack(season, file) }
                }
                .id("\(file.id) \(file.stage) \(file.originalBackup)")
                .frame(maxWidth: 760)
            case .loading:
                ProgressView().frame(maxWidth: 760, minHeight: 200)
            case .noBackupEpisode, .failed:
                VStack(spacing: 10) {
                    Text(loaded.failedToLoad ? Words.compareUnanswered : Words.compareNoEpisode)
                        .foregroundStyle(.secondary)
                    // Asking again only helps when Mediaforce didn't answer; otherwise another show can be chosen below.
                    if loaded.failedToLoad {
                        Button(Words.tryAgain) { Task { await load() } }
                    }
                }
                .frame(maxWidth: 760, minHeight: 200)
            }
            BackupShowTiles(shows: shows, selected: current?.prefix, posters: model.posters) {
                showPrefix = $0
            }
            .padding(.top, 8)
        }
        .task(id: current?.prefix) { await load() }
        .alert(item: $notice) { notice in
            Alert(title: Text(notice.title), message: Text(notice.message))
        }
    }

    private func load() async {
        guard let show = current else { return }
        generation += 1
        let mine = generation
        if loaded.show != show.prefix { loaded = .loading }
        var result = Loaded.noBackupEpisode
        do {
            for prefix in show.seasonPrefixes {
                if let found = try await model.client.item(prefix).files.first(where: \.originalBackup) {
                    result = .episode(show: show.prefix, season: prefix, file: found)
                    break
                }
            }
        } catch {
            result = .failed
        }
        // A slower or cancelled answer must not replace a newer one on screen.
        guard mine == generation, !Task.isCancelled else { return }
        loaded = result
    }

    private func putOriginalBack(_ season: String, _ file: ItemFacts.File) async {
        do {
            let result = try await model.client.putOriginalBack(season, file.libraryItemId)
            if !result.ok {
                notice = ActionNotice(title: Words.actionRefused(.putOriginalBack), message: Words.actionRefusal(result.code, result.message))
            }
        } catch {
            notice = .unanswered
        }
        await load()
    }
}

/// One poster per show that still holds backups; choosing one plays it above.
struct BackupShowTiles: View {
    let shows: [SpaceSavedFacts.Show]
    let selected: String?
    let posters: PosterStore
    let choose: (String) -> Void

    var body: some View {
        LazyVGrid(columns: [GridItem(.adaptive(minimum: 120, maximum: 140), spacing: 18, alignment: .top)], alignment: .leading, spacing: 18) {
            ForEach(shows) { show in
                Button { choose(show.prefix) } label: {
                    VStack(alignment: .leading, spacing: 5) {
                        poster(show)
                            .frame(width: 120, height: 180)
                            .clipShape(RoundedRectangle(cornerRadius: 6, style: .continuous))
                            .overlay {
                                if show.prefix == selected {
                                    RoundedRectangle(cornerRadius: 6, style: .continuous).strokeBorder(Color.accentColor, lineWidth: 3)
                                }
                            }
                        Text(show.name).font(.callout.weight(.medium)).lineLimit(2)
                        Text(Words.showBackups(show.seasonPrefixes.count, show.backupBytes))
                            .font(.caption)
                            .foregroundStyle(.secondary)
                    }
                    .frame(width: 120, alignment: .leading)
                }
                .buttonStyle(.plain)
            }
        }
    }

    @ViewBuilder
    private func poster(_ show: SpaceSavedFacts.Show) -> some View {
        if let image = posters.image(for: show.prefix) {
            Image(nsImage: image).resizable().aspectRatio(contentMode: .fill)
        } else {
            Text(show.name)
                .font(.callout.weight(.semibold))
                .multilineTextAlignment(.center)
                .padding(8)
                .frame(maxWidth: .infinity, maxHeight: .infinity)
                .background(Color.primary.opacity(0.06))
        }
    }
}

/// What the backups line shows: the deletion running, the backups waiting, and how the last deletion ended.
struct BackupsStatus {
    let cleanup: CleanupFolder
    let asking: Bool

    var deleting: Bool { asking || cleanup.deletion?.state == .running }
    var canDelete: Bool { !deleting && cleanup.fileCount > 0 }
    // The Delete button stays in place while deleting, unpressable, so the row doesn't jump.
    var showsDelete: Bool { deleting || cleanup.waitingReason == nil }

    var progress: Double? {
        guard deleting, let deletion = cleanup.deletion, let total = deletion.totalCount, total > 0 else { return nil }
        return Double(deletion.deletedCount) / Double(total)
    }

    var line: String {
        if deleting { return Words.deletingBackups(cleanup.deletion) }
        if let reason = cleanup.waitingReason { return reason }
        if cleanup.fileCount > 0 { return Words.deleteBackupsLine(cleanup.fileCount, cleanup.totalSizeBytes) }
        if let deletion = cleanup.deletion, deletion.state == .finished {
            if deletion.skippedFiles?.isEmpty == false { return Words.backupDeletionResult(deletion) }
            return "None waiting · \(deletion.deletedCount) deleted, \(Words.size(deletion.freedBytes))"
        }
        return "None waiting"
    }

    /// How the last deletion ended, when the line above doesn't already say it.
    var outcome: (text: String, failed: Bool)? {
        guard !deleting, let deletion = cleanup.deletion else { return nil }
        switch deletion.state {
        case .failed: return (Words.backupDeletionResult(deletion), true)
        case .finished where cleanup.fileCount > 0: return (Words.backupDeletionResult(deletion), deletion.skippedFiles?.isEmpty == false)
        default: return nil
        }
    }
}

/// The one place original backups are deleted. Mediaforce reads the Cleanup folder again and asks first.
struct DeleteBackups: View {
    let status: BackupsStatus
    let model: MainWindowModel
    let readNotice: String?
    let delete: () -> Void
    @State private var folder: CleanupFolder?
    @State private var confirming = false

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack(spacing: 12) {
                if status.deleting {
                    ProgressView().controlSize(.small)
                }
                VStack(alignment: .leading, spacing: 4) {
                    Text("Original backups").fontWeight(.semibold)
                    Text(readNotice ?? status.line).monospacedDigit().foregroundStyle(.secondary)
                }
                Spacer()
                if status.showsDelete {
                    Button(Words.deleteBackups) { Task { await askFirst() } }
                        .buttonStyle(.bordered)
                        .disabled(readNotice != nil || !status.canDelete)
                        .help(Words.deleteBackupsHelp)
                }
            }
            if let progress = status.progress {
                ProgressView(value: progress).frame(maxWidth: 420)
            }
            if let outcome = status.outcome {
                Label(outcome.text, systemImage: outcome.failed ? "exclamationmark.triangle.fill" : "checkmark.circle")
                    .foregroundStyle(outcome.failed ? AnyShapeStyle(Color.orange) : AnyShapeStyle(.secondary))
                    .textSelection(.enabled)
            }
        }
        .confirmationDialog(Words.deleteBackupsQuestion, isPresented: $confirming, presenting: folder) { _ in
            Button(Words.deleteAllBackups, role: .destructive, action: delete)
        } message: { folder in
            Text(Words.deleteBackupsDetail(folder))
        }
    }

    private func askFirst() async {
        do {
            let found = try await model.client.cleanupFolder()
            if let reason = found.waitingReason {
                model.notice = ActionNotice(title: Words.deleteBackups, message: reason)
            } else if found.fileCount > 0 {
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

extension CompareEpisodes.Loaded {
    var show: String? {
        if case .episode(let show, _, _) = self { show } else { nil }
    }

    var failedToLoad: Bool {
        if case .failed = self { true } else { false }
    }
}
