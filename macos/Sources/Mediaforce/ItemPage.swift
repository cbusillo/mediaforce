import AVKit
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
    @State private var browserNotice: ActionNotice?
    @State private var working = false
    @Environment(\.openURL) private var openURL

    var body: some View {
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
                                WaitingBox(files: facts.waiting) { file, action in
                                    Task { await act(action, file) }
                                }
                            }
                            if let file = selectedFile(facts) {
                                ItemPlayer(file: file, client: model.client) {
                                    Task { await act(.putOriginalBack, file) }
                                }
                                // A file that is published, put back or loses its backup plays again from the start.
                                .id("\(file.id) \(file.stage) \(file.bytes) \(file.originalBackup)")
                            }
                            if facts.sound != nil || facts.stillAiring != nil {
                                ShowChoices(facts: facts) { change in Task { await save(change) } }
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
        .background(Color(nsColor: .textBackgroundColor))
        // Until its facts arrive, the page is named by its folder.
        .navigationTitle(facts.map(Words.itemTitle) ?? prefix.split(separator: "/").last.map(String.init) ?? prefix)
        // A season names its show; a show's own page is already named by it.
        .navigationSubtitle(facts.flatMap { $0.kind == "tv_series" ? nil : $0.show } ?? "")
        .task(id: prefix) { await watch() }
        .alert(item: $notice) { notice in
            Alert(title: Text(notice.title), message: Text(notice.message))
        }
        .alert(
            browserNotice?.title ?? "",
            isPresented: Binding(get: { browserNotice != nil }, set: { if !$0 { browserNotice = nil } }),
            presenting: browserNotice
        ) { _ in
            Button(Words.openInBrowser) { openURL(model.client.seasonPage(prefix)) }
            Button(Words.cancel, role: .cancel) {}
        } message: { notice in
            Text(notice.message)
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

    private func perform(_ action: SampleFacts.Action, _ facts: ItemFacts) {
        switch action {
        case .approve, .makeAgain, .makeSample, .chooseSize, .tryAnotherSize: openSample(prefix)
        case .openInBrowser: openURL(model.client.seasonPage(prefix))
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
            if let result = try await send(action, facts), !result.ok {
                notice = ActionNotice(title: Words.workRefused(action), message: result.message ?? ActionNotice.sampleRefused.message)
            }
        } catch {
            notice = .unanswered
        }
        await refresh()
    }

    /// The server's answer, or nil when the step needs a choice made on the web page first.
    private func send(_ action: WorkAction, _ facts: ItemFacts?) async throws -> ItemActionResult? {
        let client = model.client
        switch action {
        case .startCompressing:
            // Started only when it says it is compressing; some reasons need a choice made on the web page.
            let started = try await client.compressSeason(prefix)
            if started.needsBrowser {
                browserNotice = .finishInBrowser
                return nil
            }
            return started.compressing == true ? .done : .refused(ActionNotice.approvedNotCompressing.message)
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
        work = try? await model.client.sample(prefix)
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

/// The mark a file waiting for the Director carries, in the box above the player and in the list.
struct WaitingMark: View {
    var body: some View {
        Image(systemName: "exclamationmark.circle.fill")
            .foregroundStyle(Color.accentColor)
            .accessibilityLabel(Words.waitingForYou)
    }
}

struct WaitingBox: View {
    let files: [ItemFacts.File]
    let act: (ItemFacts.File, ItemAction) -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            ForEach(files) { file in
                HStack(spacing: 12) {
                    WaitingMark()
                        .font(.title3)
                    VStack(alignment: .leading, spacing: 2) {
                        Text(Words.fileName(file))
                            .font(.headline)
                        Text(Words.waitingReason(file))
                            .foregroundStyle(.secondary)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                    Spacer()
                    if let question = file.sizeQuestion {
                        Button(Words.keepTheOriginal) { act(file, .keepOriginal) }
                        Button(Words.allowLarger(question)) { act(file, .allowLarger) }
                    } else if file.validation == "size_held" {
                        Button(Words.keepThisCopy) { act(file, .keepThisCopy) }
                        Button(Words.makeItAgain) { act(file, .makeAgain) }
                    }
                }
            }
        }
        .padding(12)
        .background(Color.accentColor.opacity(0.08), in: RoundedRectangle(cornerRadius: 8, style: .continuous))
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

/// Original on the left, compressed on the right, with a divider the Director drags. With no original
/// backup left, the compressed file plays alone.
struct ItemPlayer: View {
    let file: ItemFacts.File
    let client: MediaforceClient
    let putOriginalBack: () -> Void
    @State private var players = ItemPlayers()
    @State private var split: CGFloat = 0.5
    @State private var confirming = false

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            GeometryReader { geometry in
                let width = geometry.size.width
                ZStack(alignment: .leading) {
                    PlayerSurface(player: players.main)
                    if players.comparing {
                        PlayerSurface(player: players.original)
                            .mask(alignment: .leading) { Rectangle().frame(width: width * split) }
                        Rectangle()
                            .fill(.white)
                            .frame(width: 2)
                            .offset(x: width * split - 1)
                            .shadow(radius: 2)
                        HStack {
                            FrameLabel(text: Words.original)
                            Spacer()
                            FrameLabel(text: Words.compressed)
                        }
                        .padding(10)
                        .frame(maxHeight: .infinity, alignment: .top)
                    }
                    if players.failed {
                        Text(Words.couldNotPlay(file))
                            .foregroundStyle(.white)
                            .frame(maxWidth: .infinity)
                    } else if players.preparing {
                        ProgressView(Words.gettingReady(file))
                            .padding(12)
                            .background(.regularMaterial, in: RoundedRectangle(cornerRadius: 8))
                            .frame(maxWidth: .infinity)
                    }
                }
                .contentShape(Rectangle())
                .gesture(DragGesture(minimumDistance: 0).onChanged { value in
                    if players.comparing { split = min(max(value.location.x / width, 0), 1) }
                })
            }
            .aspectRatio(16 / 9, contentMode: .fit)
            .background(Color.black)
            .clipShape(RoundedRectangle(cornerRadius: 8, style: .continuous))
            HStack(spacing: 10) {
                Button(players.playing ? Words.pause : Words.play, systemImage: players.playing ? "pause.fill" : "play.fill") {
                    players.toggle()
                }
                .keyboardShortcut(.space, modifiers: [])
                Slider(value: Binding(get: { players.position }, set: { players.seek(to: $0) }), in: 0...max(players.duration, 1))
                Text(Words.playTime(players.position, players.duration))
                    .monospacedDigit()
                    .foregroundStyle(.secondary)
            }
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
        .task { players.load(file, client: client) }
        .onDisappear { players.pause() }
        .confirmationDialog(Words.putOriginalBackQuestion(file), isPresented: $confirming) {
            Button(Words.putOriginalBack) { putOriginalBack() }
            Button(Words.cancel, role: .cancel) {}
        } message: {
            Text(Words.putOriginalBackDetail)
        }
    }
}

struct FrameLabel: View {
    let text: String

    var body: some View {
        Text(text)
            .font(.caption.weight(.semibold))
            .foregroundStyle(.white)
            .padding(.horizontal, 7)
            .padding(.vertical, 3)
            .background(.black.opacity(0.55), in: RoundedRectangle(cornerRadius: 4))
    }
}

@MainActor
@Observable
final class ItemPlayers {
    let main = AVPlayer()
    let original = AVPlayer()
    private(set) var comparing = false
    private(set) var playing = false
    private(set) var preparing = true
    private(set) var failed = false
    private(set) var position: Double = 0
    private(set) var duration: Double = 0
    // The picture's own size in pixels, once the compressed side is ready; zero until then.
    private(set) var videoSize: CGSize = .zero
    // Where the picture opens, as a share of the episode, so there is a moment to compare before playing.
    var opensAt = 0.25
    // Whose sound plays while comparing; the compressed side's unless the original's is chosen.
    var hearOriginal = false {
        didSet { applySound() }
    }
    private var observer: Any?
    private var readiness: [NSKeyValueObservation] = []
    private var ready: Set<ObjectIdentifier> = []
    private var requests = 0
    // While a seek settles, the clock's reports would overwrite the position the Director chose.
    private var settling: Int?

    init() {
        applySound()
    }

    func load(_ file: ItemFacts.File, client: MediaforceClient) {
        load(file.libraryItemId, compressed: file.compressed, originalBackup: file.originalBackup, client: client)
    }

    func load(_ libraryItemId: Int, compressed: Bool, originalBackup: Bool, client: MediaforceClient) {
        pause()
        comparing = compressed && originalBackup
        preparing = true
        failed = false
        position = 0
        duration = 0
        videoSize = .zero
        // The compressed file plays when there is one; a file not compressed yet plays as it is.
        main.replaceCurrentItem(with: AVPlayerItem(url: client.player(libraryItemId, compressed ? .compressed : .original)))
        original.replaceCurrentItem(with: comparing ? AVPlayerItem(url: client.player(libraryItemId, .original)) : nil)
        ready = []
        // Both sides must be ready before either plays, so the two pictures show the same moment.
        readiness = [main, original].compactMap { player in
            player.currentItem?.observe(\.status, options: [.initial, .new]) { [weak self] item, _ in
                let status = item.status
                let duration = item.duration.seconds
                let side = ObjectIdentifier(player)
                Task { @MainActor in
                    // A report from the episode before a switch arrives late; only the current one counts.
                    guard item === player.currentItem else { return }
                    self?.itemChanged(side, status: status, duration: duration)
                }
            }
        }
        if observer == nil {
            observer = main.addPeriodicTimeObserver(forInterval: CMTime(seconds: 0.5, preferredTimescale: 600), queue: .main) { [weak self] time in
                MainActor.assumeIsolated { self?.tick(time) }
            }
        }
    }

    func toggle() {
        if playing {
            pause()
            return
        }
        guard !preparing else { return }
        let request = nextRequest()
        // The position the Director last chose, not wherever an unfinished seek has got to.
        let time = CMTime(seconds: position, preferredTimescale: 600)
        playing = true
        Task { await play(from: time, request) }
    }

    func pause() {
        _ = nextRequest()
        main.pause()
        original.pause()
        playing = false
    }

    func seek(to seconds: Double) {
        let wasPlaying = playing
        pause()
        let request = nextRequest()
        let time = CMTime(seconds: seconds, preferredTimescale: 600)
        position = seconds
        playing = wasPlaying
        settling = request
        // Each seek or play supersedes the ones before it, so a late one never moves the picture back.
        Task {
            if wasPlaying {
                await play(from: time, request)
            } else {
                async let mainSeek = main.seek(to: time, toleranceBefore: .zero, toleranceAfter: .zero)
                async let originalSeek = comparing ? original.seek(to: time, toleranceBefore: .zero, toleranceAfter: .zero) : true
                _ = await (mainSeek, originalSeek)
            }
            if settling == request { settling = nil }
        }
    }

    /// One frame back or forward on both sides, paused, so the two pictures stay on the same frame.
    func step(by frames: Int) {
        let rate = main.currentItem?.tracks.map(\.currentVideoFrameRate).first { $0 > 0 } ?? 24
        pause()
        // Both sides go to one exact time, so a drift left from playing never survives a step.
        seek(to: min(max(main.currentTime().seconds + Double(frames) / Double(rate), 0), duration))
    }

    private func applySound() {
        main.isMuted = comparing && hearOriginal
        original.isMuted = !(comparing && hearOriginal)
    }

    private func play(from time: CMTime, _ request: Int) async {
        async let mainSeek = main.seek(to: time, toleranceBefore: .zero, toleranceAfter: .zero)
        async let originalSeek = comparing ? original.seek(to: time, toleranceBefore: .zero, toleranceAfter: .zero) : true
        _ = await (mainSeek, originalSeek)
        guard request == requests else { return }
        guard comparing else {
            main.play()
            return
        }
        // Start both at the same host time so neither runs ahead of the other.
        let start = CMClockGetTime(CMClockGetHostTimeClock()) + CMTime(seconds: 0.2, preferredTimescale: 600)
        main.automaticallyWaitsToMinimizeStalling = false
        original.automaticallyWaitsToMinimizeStalling = false
        main.setRate(1, time: time, atHostTime: start)
        original.setRate(1, time: time, atHostTime: start)
    }

    private func nextRequest() -> Int {
        requests += 1
        return requests
    }

    private func itemChanged(_ side: ObjectIdentifier, status: AVPlayerItem.Status, duration: Double) {
        if status == .failed {
            failed = true
            preparing = false
            return
        }
        guard status == .readyToPlay, preparing else { return }
        if side == ObjectIdentifier(main) {
            self.duration = duration.isFinite ? duration : 0
            videoSize = main.currentItem?.presentationSize ?? .zero
        }
        ready.insert(side)
        guard ready.count == (comparing ? 2 : 1) else { return }
        preparing = false
        applySound()
        seek(to: self.duration * opensAt)
    }

    private func tick(_ time: CMTime) {
        guard settling == nil else { return }
        position = time.seconds
    }
}

struct ShowChoices: View {
    let facts: ItemFacts
    let save: (ShowChoice) -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            Text(Words.forEverySeason(facts.show ?? facts.title))
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
