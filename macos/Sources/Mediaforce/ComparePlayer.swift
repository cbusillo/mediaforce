import AVKit
import SwiftUI

enum CompareLayout: String, CaseIterable, Identifiable {
    case sideBySide, wipe, oneAtATime
    var id: String { rawValue }
}

/// The original and the compressed picture on one clock: side by side, a wipe dragged across one picture, or one at
/// a time. Moments to jump between, whose sound to hear, frame stepping, actual size and full screen. Every page that
/// compares an original with its compressed copy uses this one player; with no original left, the copy plays alone.
struct ComparePlayer: View {
    // Where each moment falls, as a share of the episode.
    static let moments = [0.2, 0.5, 0.8]

    /// What the moment buttons jump between: points in one whole episode, or a sample's separate clips.
    enum Moments: Equatable {
        case episode
        case clips([SampleFacts.Moment])
    }

    let players: ComparePlayers
    let model: MainWindowModel
    var moments = Moments.episode
    var names = PictureNames()
    var gettingReady = Words.gettingEpisodeReady
    var couldNotPlay = Words.couldNotPlayEpisode
    @AppStorage("kindPlayerLayout") private var layout = CompareLayout.wipe
    @State private var view = ComparePictureView()
    @State private var clip = 0
    @Environment(\.openWindow) private var openWindow

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            ComparePicture(players: players, layout: layout, names: clipNames, gettingReady: gettingReady,
                           couldNotPlay: couldNotPlay, view: $view)
            HStack(spacing: 10) {
                Button(players.playing ? Words.pause : Words.play, systemImage: players.playing ? "pause.fill" : "play.fill") {
                    players.toggle()
                }
                .keyboardShortcut(.space, modifiers: [])
                Button(Words.backOneFrame, systemImage: "backward.frame") { players.step(by: -1) }
                    .keyboardShortcut(",", modifiers: [])
                    .help(Words.backOneFrameHelp)
                Button(Words.forwardOneFrame, systemImage: "forward.frame") { players.step(by: 1) }
                    .keyboardShortcut(".", modifiers: [])
                    .help(Words.forwardOneFrameHelp)
                Slider(value: Binding(get: { players.position }, set: { players.seek(to: $0) }), in: 0...max(players.duration, 1))
                Text(Words.playTime(players.position, players.duration))
                    .monospacedDigit()
                    .foregroundStyle(.secondary)
                Button(Words.fullScreen, systemImage: "arrow.up.left.and.arrow.down.right") {
                    model.theater = Theater(players: players, layout: layout, names: clipNames)
                    openWindow(id: TheaterWindow.id)
                }
                .keyboardShortcut("f", modifiers: [])
                .help(Words.fullScreenHelp)
            }
            .labelStyle(.iconOnly)
            .disabled(players.preparing || players.failed)
            HStack(spacing: 14) {
                if players.comparing {
                    Picker(Words.viewLabel, selection: $layout) {
                        ForEach(CompareLayout.allCases) { Text(Words.playerLayout($0)).tag($0) }
                    }
                    .pickerStyle(.segmented)
                    .labelsHidden()
                    .fixedSize()
                }
                momentButtons
                if players.comparing {
                    Picker(Words.hear, selection: Bindable(players).hearOriginal) {
                        Text(names.original).tag(true)
                        Text(names.compressed).tag(false)
                    }
                    .pickerStyle(.segmented)
                    .fixedSize()
                }
                Spacer()
                Toggle(Words.actualSize, isOn: $view.actualSize)
                    .toggleStyle(.button)
                    .help(Words.actualSizeHelp)
            }
            .controlSize(.small)
            .disabled(players.preparing || players.failed)
        }
        .task(id: moments) {
            guard case .clips(let clips) = moments, !clips.isEmpty else { return }
            clip = min(clip, clips.count - 1)
            load(clips[clip])
        }
        .onReceive(NotificationCenter.default.publisher(for: AVPlayerItem.didPlayToEndTimeNotification).receive(on: RunLoop.main)) { note in
            if (note.object as? AVPlayerItem) === players.main.currentItem { players.pause() }
        }
    }

    // Buttons, not a selection, so the moment the picture is nearest to can be jumped back to.
    private var momentButtons: some View {
        HStack(spacing: 4) {
            Text(Words.momentChoice)
            switch moments {
            case .episode:
                ForEach(Self.moments.indices, id: \.self) { index in
                    let at = (players.duration * Self.moments[index]).rounded(.down)
                    Button(Words.momentTime(at)) { players.seek(to: at) }
                        .buttonStyle(.bordered)
                        .tint(index == currentMoment ? .accentColor : nil)
                }
            case .clips(let clips):
                ForEach(clips.indices, id: \.self) { index in
                    Button(Words.momentTime(clips[index].atSeconds)) {
                        if index == clip { players.seek(to: 0) } else { clip = index; load(clips[index]) }
                    }
                    .buttonStyle(.bordered)
                    .tint(index == clip ? .accentColor : nil)
                }
            }
        }
    }

    /// A sample's clips each carry their own size, so the frame labels say it.
    private var clipNames: PictureNames {
        guard case .clips(let clips) = moments, clips.indices.contains(clip) else { return names }
        return PictureNames(original: names.original, compressed: names.compressed,
                            originalDetail: Words.clipSize(clips[clip].original),
                            compressedDetail: Words.clipSize(clips[clip].sample))
    }

    private func load(_ moment: SampleFacts.Moment) {
        players.opensAt = 0
        players.load(compressed: model.client.reviewMedia(moment.sample.url), original: model.client.reviewMedia(moment.original.url))
    }

    // The moment the picture is nearest to, so the choice follows the timeline.
    private var currentMoment: Int {
        guard players.duration > 0 else { return 0 }
        let share = players.position / players.duration
        return Self.moments.indices.min { abs(Self.moments[$0] - share) < abs(Self.moments[$1] - share) } ?? 0
    }
}

/// What each side is called on the picture and in the sound choice, with a size when a clip has one.
struct PictureNames: Equatable {
    var original = Words.original
    var compressed = Words.compressed
    var originalDetail: String?
    var compressedDetail: String?

    var originalLabel: String { [original, originalDetail].compactMap { $0 }.joined(separator: ", ") }
    var compressedLabel: String { [compressed, compressedDetail].compactMap { $0 }.joined(separator: ", ") }
}

/// How the picture is looked at: where the wipe sits, which side shows one at a time, and actual size.
struct ComparePictureView: Equatable {
    var split: CGFloat = 0.5
    var showingOriginal = false
    var actualSize = false
}

/// The picture itself in the chosen layout. In actual size every pixel shows, and both sides pan together.
struct ComparePicture: View {
    let players: ComparePlayers
    let layout: CompareLayout
    let names: PictureNames
    var gettingReady = Words.gettingEpisodeReady
    var couldNotPlay = Words.couldNotPlayEpisode
    @Binding var view: ComparePictureView
    @State private var pan = ScrollPosition()
    @State private var otherPan = ScrollPosition()
    // Where the picture was last panned to, by either side.
    @State private var panned = CGPoint.zero

    var body: some View {
        Group {
            if !players.comparing {
                framed(nil) { pannable(position: $pan, follows: nil) { PlayerSurface(player: players.main) } }
            } else {
                switch layout {
                case .sideBySide:
                    HStack(spacing: 8) {
                        framed(names.originalLabel) { pannable(position: $pan, follows: $otherPan) { PlayerSurface(player: players.original) } }
                        framed(names.compressedLabel) { pannable(position: $otherPan, follows: $pan) { PlayerSurface(player: players.main) } }
                    }
                case .wipe:
                    framed(nil) { pannable(position: $pan, follows: nil) { wipe } }
                case .oneAtATime:
                    framed(view.showingOriginal ? names.originalLabel : names.compressedLabel) {
                        pannable(position: $pan, follows: nil) {
                            PlayerSurface(player: view.showingOriginal ? players.original : players.main)
                        }
                    }
                    .onTapGesture { view.showingOriginal.toggle() }
                    .help(Words.oneAtATimeHelp(names.compressed))
                }
            }
        }
        .overlay {
            if players.failed {
                Text(couldNotPlay).foregroundStyle(.white)
            } else if players.preparing {
                ProgressView(gettingReady)
                    .padding(12)
                    .background(.regularMaterial, in: RoundedRectangle(cornerRadius: 8))
            }
        }
    }

    private var wipe: some View {
        GeometryReader { geometry in
            let width = geometry.size.width
            ZStack(alignment: .leading) {
                PlayerSurface(player: players.main)
                PlayerSurface(player: players.original)
                    .mask(alignment: .leading) { Rectangle().frame(width: width * view.split) }
                Rectangle().fill(.white).frame(width: 2).offset(x: width * view.split - 1).shadow(radius: 2)
                Circle().fill(.white).frame(width: 22, height: 22)
                    .overlay(Image(systemName: "arrow.left.and.right").font(.system(size: 10, weight: .bold)).foregroundStyle(.black))
                    .offset(x: width * view.split - 11)
                HStack {
                    FrameLabel(text: names.originalLabel)
                    Spacer()
                    FrameLabel(text: names.compressedLabel)
                }
                .padding(10)
                .frame(maxHeight: .infinity, alignment: .top)
            }
            .contentShape(Rectangle())
            .gesture(DragGesture(minimumDistance: 0).onChanged { value in
                view.split = min(max(value.location.x / width, 0), 1)
            })
        }
    }

    private func framed(_ label: String?, @ViewBuilder _ content: () -> some View) -> some View {
        content()
            .overlay(alignment: .topLeading) {
                if let label { FrameLabel(text: label).padding(10) }
            }
            .aspectRatio(16 / 9, contentMode: .fit)
            .frame(maxWidth: .infinity)
            .background(Color.black)
            .clipShape(RoundedRectangle(cornerRadius: 8, style: .continuous))
    }

    /// Fits the picture to its frame, or in actual size shows it pixel for pixel in a view that pans; a second
    /// view following it keeps both sides on the same spot.
    @ViewBuilder
    private func pannable(position: Binding<ScrollPosition>, follows: Binding<ScrollPosition>?,
                          @ViewBuilder _ content: () -> some View) -> some View {
        if view.actualSize, players.videoSize.width > 0 {
            ScrollView([.horizontal, .vertical]) {
                content().frame(width: players.videoSize.width, height: players.videoSize.height)
            }
            .scrollPosition(position)
            .defaultScrollAnchor(.center)
            .onScrollGeometryChange(for: CGPoint.self) { $0.contentOffset } action: { _, offset in
                // The follower reports the spot it was moved to; ignoring that echo keeps a scroll's momentum.
                guard abs(offset.x - panned.x) > 0.5 || abs(offset.y - panned.y) > 0.5 else { return }
                panned = offset
                follows?.wrappedValue.scrollTo(point: offset)
            }
            .scrollIndicators(.never)
        } else {
            content()
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

struct PlayerSurface: NSViewRepresentable {
    let player: AVPlayer

    func makeNSView(context: Context) -> AVPlayerView {
        let view = AVPlayerView()
        view.controlsStyle = .none
        view.player = player
        return view
    }

    func updateNSView(_ view: AVPlayerView, context: Context) {
        view.player = player
    }
}

/// What the full-screen player shows: the page's own players, so it continues where the page was.
struct Theater {
    let players: ComparePlayers
    let layout: CompareLayout
    let names: PictureNames
}

/// The player alone, full screen. Escape or closing it returns to the page, still on the same moment.
struct TheaterWindow: View {
    static let id = "theater"

    let model: MainWindowModel
    @State private var view = ComparePictureView()
    @Environment(\.dismissWindow) private var dismissWindow

    var body: some View {
        ZStack {
            Color.black
            if let theater = model.theater {
                ComparePicture(players: theater.players, layout: theater.layout, names: theater.names, view: $view)
                    .padding(theater.layout == .sideBySide && theater.players.comparing ? 12 : 0)
                Button(Words.pause) { theater.players.toggle() }
                    .keyboardShortcut(.space, modifiers: [])
                    .hidden()
            }
            Button(Words.exitFullScreen) { dismissWindow(id: Self.id) }
                .keyboardShortcut(.cancelAction)
                .hidden()
        }
        .background(FullScreenOnOpen())
        .onDisappear { model.theater = nil }
    }
}

/// Puts its window into full screen as soon as the window exists.
struct FullScreenOnOpen: NSViewRepresentable {
    func makeNSView(context: Context) -> NSView { Opener() }
    func updateNSView(_ view: NSView, context: Context) {}

    final class Opener: NSView {
        override func viewDidMoveToWindow() {
            super.viewDidMoveToWindow()
            guard let window, !window.styleMask.contains(.fullScreen) else { return }
            DispatchQueue.main.async { window.toggleFullScreen(nil) }
        }
    }
}

/// The two players behind the picture, kept on one clock.
@MainActor
@Observable
final class ComparePlayers {
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
    // Where the picture opens, as a share of its length, so there is a moment to compare before playing.
    var opensAt = ComparePlayer.moments[0]
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
        // The compressed file plays when there is one; a file not compressed yet plays as it is.
        load(compressed: client.player(libraryItemId, compressed ? .compressed : .original),
             original: compressed && originalBackup ? client.player(libraryItemId, .original) : nil)
    }

    /// Loads the compressed picture and, when there is one to compare with, its original.
    func load(compressed: URL, original originalURL: URL?) {
        pause()
        comparing = originalURL != nil
        preparing = true
        failed = false
        position = 0
        duration = 0
        videoSize = .zero
        main.replaceCurrentItem(with: AVPlayerItem(url: compressed))
        original.replaceCurrentItem(with: originalURL.map { AVPlayerItem(url: $0) })
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
        // Played to the end, it starts over; otherwise from the position the Director last chose, not wherever an
        // unfinished seek has got to.
        if duration > 0, position >= duration - 0.25 { position = 0 }
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
