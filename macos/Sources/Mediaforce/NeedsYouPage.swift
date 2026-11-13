import SwiftUI

/// What is waiting for the Director, in the TV shows page's own parts. A decision about a kind of content leads,
/// then shows as poster cards, then the compact rows for computers and cleanup.
struct NeedsYouPage: View {
    let facts: MainWindowFacts
    let posters: PosterStore
    let model: MainWindowModel
    let open: (Place) -> Void
    @State private var originalPreview: OriginalPreview?
    // Each waiting season's files, read from its page, so its card can say what they ask.
    @State private var seasonFiles: [String: ItemFacts] = [:]

    var body: some View {
        let waiting = WaitingGroups(facts)
        let previews = facts.originalPreviews ?? []
        PageFrame(title: Words.needsYou, subtitle: Words.workHeadline(facts)) {
            if let status = Words.workExplanation(facts, onNeedsYou: true) {
                Text(status)
                    .foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
                    .frame(maxWidth: 640, alignment: .leading)
            }
            if waiting.isEmpty && previews.isEmpty {
                VStack(alignment: .leading, spacing: 4) {
                    Text("Nothing is waiting for you").font(.title2.weight(.semibold))
                    Text("Mediaforce marks this page and the sidebar when something does.")
                        .font(.callout)
                        .foregroundStyle(.secondary)
                }
                FinishedShelf(seasons: Array(facts.recentlyFinishedSeasons.prefix(6)), posters: posters, open: open)
            }
            ForEach(waiting.kinds, id: \.id) { item in
                KindDecisionBand(item: item, kind: model.kinds?.kinds.first { item.id.hasPrefix("kind:\($0.kind):") },
                                 action: facts.work.kindsNeedingYou.first { item.id.hasPrefix("kind:\($0.kind):") }.map(Words.kindAction)
                                     ?? Words.waitingAction(item.opens),
                                 posters: posters, primary: item == waiting.first, open: open)
            }
            if !waiting.shows.isEmpty { shows(waiting) }
            if !waiting.computers.isEmpty { computers(waiting) }
            if waiting.backups != nil || !previews.isEmpty { cleanup(waiting, previews) }
        }
        .sheet(item: $originalPreview) { OriginalApprovalView(preview: $0, model: model) }
        .task(id: waitingSeasonsKey) { await readSeasonFiles() }
    }

    // A season's files are read again when its state changes or another file finishes somewhere.
    private var waitingSeasonsKey: [String] {
        facts.work.seasonsNeedingYou.map { "\($0.prefix):\($0.state ?? ""):\($0.action ?? "")" } + ["\(facts.lastChange?.id ?? 0)"]
    }

    private func readSeasonFiles() async {
        var read: [String: ItemFacts] = [:]
        for season in facts.work.seasonsNeedingYou {
            read[season.prefix] = try? await model.client.item(season.prefix)
        }
        guard !Task.isCancelled else { return }
        seasonFiles = read
    }

    private func heading(_ title: String, count: Int? = nil) -> some View {
        HStack(spacing: 6) {
            Text(title).font(.headline)
            if let count { Text("\(count)").foregroundStyle(.secondary).monospacedDigit() }
        }
    }

    private func shows(_ waiting: WaitingGroups) -> some View {
        VStack(alignment: .leading, spacing: 12) {
            heading("Shows", count: waiting.shows.count)
            LazyVGrid(columns: [GridItem(.adaptive(minimum: ShowCard.width, maximum: ShowCard.width), spacing: 22, alignment: .top)],
                      alignment: .leading, spacing: 22) {
                ForEach(waiting.shows, id: \.id) { item in
                    ShowCard(item: item, card: WaitingShow(item, facts: facts, files: seasonFiles), posters: posters,
                             primary: item == waiting.first, open: open)
                }
            }
        }
    }

    private func computers(_ waiting: WaitingGroups) -> some View {
        VStack(alignment: .leading, spacing: 10) {
            heading("Computers", count: waiting.computers.count)
            ForEach(waiting.computers, id: \.id) { item in
                let computer = facts.computers.first { item.id.hasPrefix("computer:\($0.key):") }
                CompactDecision(symbol: computer.map(Words.computerSymbol) ?? "desktopcomputer",
                                name: computer.flatMap(\.name) ?? item.title,
                                detail: computer.map { $0.attentionReason ?? Words.computerState($0.state) } ?? item.body,
                                waiting: true, action: Words.waitingAction(item.opens),
                                primary: item == waiting.first) { item.opens.map(open) }
            }
        }
    }

    private func cleanup(_ waiting: WaitingGroups, _ previews: [OriginalPreview]) -> some View {
        VStack(alignment: .leading, spacing: 10) {
            heading("Cleanup")
            ForEach(previews) { preview in
                CompactDecision(symbol: "doc.text.magnifyingglass", name: previewLine(preview), action: "Review file list") {
                    originalPreview = preview
                }
            }
            if let backups = waiting.backups {
                BackupsCleanup(backups: facts.originalBackups, client: model.client, action: Words.waitingAction(backups.opens),
                               primary: backups == waiting.first) {
                    backups.opens.map(open)
                }
            }
        }
    }

    private func previewLine(_ preview: OriginalPreview) -> String {
        if let problem = preview.problem { return problem }
        if let waiting = preview.waiting { return waiting }
        if let list = try? preview.fileList() { return "\(list.items.count) original files need your confirmation" }
        return "An original-file request needs preparing again"
    }
}

/// The page's waiting items by where each one is drawn. Original-file confirmations are drawn from their own previews.
struct WaitingGroups {
    let kinds: [AttentionNotice]
    let shows: [AttentionNotice]
    let computers: [AttentionNotice]
    let backups: AttentionNotice?
    // The one strong button on the page belongs to whichever item comes first.
    var first: AttentionNotice? { kinds.first ?? shows.first ?? computers.first ?? backups }
    var isEmpty: Bool { first == nil }

    init(_ facts: MainWindowFacts) {
        let items = Attention.waitingItems(facts).filter { !$0.id.hasPrefix("original:") }
        kinds = items.filter { $0.id.hasPrefix("kind:") }
        computers = items.filter { $0.id.hasPrefix("computer:") }
        backups = items.first { $0.id == Attention.backupsNoticeID }
        shows = items.filter { !$0.id.hasPrefix("kind:") && !$0.id.hasPrefix("computer:") && $0.id != Attention.backupsNoticeID }
    }
}

/// What one waiting show says on its card. The poster's label says its state and the button what to do, so a
/// description appears only when it tells the Director why.
@MainActor
struct WaitingShow {
    let name: String
    let posterPrefix: String?
    let label: String
    let state: String
    let description: String?
    let action: String

    init(_ item: AttentionNotice, facts: MainWindowFacts, files: [String: ItemFacts] = [:]) {
        if let sample = facts.samples.waitingOnYou.first(where: { item.opens == .sample($0.prefix) }) {
            name = sample.show
            posterPrefix = sample.showPrefix
            label = sample.season.map { "Season \($0)" } ?? sample.show
            state = Words.sampleState(sample)
            description = Words.sampleFailure(sample.failure)
            action = Words.sampleAction(sample.action) ?? Words.waitingAction(item.opens)
        } else if let season = facts.work.seasonsNeedingYou.first(where: { item.opens == .item($0.prefix) }) {
            name = season.showName
            posterPrefix = MediaPoster.titlePrefix(season.prefix)
            label = Words.seasonName(season)
            state = files[season.prefix].flatMap { Words.filesAsk($0.waiting) } ?? Words.tvSeasonState(season)
            description = Words.sampleFailure(season.failure)
            action = Words.waitingAction(item.opens)
        } else {
            name = item.title
            posterPrefix = nil
            label = Words.needsYou
            state = ""
            description = item.body
            action = Words.waitingAction(item.opens)
        }
    }
}

/// A decision that covers many shows: wide, with a stack of the shows it decides, so it never reads as one show's card.
private struct KindDecisionBand: View {
    private static let postersShown = 5

    let item: AttentionNotice
    let kind: KindsFacts.Kind?
    let action: String
    let posters: PosterStore
    let primary: Bool
    let open: (Place) -> Void

    var body: some View {
        HStack(alignment: .center, spacing: 18) {
            if let shows = kind?.shows, !shows.isEmpty {
                HStack(spacing: -16) {
                    ForEach(shows.prefix(Self.postersShown)) { show in
                        MediaPoster(titlePrefix: show.prefix, name: show.name,
                                    kind: MediaKind(rawValue: String(show.prefix.split(separator: "/").first ?? "")) ?? .other,
                                    posters: posters, width: 46)
                            .overlay(RoundedRectangle(cornerRadius: 3, style: .continuous)
                                .strokeBorder(Color(nsColor: .textBackgroundColor), lineWidth: 1.5))
                    }
                }
            }
            VStack(alignment: .leading, spacing: 6) {
                HStack(alignment: .firstTextBaseline, spacing: 8) {
                    Text(item.title).font(.title3.weight(.semibold))
                    if let kind { Text(Words.kindSubtitle(kind)).foregroundStyle(.secondary).monospacedDigit() }
                }
                Text(item.body)
                    .foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
                if let place = item.opens {
                    DecisionButton(title: action, primary: primary) { open(place) }
                        .padding(.top, 2)
                }
            }
        }
        .padding(14)
        .frame(maxWidth: 720, alignment: .leading)
        .background(Color.accentColor.opacity(0.08), in: RoundedRectangle(cornerRadius: 8, style: .continuous))
    }
}

private struct ShowCard: View {
    static let width: CGFloat = 148

    let item: AttentionNotice
    let card: WaitingShow
    let posters: PosterStore
    let primary: Bool
    let open: (Place) -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            Button { item.opens.map(open) } label: {
                PosterView(show: card.name, image: card.posterPrefix.flatMap(posters.image(for:)),
                           headline: card.label, detail: card.state, width: Self.width)
            }
            .buttonStyle(.plain)
            .pointerStyle(.link)
            .accessibilityLabel(item.title)
            .help(card.state)
            Text(card.name).font(.callout.weight(.semibold)).lineLimit(1)
            if let description = card.description {
                Text(description)
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            }
            Spacer(minLength: 0)
            if let place = item.opens {
                DecisionButton(title: card.action, primary: primary) { open(place) }
            }
        }
        // A row's cards share its height, so their buttons line up.
        .frame(width: Self.width, alignment: .leading)
        .frame(maxHeight: .infinity, alignment: .top)
    }
}

/// A computer or cleanup job as the TV shows page draws a computer, with its button beside it.
private struct CompactDecision: View {
    let symbol: String
    let name: String
    var detail: String?
    var waiting = false
    let action: String
    var primary = false
    let run: () -> Void

    var body: some View {
        HStack(alignment: .center, spacing: 10) {
            Image(systemName: symbol)
                .foregroundStyle(.secondary)
                .frame(width: 22)
                .accessibilityHidden(true)
            VStack(alignment: .leading, spacing: 1) {
                Text(name).fixedSize(horizontal: false, vertical: true)
                if let detail {
                    HStack(spacing: 4) {
                        if waiting { WaitingMark().accessibilityHidden(true) }
                        Text(detail).foregroundStyle(.secondary)
                    }
                    .font(.caption)
                }
            }
            .frame(minWidth: 260, alignment: .leading)
            DecisionButton(title: action, primary: primary, run: run)
        }
        .padding(.leading, 6)
    }
}

/// Original backups as one sentence with their size and the one button, with the titles holding them listed only on request.
private struct BackupsCleanup: View {
    let backups: MainWindowFacts.OriginalBackups
    let client: MediaforceClient
    let action: String
    let primary: Bool
    let review: () -> Void
    @State private var listed = false
    @State private var titles: [SpaceSavedFacts.Show]?
    @State private var unreadable = false

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack(alignment: .firstTextBaseline, spacing: 12) {
                Text(Words.backupsReady(backups)).monospacedDigit()
                DecisionButton(title: action, primary: primary, run: review)
            }
            DisclosureGroup(Words.backupsByTitle, isExpanded: $listed) {
                if let titles {
                    Grid(alignment: .leadingFirstTextBaseline, horizontalSpacing: 18, verticalSpacing: 4) {
                        ForEach(titles) { title in
                            GridRow {
                                Text(title.name)
                                Text(Words.showBackups(title.seasonPrefixes.count, title.backupBytes))
                                    .foregroundStyle(.secondary)
                                    .monospacedDigit()
                            }
                        }
                    }
                    .padding(.top, 4)
                } else if unreadable {
                    Text(Words.backupsUnlisted).foregroundStyle(.secondary)
                } else {
                    ProgressView().controlSize(.small)
                }
            }
            .font(.callout)
            .frame(maxWidth: 640, alignment: .leading)
        }
        .padding(.leading, 6)
        .task(id: listed) { await list() }
    }

    private func list() async {
        guard listed, titles == nil else { return }
        do {
            titles = SpaceSavedFacts.shows(try await client.completed().folders)
            unreadable = false
        } catch {
            unreadable = true
        }
    }
}

private struct DecisionButton: View {
    let title: String
    let primary: Bool
    let run: () -> Void

    var body: some View {
        if primary {
            Button(title, action: run).buttonStyle(.borderedProminent).controlSize(.small)
        } else {
            Button(title, action: run).controlSize(.small)
        }
    }
}

struct WaitingItems: View {
    let items: [AttentionNotice]
    let posters: PosterStore
    let open: (Place) -> Void

    var body: some View {
            ForEach(items, id: \.id) { item in
                HStack(alignment: .top, spacing: 14) {
                    artwork(item)
                    VStack(alignment: .leading, spacing: 8) {
                        HStack(alignment: .firstTextBaseline, spacing: 6) {
                            WaitingMark()
                            if let place = item.opens {
                                Button(item.title) { open(place) }
                                    .buttonStyle(QuietLinkStyle(primary: true))
                            } else {
                                Text(item.title)
                            }
                        }
                        .font(.headline)
                        DecisionLine {
                            Text(item.body).foregroundStyle(.secondary)
                        } buttons: {
                            if let place = item.opens {
                                Button(Words.waitingAction(place)) { open(place) }
                            }
                        }
                    }
                    .frame(maxWidth: .infinity, alignment: .leading)
                }
                Divider()
            }
    }

    @ViewBuilder
    private func artwork(_ item: AttentionNotice) -> some View {
        switch item.opens {
        case .sample(let prefix), .item(let prefix):
            MediaPoster(titlePrefix: MediaPoster.titlePrefix(prefix), name: item.title,
                        kind: MediaKind(rawValue: String(prefix.split(separator: "/").first ?? "")) ?? .other,
                        posters: posters, width: 44)
        default:
            Image(systemName: symbol(item.opens))
                .font(.title2)
                .foregroundStyle(.secondary)
                .frame(width: 44, height: 66)
                .accessibilityHidden(true)
        }
    }

    private func symbol(_ place: Place?) -> String {
        switch place {
        case .kind: "film"
        case .spaceSaved: "trash"
        default: "desktopcomputer"
        }
    }
}
