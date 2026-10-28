import SwiftUI

/// The page for one show season, movie or folder: the player leads, every file sits beside it, and
/// anything waiting for the Director is answered right above the player.
struct ItemPage: View {
    let prefix: String
    let model: MainWindowModel
    let openSample: (String) -> Void
    let openItem: (String) -> Void
    @State private var facts: ItemFacts?
    @State private var work: SampleFacts?
    @State private var show: TVShowsFacts.Show?
    @State private var refreshFailed = false
    @State private var selected: ItemFacts.File.ID?
    @State private var notice: ActionNotice?
    @State private var confirming: WorkAction?
    @State private var working = false

    var body: some View {
        page
            .background(Color(nsColor: .textBackgroundColor))
            // Until its facts arrive, the page is named by its folder.
            .navigationTitle(facts.map(Words.itemTitle) ?? prefix.split(separator: "/").last.map(String.init) ?? prefix)
            // A season names its show; a show's own page is already named by it.
            .navigationSubtitle(facts.flatMap { $0.kind == "tv_series" ? nil : $0.show } ?? "")
            .task(id: prefix) { await watch() }
            .alert(item: $notice) { notice in
                Alert(title: Text(notice.title), message: Text(notice.message))
            }
            .confirmationDialog(
                confirming.map(Words.workQuestion) ?? "",
                isPresented: Binding(get: { confirming != nil }, set: { if !$0 { confirming = nil } }),
                presenting: confirming
            ) { action in
                Button(Words.workButton(action)) { Task { await run(action, facts) } }
                Button(Words.cancel, role: .cancel) {}
            } message: { action in
                Text(Words.workDetail(action))
            }
    }

    private var page: some View {
        Group {
            if let facts {
                HStack(alignment: .top, spacing: 0) {
                    ScrollView {
                        VStack(alignment: .leading, spacing: 18) {
                            HStack(alignment: .top) {
                                ItemHeader(facts: facts, poster: facts.showPrefix.flatMap(model.posters.image(for:)), openShow: openItem)
                                if let work {
                                    WorkActions(actions: work.actions, working: working) { action in
                                        perform(action, facts)
                                    }
                                }
                            }
                            if let show {
                                ShowSeasons(show: show, openSample: openSample, openItem: openItem)
                            }
                            if !facts.waiting.isEmpty {
                                WaitingFiles(files: facts.waiting) { file, action in
                                    Task { await act(action, file) }
                                }
                            }
                            if let file = selectedFile(facts) {
                                FilePlayer(file: file, model: model) {
                                    Task { await act(.putOriginalBack, file) }
                                }
                                // A file that is published, put back or loses its backup plays again from the start.
                                .id("\(file.id) \(file.stage) \(file.bytes) \(file.originalBackup)")
                            }
                            if facts.sound != nil || facts.stillAiring != nil {
                                ShowChoices(facts: facts, openShow: openItem) { change in Task { await save(change) } }
                            }
                        }
                        .padding(.horizontal, 28)
                        .padding(.vertical, 20)
                    }
                    .frame(maxWidth: .infinity)
                    Divider()
                    FileList(files: facts.files, selected: $selected)
                        .frame(width: 330)
                }
            } else if refreshFailed {
                Text(Words.staleNotice(.failing, lastHeard: nil))
                    .foregroundStyle(.secondary)
                    .frame(maxWidth: .infinity, maxHeight: .infinity)
            } else {
                ProgressView()
                    .frame(maxWidth: .infinity, maxHeight: .infinity)
            }
        }
    }

    private func perform(_ action: SampleFacts.Action, _ facts: ItemFacts) {
        switch action {
        case .approve, .makeAgain, .makeSample, .chooseSize, .tryAnotherSize, .openSample: openSample(prefix)
        case .replaceOriginals: confirming = .replaceOriginals
        case .decideMotion: confirming = .keepMotionOriginals
        case .startCompressing: Task { await run(.startCompressing, facts) }
        case .checkFiles: Task { await run(.checkFiles, facts) }
        case .retryUnfinished: Task { await run(.retryUnfinished, facts) }
        }
    }

    private func run(_ action: WorkAction, _ facts: ItemFacts?) async {
        working = true
        defer { working = false }
        do {
            let result = try await send(action, facts)
            if !result.ok {
                notice = ActionNotice(title: Words.workRefused(action), message: result.message ?? ActionNotice.sampleRefused.message)
            }
        } catch {
            notice = .unanswered
        }
        await refresh()
    }

    private func send(_ action: WorkAction, _ facts: ItemFacts?) async throws -> ItemActionResult {
        let client = model.client
        switch action {
        case .startCompressing:
            // Started only when it says it is compressing; some reasons need a choice made first.
            let started = try await client.compressSeason(prefix)
            if started.compressing == true { return .done }
            return .refused((started.needsChoice ? ActionNotice.compressNeedsChoice : .approvedNotCompressing).message)
        case .checkFiles: return try await client.checkFiles(prefix)
        case .replaceOriginals: return try await client.replaceOriginals(prefix)
        case .retryUnfinished: return try await client.retryUnfinished(prefix)
        case .keepMotionOriginals:
            return try await client.keepMotionOriginals(prefix, facts?.files.filter { $0.heldReason == "motion_pattern" }.map(\.libraryItemId) ?? [])
        }
    }

    private func selectedFile(_ facts: ItemFacts) -> ItemFacts.File? {
        facts.files.first { $0.id == selected }
            ?? facts.files.first { $0.originalBackup }
            ?? facts.files.first { $0.compressed }
            ?? facts.files.first
    }

    private func watch() async {
        while !Task.isCancelled {
            await refresh()
            try? await Task.sleep(for: MainWindowModel.refreshInterval)
        }
    }

    private func refresh() async {
        do {
            facts = try await model.client.item(prefix)
            refreshFailed = false
        } catch {
            refreshFailed = true
        }
        // The season's own next step, read the same way the sample page reads it; none for a plain folder.
        // A show is sampled season by season, so its page leaves each season's step to its row.
        work = facts?.kind == "tv_series" ? nil : try? await model.client.sample(prefix)
        // A whole show's page lists its seasons, each with its own next step.
        if facts?.kind == "tv_series" {
            show = (try? await model.client.tvShows())?.shows.first { $0.showPrefix == prefix } ?? show
        }
    }

    private func act(_ action: ItemAction, _ file: ItemFacts.File) async {
        do {
            let client = model.client
            let result = switch action {
            case .makeAgain: try await client.makeAgain(prefix, file.libraryItemId)
            case .keepThisCopy: try await client.keepThisCopy(prefix, file.libraryItemId)
            case .putOriginalBack: try await client.putOriginalBack(prefix, file.libraryItemId)
            case .allowLarger: try await client.decideSize(file.sizeQuestion?.jobId ?? "", allow: true)
            case .keepOriginal: try await client.decideSize(file.sizeQuestion?.jobId ?? "", allow: false)
            }
            if !result.ok {
                notice = ActionNotice(title: Words.actionRefused(action), message: Words.actionRefusal(result.code, result.message))
            }
        } catch {
            notice = .unanswered
        }
        await refresh()
    }

    private func save(_ change: ShowChoice) async {
        do {
            let result = switch change {
            case .sound(let showPrefix, let preset): try await model.client.setSound(showPrefix, preset)
            case .stillAiring(let seriesPrefix, let mode): try await model.client.setStillAiring(seriesPrefix, mode)
            }
            if !result.ok { notice = .unanswered }
        } catch {
            notice = .unanswered
        }
        await refresh()
    }
}

enum ItemAction {
    case makeAgain, keepThisCopy, putOriginalBack, allowLarger, keepOriginal
}

/// A step on the whole season or title, as its page state offers it.
enum WorkAction: Identifiable {
    case startCompressing, checkFiles, replaceOriginals, retryUnfinished, keepMotionOriginals
    var id: Self { self }
}

struct WorkActions: View {
    let actions: [SampleFacts.Action]
    let working: Bool
    let perform: (SampleFacts.Action) -> Void

    var body: some View {
        HStack(spacing: 8) {
            ForEach(actions.filter { $0 != .tryAnotherSize && $0 != .makeAgain }, id: \.self) { action in
                Button(Words.workAction(action)) { perform(action) }
                    .buttonStyle(.borderedProminent)
            }
        }
        .disabled(working)
    }
}

enum ShowChoice {
    case sound(String, String)
    case stillAiring(String, String)
}

struct ItemHeader: View {
    let facts: ItemFacts
    let poster: NSImage?
    // Opens the show's own page from a season's page.
    let openShow: (String) -> Void

    /// The show this page is a season of; nil on the show's own page or for a movie or folder.
    private var seasonsShow: String? {
        facts.kind == "tv_series" ? nil : facts.showPrefix
    }

    var body: some View {
        HStack(alignment: .bottom, spacing: 18) {
            if let poster {
                Image(nsImage: poster)
                    .resizable()
                    .aspectRatio(contentMode: .fill)
                    .frame(width: 84, height: 126)
                    .clipShape(RoundedRectangle(cornerRadius: 6, style: .continuous))
                    .shadow(color: .black.opacity(0.18), radius: 5, y: 2)
                    // On a season's page the show's poster opens the show, as its name does.
                    .onTapGesture { if let showPrefix = seasonsShow { openShow(showPrefix) } }
                    .pointerStyle(seasonsShow == nil ? nil : .link)
            }
            VStack(alignment: .leading, spacing: 5) {
                // The title bar names the page; on a season's page the show's name here opens the show.
                if let show = facts.show, let showPrefix = seasonsShow {
                    Button(show) { openShow(showPrefix) }
                        .buttonStyle(QuietLinkStyle())
                        .font(.title3)
                }
                Text(Words.beforeAfter(facts.originalBytes, facts.bytes))
                    .font(.system(size: 17, weight: .semibold))
                    .monospacedDigit()
                Text(Words.itemSummary(facts))
                    .foregroundStyle(.secondary)
            }
            Spacer(minLength: 0)
        }
    }
}

/// The mark a file waiting for the Director carries, in its line above the player and in the list.
struct WaitingMark: View {
    var body: some View {
        Image(systemName: "exclamationmark.circle.fill")
            .foregroundStyle(Color.accentColor)
            .accessibilityLabel(Words.waitingForYou)
    }
}

/// Each file waiting for the Director as one plain line, the way a kind's decision reads, with its buttons beside it.
struct WaitingFiles: View {
    let files: [ItemFacts.File]
    let act: (ItemFacts.File, ItemAction) -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            ForEach(files) { file in
                HStack(alignment: .firstTextBaseline, spacing: 8) {
                    WaitingMark()
                    Text("\(Words.fileName(file)): \(Words.waitingReason(file))")
                        .fixedSize(horizontal: false, vertical: true)
                    Spacer(minLength: 12)
                    HStack(spacing: 8) {
                        if let question = file.sizeQuestion {
                            Button(Words.keepTheOriginal) { act(file, .keepOriginal) }
                            Button(Words.allowLarger(question)) { act(file, .allowLarger) }
                        } else if file.validation == "size_held" {
                            Button(Words.keepThisCopy) { act(file, .keepThisCopy) }
                            Button(Words.makeItAgain) { act(file, .makeAgain) }
                        }
                    }
                    .fixedSize()
                }
            }
        }
    }
}

struct FileList: View {
    let files: [ItemFacts.File]
    @Binding var selected: ItemFacts.File.ID?

    var body: some View {
        List(files, selection: $selected) { file in
            HStack(alignment: .firstTextBaseline, spacing: 8) {
                VStack(alignment: .leading, spacing: 2) {
                    HStack(spacing: 5) {
                        if file.needsYou { WaitingMark() }
                        Text(Words.fileName(file))
                    }
                    if !file.compressed {
                        Text(Words.fileState(file))
                            .font(.callout)
                            .foregroundStyle(.secondary)
                    }
                }
                Spacer()
                Text(file.originalBytes.map { Words.beforeAfter($0, file.bytes) } ?? Words.size(file.bytes))
                    .monospacedDigit()
                    .foregroundStyle(.secondary)
            }
            .padding(.vertical, 2)
        }
        .listStyle(.inset)
    }
}

/// One file in the shared player, with its sizes under it and Put Original Back beside them. With no original
/// backup left, the compressed file plays alone.
struct FilePlayer: View {
    let file: ItemFacts.File
    let model: MainWindowModel
    let putOriginalBack: () -> Void
    @State private var players = ComparePlayers()
    @State private var confirming = false

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            ComparePlayer(players: players, model: model, gettingReady: Words.gettingReady(file), couldNotPlay: Words.couldNotPlay(file))
            HStack {
                Text(Words.playerCaption(file, comparing: players.comparing))
                    .foregroundStyle(.secondary)
                    .monospacedDigit()
                Spacer()
                if file.originalBackup {
                    Button(Words.putOriginalBack) { confirming = true }
                }
            }
        }
        .task { players.load(file, client: model.client) }
        .onDisappear { players.pause() }
        .confirmationDialog(Words.putOriginalBackQuestion(file), isPresented: $confirming) {
            Button(Words.putOriginalBack) { putOriginalBack() }
            Button(Words.cancel, role: .cancel) {}
        } message: {
            Text(Words.putOriginalBackDetail)
        }
    }
}

struct ShowChoices: View {
    let facts: ItemFacts
    // On a season's page the heading names the show, so it opens the show's page.
    let openShow: (String) -> Void
    let save: (ShowChoice) -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            Group {
                if facts.kind != "tv_series", let showPrefix = facts.showPrefix {
                    Button(Words.forEverySeason(facts.show ?? facts.title)) { openShow(showPrefix) }
                        .buttonStyle(QuietLinkStyle(primary: true))
                } else {
                    Text(Words.forEverySeason(facts.show ?? facts.title))
                }
            }
            .font(.headline)
            Form {
                if let sound = facts.sound, let showPrefix = sound.showPrefix {
                    Picker(Words.soundLabel, selection: Binding(
                        get: { sound.current ?? "" },
                        set: { save(.sound(showPrefix, $0)) }
                    )) {
                        ForEach(sound.options, id: \.self) { Text(Words.soundName($0)).tag($0) }
                    }
                    Text(Words.soundExplanation(sound.current))
                        .font(.callout)
                        .foregroundStyle(.secondary)
                }
                if let airing = facts.stillAiring {
                    Picker(Words.stillAiringLabel, selection: Binding(
                        get: { airing.mode },
                        set: { save(.stillAiring(airing.seriesPrefix, $0)) }
                    )) {
                        ForEach(Words.stillAiringModes, id: \.self) { Text(Words.stillAiringName($0)).tag($0) }
                    }
                    Text(Words.stillAiringExplanation(airing))
                        .font(.callout)
                        .foregroundStyle(.secondary)
                }
            }
            .formStyle(.grouped)
            .scrollDisabled(true)
            .padding(.horizontal, -20)
        }
    }
}
