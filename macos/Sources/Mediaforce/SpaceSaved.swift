import SwiftUI

/// What compressing has given back, spot-check first: the compare player on an episode whose original backup is still
/// there, one tile per show that still holds backups, then the library as one storage bar with the one Delete.
struct SpaceSavedPage: View {
    let facts: MainWindowFacts
    let model: MainWindowModel

    var body: some View {
        RefreshingPage(title: Words.spaceSaved, load: { try await SpaceSavedFacts.load(model.client) }) { (saved: SpaceSavedFacts) in
            Text(Words.savedSoFar(facts.spaceSaved.bytes))
                .font(.title3)
                .foregroundStyle(.secondary)
            if saved.shows.isEmpty {
                Text(Words.noBackupsToCheck)
                    .foregroundStyle(.secondary)
            } else {
                SpotCheck(shows: saved.shows, model: model)
            }
            SpaceSavedBar(library: facts.library, savedBytes: facts.spaceSaved.bytes, backupBytes: saved.backupBytes)
                .padding(.top, 8)
            if saved.backupCount > 0 {
                DeleteBackups(count: saved.backupCount, bytes: saved.backupBytes, model: model)
            }
        }
    }
}

struct SpaceSavedFacts: Decodable, Sendable {
    let folders: [Folder]
    let archiveCleanup: Cleanup

    struct Folder: Decodable, Sendable {
        let prefix: String
        let subtitle: String
        let archivedBackupCount: Int
        let archivedBackupSizeBytes: Int64
    }

    struct Cleanup: Decodable, Sendable {
        let fileCount: Int
        let totalSizeBytes: Int64
    }

    /// A show, movie or folder whose replaced seasons still hold original backups.
    struct Show: Identifiable, Sendable {
        let prefix: String
        let name: String
        var seasonPrefixes: [String]
        var backupBytes: Int64
        var id: String { prefix }
    }

    var backupCount: Int { archiveCleanup.fileCount }
    var backupBytes: Int64 { archiveCleanup.totalSizeBytes }

    var shows: [Show] {
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
        try await client.completed()
    }
}

/// The item page's player on one show at a time. "Looks Good" ticks the show and moves on; "Next" moves on without one.
struct SpotCheck: View {
    let shows: [SpaceSavedFacts.Show]
    let model: MainWindowModel
    @State private var showPrefix: String?
    @State private var season: String?
    @State private var file: ItemFacts.File?
    @State private var notice: ActionNotice?

    private var current: SpaceSavedFacts.Show? {
        shows.first { $0.prefix == showPrefix } ?? shows.first
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack(alignment: .firstTextBaseline) {
                Text(Words.spotCheckFirst).font(.headline)
                Spacer()
                if let season, let file {
                    Text(Words.spotCheckFile(season, file)).foregroundStyle(.secondary)
                }
            }
            .frame(maxWidth: 760)
            if let file, let season {
                ItemPlayer(file: file, client: model.client) {
                    Task { await putOriginalBack(season, file) }
                }
                .id("\(file.id) \(file.stage) \(file.originalBackup)")
                .frame(maxWidth: 760)
            } else {
                ProgressView().frame(maxWidth: 760, minHeight: 200)
            }
            HStack {
                Text(Words.spotCheckPrompt).foregroundStyle(.secondary)
                Spacer()
                Button(Words.next) { advance() }
                    .disabled(shows.count < 2)
                Button(Words.looksGood) {
                    if let current { model.checkedShows.insert(current.prefix) }
                    advance()
                }
                .buttonStyle(.borderedProminent)
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
        file = nil
        guard let current else { return }
        for prefix in current.seasonPrefixes {
            if let found = try? await model.client.item(prefix).files.first(where: \.originalBackup) {
                season = prefix
                file = found
                return
            }
        }
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

    private var nowBytes: Int64 { library.tv.totalBytes + library.movies.totalBytes + library.other.totalBytes }
    private var stillOriginalBytes: Int64 { library.tv.originalBytes + library.movies.originalBytes + library.other.originalBytes }
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
