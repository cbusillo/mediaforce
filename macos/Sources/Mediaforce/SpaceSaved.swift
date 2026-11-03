import SwiftUI

/// What compressing has given back, spot-check first: the compare player on an episode whose original backup is still
/// there, one tile per show that still holds backups, then the library as one storage bar with the one Delete.
struct SpaceSavedPage: View {
    static let spotCheckID = "spotCheck"

    let facts: MainWindowFacts
    let model: MainWindowModel
    let openItem: (String) -> Void

    var body: some View {
        // Around the page, so it reaches the page's own scroll view.
        ScrollViewReader { proxy in
            RefreshingPage(title: Words.spaceSaved, subtitle: Words.savedSoFar(facts.spaceSaved.bytes),
                           load: { try await SpaceSavedFacts.load(model.client) }) { (saved: SpaceSavedFacts) in
                if saved.shows.isEmpty {
                    Text(Words.noBackupsToCheck)
                        .foregroundStyle(.secondary)
                } else {
                    SpotCheck(shows: saved.shows, model: model, openItem: openItem)
                        .id(Self.spotCheckID)
                }
                SpaceSavedBar(
                    library: facts.library, savedBytes: facts.spaceSaved.bytes, backupBytes: saved.cleanup.totalSizeBytes,
                    // The backups' slice opens the spot-check of what they hold.
                    openBackups: saved.shows.isEmpty ? nil : { withAnimation { proxy.scrollTo(Self.spotCheckID, anchor: .top) } }
                )
                .padding(.top, 8)
                if saved.cleanup.fileCount > 0 {
                    DeleteBackups(count: saved.cleanup.fileCount, bytes: saved.cleanup.totalSizeBytes, model: model)
                }
            }
        }
    }
}

struct SpaceSavedFacts: Sendable {
    let shows: [Show]
    // The whole Cleanup folder, as Delete clears it, not only the backups of seasons listed as finished.
    let cleanup: CleanupFolder

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

    @MainActor
    static func load(_ client: MediaforceClient) async throws -> SpaceSavedFacts {
        async let completed = client.completed()
        async let cleanup = client.cleanupFolder()
        return try await SpaceSavedFacts(shows: shows(completed.folders), cleanup: cleanup)
    }
}

/// The item page's player on one show at a time. "Looks Good" ticks the show and moves on; "Next" moves on without one.
struct SpotCheck: View {
    let shows: [SpaceSavedFacts.Show]
    let model: MainWindowModel
    // Opens the season the episode being checked belongs to.
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
            HStack(alignment: .firstTextBaseline) {
                Text(Words.spotCheckFirst).font(.headline)
                Spacer()
                if case .episode(_, let season, let file) = loaded {
                    Button(Words.spotCheckFile(season, file)) { openItem(season) }
                        .buttonStyle(QuietLinkStyle())
                }
            }
            .frame(maxWidth: 760)
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
                    Text(loaded.failedToLoad ? Words.spotCheckUnanswered : Words.spotCheckNoEpisode)
                        .foregroundStyle(.secondary)
                    // Asking again only helps when Mediaforce didn't answer; a show with no backup to compare moves on with Next.
                    if loaded.failedToLoad {
                        Button(Words.tryAgain) { Task { await load() } }
                    }
                }
                .frame(maxWidth: 760, minHeight: 200)
            }
            HStack {
                Text(Words.spotCheckPrompt).foregroundStyle(.secondary)
                Spacer()
                Button(Words.next) { advance() }
                    .disabled(shows.count < 2)
                Button(Words.looksGood) {
                    if case .episode(let show, _, _) = loaded { model.checkedShows.insert(show) }
                    advance()
                }
                .buttonStyle(.borderedProminent)
                // Only a show whose episode is on screen can be ticked.
                .disabled(loaded.show == nil || loaded.show != current?.prefix)
            }
            .frame(maxWidth: 760)
            BackupShowTiles(shows: shows, selected: current?.prefix, checked: model.checkedShows, posters: model.posters) {
                showPrefix = $0
            }
            .padding(.top, 8)
        }
        .task(id: current?.prefix) { await load() }
        .alert(item: $notice) { notice in
            Alert(title: Text(notice.title), message: Text(notice.message))
        }
    }

    /// The next show not yet ticked, or simply the next one once every show is.
    private func advance() {
        guard let current, let index = shows.firstIndex(where: { $0.prefix == current.prefix }) else { return }
        let after = shows[(index + 1)...] + shows[..<index]
        showPrefix = (after.first { !model.checkedShows.contains($0.prefix) } ?? after.first)?.prefix
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

/// One poster per show that still holds backups; a tick marks each show checked, and choosing one plays it above.
struct BackupShowTiles: View {
    let shows: [SpaceSavedFacts.Show]
    let selected: String?
    let checked: Set<String>
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
                            .overlay(alignment: .topTrailing) {
                                if checked.contains(show.prefix) {
                                    Image(systemName: "checkmark.circle.fill")
                                        .font(.title2)
                                        .foregroundStyle(.white, Color.accentColor)
                                        .padding(6)
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
                .help(checked.contains(show.prefix) ? Words.showChecked : Words.checkThisShow)
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

/// The library as one bar: compressed now, still original, and the space got back, with the backups' share marked.
struct SpaceSavedBar: View {
    // Wide enough to see even when the backups are a sliver of the library.
    static let smallestBackupSlice: CGFloat = 24

    let library: MainWindowFacts.Library
    let savedBytes: Int64
    let backupBytes: Int64
    var openBackups: (() -> Void)?

    private var nowBytes: Int64 { MediaKind.everything.totalBytes(in: library) }
    private var stillOriginalBytes: Int64 { MediaKind.everything.originalBytes(in: library) }
    private var compressedBytes: Int64 { nowBytes - stillOriginalBytes }

    var body: some View {
        let total = max(Double(nowBytes + savedBytes), 1)
        VStack(alignment: .leading, spacing: 8) {
            GeometryReader { geometry in
                let width = geometry.size.width
                HStack(spacing: 2) {
                    Rectangle().fill(Color.accentColor).frame(width: width * Double(compressedBytes) / total)
                    Rectangle().fill(Color.accentColor.opacity(0.35)).frame(width: width * Double(stillOriginalBytes) / total)
                    ZStack(alignment: .leading) {
                        Rectangle().fill(.quaternary)
                        if backupBytes > 0 {
                            Rectangle().fill(Color.orange.opacity(0.85))
                                .frame(width: max(width * Double(backupBytes) / total, Self.smallestBackupSlice))
                                .contentShape(Rectangle())
                                .onTapGesture { openBackups?() }
                                .pointerStyle(openBackups == nil ? nil : .link)
                                .help(openBackups == nil ? "" : Words.spotCheckBackups)
                        }
                    }
                }
                .clipShape(RoundedRectangle(cornerRadius: 6, style: .continuous))
            }
            .frame(height: 18)
            HStack(spacing: 18) {
                StorageKey(style: AnyShapeStyle(Color.accentColor), text: Words.storageCompressed(compressedBytes))
                StorageKey(style: AnyShapeStyle(Color.accentColor.opacity(0.35)), text: Words.storageStillOriginal(stillOriginalBytes))
                StorageKey(style: AnyShapeStyle(.quaternary), text: Words.storageSaved(savedBytes))
                if backupBytes > 0 {
                    StorageKey(style: AnyShapeStyle(Color.orange.opacity(0.85)), text: Words.storageBackups(backupBytes))
                }
                Spacer()
            }
            .font(.callout)
            .foregroundStyle(.secondary)
            Text(Words.libraryWasAndIs(nowBytes + savedBytes, nowBytes))
                .font(.callout)
                .foregroundStyle(.secondary)
        }
    }
}

struct StorageKey: View {
    let style: AnyShapeStyle
    let text: String

    var body: some View {
        HStack(spacing: 6) {
            RoundedRectangle(cornerRadius: 2).fill(style).frame(width: 10, height: 10)
            Text(text).monospacedDigit()
        }
    }
}

/// The one place original backups are deleted. Mediaforce reads the Cleanup folder again and asks first.
struct DeleteBackups: View {
    let count: Int
    let bytes: Int64
    let model: MainWindowModel
    @State private var folder: CleanupFolder?
    @State private var confirming = false

    var body: some View {
        HStack(spacing: 12) {
            Text(Words.deleteBackupsLine(count, bytes))
            Spacer()
            Button(Words.deleteBackups) {
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
            .buttonStyle(.borderedProminent)
            .help(Words.deleteBackupsHelp)
        }
        .confirmationDialog(Words.deleteBackupsQuestion, isPresented: $confirming, presenting: folder) { _ in
            Button(Words.deleteAllBackups, role: .destructive) {
                Task { await model.deleteCleanupFolder() }
            }
        } message: { folder in
            Text(Words.deleteBackupsDetail(folder))
        }
    }
}

extension SpotCheck.Loaded {
    var show: String? {
        if case .episode(let show, _, _) = self { show } else { nil }
    }

    var failedToLoad: Bool {
        if case .failed = self { true } else { false }
    }
}
