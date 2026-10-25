import AVKit
import SwiftUI

/// The page for one show season, movie or folder: the player leads, every file sits beside it, and
/// anything waiting for the Director is answered right above the player.
struct ItemPage: View {
    let prefix: String
    let model: MainWindowModel
    @State private var facts: ItemFacts?
    @State private var refreshFailed = false
    @State private var selected: ItemFacts.File.ID?
    @State private var notice: ActionNotice?

    var body: some View {
        Group {
            if let facts {
                HStack(alignment: .top, spacing: 0) {
                    ScrollView {
                        VStack(alignment: .leading, spacing: 18) {
                            ItemHeader(facts: facts, poster: facts.showPrefix.flatMap(model.posters.image(for:)))
                            if !facts.waiting.isEmpty {
                                WaitingBox(files: facts.waiting) { file, action in
                                    Task { await act(action, file) }
                                }
                            }
                            if let file = selectedFile(facts) {
                                ItemPlayer(file: file, client: model.client) {
                                    Task { await act(.putOriginalBack, file) }
                                }
                                .id(file.id)
                            }
                            if facts.sound != nil || facts.stillAiring != nil {
                                ShowChoices(facts: facts) { change in Task { await save(change) } }
                            }
                        }
                        .padding(.horizontal, 28)
                        .padding(.top, 6)
                        .padding(.bottom, 24)
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
        .task(id: prefix) { await watch() }
        .alert(item: $notice) { notice in
            Alert(title: Text(notice.title), message: Text(notice.message))
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
    }

    private func act(_ action: ItemAction, _ file: ItemFacts.File) async {
        do {
            let client = model.client
            let result = switch action {
            case .makeAgain: try await client.makeAgain(prefix, file.libraryItemId)
            case .keepThisCopy: try await client.keepThisCopy(prefix, file.libraryItemId)
            case .putOriginalBack: try await client.putOriginalBack(prefix, file.libraryItemId)
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
    case makeAgain, keepThisCopy, putOriginalBack
}

enum ShowChoice {
    case sound(String, String)
    case stillAiring(String, String)
}

struct ItemHeader: View {
    let facts: ItemFacts
    let poster: NSImage?

    var body: some View {
        HStack(alignment: .bottom, spacing: 18) {
            if let poster {
                Image(nsImage: poster)
                    .resizable()
                    .aspectRatio(contentMode: .fill)
                    .frame(width: 84, height: 126)
                    .clipShape(RoundedRectangle(cornerRadius: 6, style: .continuous))
                    .shadow(color: .black.opacity(0.18), radius: 5, y: 2)
            }
            VStack(alignment: .leading, spacing: 5) {
                if let show = facts.show {
                    Text(show)
                        .font(.title3)
                        .foregroundStyle(.secondary)
                }
                Text(Words.itemTitle(facts))
                    .font(.system(size: 30, weight: .bold))
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
                    if file.validation == "size_held" {
                        Button(Words.keepThisCopy) { act(file, .keepThisCopy) }
                    }
                    if file.validation != nil {
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
                    if players.preparing {
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
    private(set) var position: Double = 0
    private(set) var duration: Double = 0
    private var observer: Any?
    private var readiness: NSKeyValueObservation?

    init() {
        original.isMuted = true
    }

    func load(_ file: ItemFacts.File, client: MediaforceClient) {
        comparing = file.compressed && file.originalBackup
        // The compressed file plays when there is one; a file not compressed yet plays as it is.
        main.replaceCurrentItem(with: AVPlayerItem(url: client.player(file.libraryItemId, file.compressed ? .compressed : .original)))
        original.replaceCurrentItem(with: comparing ? AVPlayerItem(url: client.player(file.libraryItemId, .original)) : nil)
        readiness = main.currentItem?.observe(\.status, options: [.initial, .new]) { [weak self] item, _ in
            let duration = item.duration.seconds
            let ready = item.status == .readyToPlay
            Task { @MainActor in
                guard let self, ready, self.preparing else { return }
                self.preparing = false
                self.duration = duration.isFinite ? duration : 0
                // Open on a moment a quarter of the way in, so there is a picture to compare before playing.
                self.seek(to: self.duration / 4)
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
        original.seek(to: main.currentTime(), toleranceBefore: .zero, toleranceAfter: .zero)
        main.play()
        if comparing { original.play() }
        playing = true
    }

    func pause() {
        main.pause()
        original.pause()
        playing = false
    }

    func seek(to seconds: Double) {
        let time = CMTime(seconds: seconds, preferredTimescale: 600)
        main.seek(to: time, toleranceBefore: .zero, toleranceAfter: .zero)
        if comparing { original.seek(to: time, toleranceBefore: .zero, toleranceAfter: .zero) }
        position = seconds
    }

    private func tick(_ time: CMTime) {
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
