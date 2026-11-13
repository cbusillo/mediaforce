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
    @State private var scrubbing = false
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
                // Dragged, the pictures follow the scrubber; let go, or moved without a drag, they land on the exact frame.
                Slider(value: Binding(get: { players.position }, set: { scrubbing ? players.drag(to: $0) : players.seek(to: $0) }),
                       in: 0...max(players.duration, 1)) { editing in
                    scrubbing = editing
                    if !editing { players.endDrag() }
                }
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
            .disabled(unplayable)
            // One row when it fits; on a narrower page the sound and size choices drop to a second row.
            ViewThatFits(in: .horizontal) {
                HStack(spacing: 14) {
                    layoutPicker
                    momentButtons
                    hearPicker
                    Spacer(minLength: 0)
                    actualSize
                }
                VStack(alignment: .leading, spacing: 8) {
                    HStack(spacing: 14) {
                        layoutPicker
                        momentButtons
                    }
                    HStack(spacing: 14) {
                        hearPicker
                        Spacer(minLength: 0)
                        actualSize
                    }
                }
            }
            .controlSize(.small)
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

    @ViewBuilder
    private var layoutPicker: some View {
        if players.comparing {
            Picker(Words.viewLabel, selection: $layout) {
                ForEach(CompareLayout.allCases) { Text(Words.playerLayout($0)).tag($0) }
            }
            .pickerStyle(.segmented)
            .labelsHidden()
            .fixedSize()
            .disabled(unplayable)
        }
    }

    @ViewBuilder
    private var hearPicker: some View {
        if players.comparing {
            Picker(Words.hear, selection: Bindable(players).hearOriginal) {
                Text(names.original).tag(true)
                Text(names.compressed).tag(false)
            }
            .pickerStyle(.segmented)
            .fixedSize()
            .disabled(unplayable)
        }
    }

    private var actualSize: some View {
        Toggle(Words.actualSize, isOn: $view.actualSize)
            .toggleStyle(.button)
            .help(Words.actualSizeHelp)
            .fixedSize()
            .disabled(unplayable)
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
        .fixedSize()
        // A sample's other clips stay open to choose when one of them can't play.
        .disabled(moments == .episode && unplayable)
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

    private var unplayable: Bool { players.preparing || players.failed }

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
    @State private var pan = Pan()

    var body: some View {
        Group {
            if !players.comparing {
                framed(nil) { pannable { PlayerSurface(players: players, player: players.main) } }
            } else {
                switch layout {
                case .sideBySide:
                    HStack(spacing: 8) {
                        framed(names.originalLabel) { pannable { PlayerSurface(players: players, player: players.original) } }
                        framed(names.compressedLabel) { pannable { PlayerSurface(players: players, player: players.main) } }
                    }
                case .wipe:
                    framed(nil) { pannable { wipe } }
                case .oneAtATime:
                    framed(view.showingOriginal ? names.originalLabel : names.compressedLabel) {
                        pannable {
                            PlayerSurface(players: players, player: view.showingOriginal ? players.original : players.main)
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
                PlayerSurface(players: players, player: players.main)
                PlayerSurface(players: players, player: players.original)
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

    /// Fits the picture to its frame, or in actual size shows it pixel for pixel in a view that pans with every
    /// other view of the same picture.
    @ViewBuilder
    private func pannable(@ViewBuilder _ content: () -> some View) -> some View {
        if view.actualSize, players.videoSize.width > 0 {
            PanningPicture(size: players.videoSize, pan: pan, content: content())
        } else {
            content()
        }
    }
}

/// Where an actual-size picture is panned to. Every view of the picture joins one pan, and moving any of them moves
/// the rest in the same step, so side by side never shows two different spots.
@MainActor
final class Pan {
    private let views = NSHashTable<PanningScrollView>.weakObjects()
    private var origin: NSPoint?
    private var moving = false

    func join(_ view: PanningScrollView) {
        views.add(view)
        view.pan = self
    }

    /// A view that has just been laid out opens where the others are, or on the middle of the picture.
    func place(_ view: PanningScrollView) {
        move(view, to: origin ?? view.middle)
    }

    func moved(_ view: PanningScrollView) {
        // AppKit scrolls a view while setting it up; only a placed view says where the picture is.
        guard view.placed, !moving else { return }
        moving = true
        defer { moving = false }
        let spot = view.contentView.bounds.origin
        origin = spot
        for other in views.allObjects where other !== view {
            move(other, to: spot)
        }
    }

    private func move(_ view: PanningScrollView, to spot: NSPoint) {
        let clip = view.contentView
        clip.scroll(to: clip.constrainBoundsRect(NSRect(origin: spot, size: clip.bounds.size)).origin)
        view.reflectScrolledClipView(view.contentView)
    }
}

final class PanningScrollView: NSScrollView {
    weak var pan: Pan?
    private(set) var placed = false

    var middle: NSPoint {
        let picture = documentView?.frame.size ?? .zero
        let shown = contentView.bounds.size
        return NSPoint(x: max((picture.width - shown.width) / 2, 0), y: max((picture.height - shown.height) / 2, 0))
    }

    override func reflectScrolledClipView(_ clipView: NSClipView) {
        super.reflectScrolledClipView(clipView)
        pan?.moved(self)
    }

    override func layout() {
        super.layout()
        guard !placed, contentView.bounds.width > 0, (documentView?.frame.width ?? 0) > 0 else { return }
        placed = true
        pan?.place(self)
    }
}

/// The picture pixel for pixel in a view that pans by scrolling, joined to the page's pan.
struct PanningPicture<Content: View>: NSViewRepresentable {
    let size: CGSize
    let pan: Pan
    let content: Content

    func makeNSView(context: Context) -> PanningScrollView {
        let view = PanningScrollView()
        view.drawsBackground = false
        view.hasHorizontalScroller = false
        view.hasVerticalScroller = false
        view.documentView = NSHostingView(rootView: content)
        view.documentView?.setFrameSize(size)
        pan.join(view)
        return view
    }

    func updateNSView(_ view: PanningScrollView, context: Context) {
        (view.documentView as? NSHostingView<Content>)?.rootView = content
        if view.documentView?.frame.size != size {
            view.documentView?.setFrameSize(size)
        }
        if view.pan !== pan {
            pan.join(view)
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

/// One side's picture, joined to its players so every move holds it with the other side's.
struct PlayerSurface: NSViewRepresentable {
    let players: ComparePlayers
    let player: AVPlayer

    func makeNSView(context: Context) -> PictureView {
        let view = PictureView(player: player)
        players.join(view)
        return view
    }

    func updateNSView(_ view: PictureView, context: Context) {
        view.show(player)
        players.join(view)
    }
}

/// The player's own picture, with a still above it that keeps a frame, or blank, while the players move.
final class PictureView: NSView {
    private static let renderer = CIContext()
    private let still = CALayer()
    private var picture: AVPlayerLayer? { layer as? AVPlayerLayer }

    init(player: AVPlayer) {
        super.init(frame: .zero)
        layer = AVPlayerLayer(player: player)
        layer?.backgroundColor = NSColor.black.cgColor
        wantsLayer = true
        still.backgroundColor = NSColor.black.cgColor
        still.contentsGravity = .resizeAspect
        still.isHidden = true
        layer?.addSublayer(still)
    }

    @available(*, unavailable)
    required init?(coder: NSCoder) { nil }

    override func layout() {
        super.layout()
        CATransaction.begin()
        CATransaction.setDisableActions(true)
        still.frame = bounds
        CATransaction.commit()
    }

    func show(_ player: AVPlayer) {
        guard picture?.player !== player else { return }
        picture?.player = player
        // A frame kept from the other side would show it under this side's name.
        if !still.isHidden { hold(nil) }
    }

    /// The frame showing now, while paused.
    func frame() -> CGImage? {
        guard let buffer = picture?.displayedPixelBuffer() else { return nil }
        let image = CIImage(cvPixelBuffer: buffer)
        return Self.renderer.createCGImage(image, from: image.extent)
    }

    var holding: Bool { !still.isHidden }

    func hold(_ frame: CGImage?) {
        still.contents = frame
        still.isHidden = false
    }

    func release() {
        still.isHidden = true
        still.contents = nil
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

/// The two players behind the picture, kept on one clock. Every move to a new moment holds the pictures until both
/// sides are on the same frame, so the two are never seen showing different moments.
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
    // False while the sides move to a new moment: each picture keeps the frame it showed, or nothing while an
    // episode opens, until both sides are on the same frame.
    private(set) var pictureShown = false
    // Where the picture opens, as a share of its length, so there is a moment to compare before playing.
    var opensAt = ComparePlayer.moments[0]
    // Whose sound plays while comparing; the compressed side's unless the original's is chosen.
    var hearOriginal = false {
        didSet { applySound() }
    }
    private var observer: Any?
    private var readiness: [NSKeyValueObservation] = []
    // An opening waiting for a side to load.
    private var loading: [ObjectIdentifier: CheckedContinuation<Bool, Never>] = [:]
    private let pictures = NSHashTable<PictureView>.weakObjects()
    // Any newer action cancels a start of play that has not happened yet.
    private var requests = 0
    // Only the newest move shows the pictures again when it lands.
    private var lastMove = 0
    // While a seek settles, the clock's reports would overwrite the position the Director chose.
    private var settling: Int?
    private var drag: Drag?

    /// The scrubber being dragged: where it is wanted next, whether a seek is on its way, and whether to play again
    /// when it is let go.
    private struct Drag {
        var chase = Chase()
        let resume: Bool
    }

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

    /// Loads the compressed picture and, when there is one to compare with, its original, straight onto the opening
    /// moment.
    func load(compressed: URL, original originalURL: URL?) {
        pause()
        drag = nil
        comparing = originalURL != nil
        preparing = true
        failed = false
        position = 0
        duration = 0
        videoSize = .zero
        let move = beginMove(blank: true)
        main.replaceCurrentItem(with: nil)
        original.replaceCurrentItem(with: nil)
        readiness = []
        for waiting in loading.values { waiting.resume(returning: false) }
        loading = [:]
        if observer == nil {
            observer = main.addPeriodicTimeObserver(forInterval: CMTime(seconds: 0.5, preferredTimescale: 600), queue: .main) { [weak self] time in
                MainActor.assumeIsolated { self?.tick(time) }
            }
        }
        Task { await open(AVURLAsset(url: compressed), originalURL.map { AVURLAsset(url: $0) }, move) }
    }

    /// The episode's length comes from its playlist alone, and each side is asked for the opening moment before it
    /// reads any picture, so neither fetches or decodes the start first.
    private func open(_ compressed: AVURLAsset, _ originalAsset: AVURLAsset?, _ move: Int) async {
        let length = try? await compressed.load(.duration).seconds
        guard move == lastMove else { return }
        guard let length, length.isFinite, length > 0 else {
            failed = true
            preparing = false
            return
        }
        duration = length
        position = length * opensAt
        for (player, asset) in [(main, compressed), (original, originalAsset)] {
            player.replaceCurrentItem(with: asset.map(Self.item))
        }
        readiness = [main, original].compactMap { player in
            player.currentItem?.observe(\.status, options: [.initial, .new]) { [weak self] item, _ in
                let status = item.status
                let size = item.presentationSize
                Task { @MainActor in
                    // A report from the episode before a switch arrives late; only the current one counts.
                    guard item === player.currentItem else { return }
                    self?.itemChanged(player, status: status, size: size)
                }
            }
        }
        applySound()
        // Asked before a side loads, the seek makes its first read the opening moment's piece. It reports done at once
        // and is sometimes dropped, so once both have loaded, a side not on the moment is sent there again.
        let time = CMTime(seconds: position, preferredTimescale: 600)
        Task { await land(at: time, move, show: false) }
        async let mainLoaded = loaded(main)
        async let originalLoaded = loaded(original)
        guard await (mainLoaded, originalLoaded) == (true, true) else { return }
        async let mainThere = main.currentTime() == time ? true : seek(main, to: time, exact: true, move)
        async let originalThere = !comparing || original.currentTime() == time ? true : seek(original, to: time, exact: true, move)
        guard show(await (mainThere, originalThere) == (true, true), move) else { return }
        preparing = false
    }

    /// Waits until the player's current item has loaded or failed.
    private func loaded(_ player: AVPlayer) async -> Bool {
        guard let item = player.currentItem else { return true }
        switch item.status {
        case .readyToPlay: return true
        case .failed: return false
        default: return await withCheckedContinuation { loading[ObjectIdentifier(player)] = $0 }
        }
    }

    /// An episode streams from the server a few seconds at a time, so each side reads only what is near.
    private static func item(_ asset: AVURLAsset) -> AVPlayerItem {
        let item = AVPlayerItem(asset: asset)
        item.preferredForwardBufferDuration = 20
        return item
    }

    /// Every picture of these players joins them, so a move holds it with the rest.
    func join(_ picture: PictureView) {
        guard !pictures.contains(picture) else { return }
        pictures.add(picture)
        if !pictureShown { picture.hold(nil) }
    }

    func toggle() {
        if playing {
            pause()
            return
        }
        guard !preparing else { return }
        // Played to the end, it starts over; otherwise from the position the Director last chose, not wherever an
        // unfinished seek has got to.
        if duration > 0, position >= duration - 0.25 { position = 0 }
        playing = true
        seek(to: position)
    }

    func pause() {
        _ = nextRequest()
        main.pause()
        original.pause()
        playing = false
    }

    /// Both sides to one exact frame, then on playing if they were.
    func seek(to seconds: Double) {
        guard !preparing else { return }
        let wasPlaying = playing
        pause()
        drag = nil
        playing = wasPlaying
        position = seconds
        let time = CMTime(seconds: seconds, preferredTimescale: 600)
        let move = beginMove()
        let request = requests
        settling = move
        Task {
            // Each move supersedes the ones before it, so a late one never moves the picture back.
            if await land(at: time, move), wasPlaying, request == requests { start(at: time) }
            if settling == move { settling = nil }
        }
    }

    /// The scrubber is being dragged: the pictures follow it at the nearest frame each side can show quickly, one
    /// seek at a time, always to where the scrubber is now, never through a backlog of where it has been.
    func drag(to seconds: Double) {
        guard !preparing else { return }
        position = seconds
        if drag == nil {
            drag = Drag(resume: playing)
            pause()
        }
        guard let next = drag?.chase.want(seconds) else { return }
        Task { await follow(from: next) }
    }

    /// The scrubber is let go: both sides land on the exact frame there, and play on if they were playing.
    func endDrag() {
        guard let drag else { return }
        self.drag = nil
        playing = drag.resume
        seek(to: position)
    }

    private func follow(from first: Double) async {
        var next: Double? = first
        while let target = next, drag != nil {
            let move = beginMove()
            settling = move
            _ = await land(near: CMTime(seconds: target, preferredTimescale: 600), move)
            if settling == move { settling = nil }
            next = drag?.chase.landed()
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

    /// Start both at the same host time so neither runs ahead of the other.
    private func start(at time: CMTime) {
        guard comparing else {
            main.play()
            return
        }
        let start = CMClockGetTime(CMClockGetHostTimeClock()) + CMTime(seconds: 0.2, preferredTimescale: 600)
        main.automaticallyWaitsToMinimizeStalling = false
        original.automaticallyWaitsToMinimizeStalling = false
        main.setRate(1, time: time, atHostTime: start)
        original.setRate(1, time: time, atHostTime: start)
    }

    /// Holds every picture for a new move: on the frame it shows, or blank while an episode opens or when a frame
    /// cannot be kept. A move that supersedes one still landing keeps what that one held.
    private func beginMove(blank: Bool = false) -> Int {
        lastMove = nextRequest()
        guard pictureShown || blank else { return lastMove }
        pictureShown = false
        let views = pictures.allObjects
        let frames = blank ? [] : views.map { $0.frame() }
        let kept = frames.count == views.count && frames.allSatisfy { $0 != nil }
        CATransaction.begin()
        CATransaction.setDisableActions(true)
        for (index, view) in views.enumerated() { view.hold(kept ? frames[index] : nil) }
        CATransaction.commit()
        return lastMove
    }

    /// Both sides on the exact frame for the time; the pictures show again if this is still the newest move.
    private func land(at time: CMTime, _ move: Int, show showing: Bool = true) async -> Bool {
        async let mainSeek = seek(main, to: time, exact: true, move)
        async let originalSeek = comparing ? seek(original, to: time, exact: true, move) : true
        let landed = await (mainSeek, originalSeek) == (true, true)
        return showing ? show(landed, move) : landed
    }

    /// The compressed side to the nearest frame it can show quickly, then the original to that same frame.
    private func land(near time: CMTime, _ move: Int) async -> Bool {
        guard await seek(main, to: time, exact: false, move) else { return show(false, move) }
        let landed = !comparing ? true : await seek(original, to: main.currentTime(), exact: true, move)
        return show(landed, move)
    }

    private func show(_ landed: Bool, _ move: Int) -> Bool {
        guard landed, move == lastMove else { return false }
        pictureShown = true
        CATransaction.begin()
        CATransaction.setDisableActions(true)
        for view in pictures.allObjects { view.release() }
        CATransaction.commit()
        return true
    }

    private func seek(_ player: AVPlayer, to time: CMTime, exact: Bool, _ move: Int) async -> Bool {
        guard move == lastMove else { return false }
        let tolerance = exact ? CMTime.zero : .positiveInfinity
        return await player.seek(to: time, toleranceBefore: tolerance, toleranceAfter: tolerance)
    }

    private func nextRequest() -> Int {
        requests += 1
        return requests
    }

    private func itemChanged(_ player: AVPlayer, status: AVPlayerItem.Status, size: CGSize) {
        if status != .unknown { loading.removeValue(forKey: ObjectIdentifier(player))?.resume(returning: status == .readyToPlay) }
        if status == .failed {
            failed = true
            preparing = false
        } else if status == .readyToPlay, player === main {
            videoSize = size
        }
    }

    private func tick(_ time: CMTime) {
        guard settling == nil, drag == nil else { return }
        position = time.seconds
    }
}

/// Where a dragged scrubber wants the pictures, one seek at a time: a new place while a seek is on its way waits,
/// replacing any place that was waiting, so only the newest is ever sought next.
struct Chase: Equatable {
    private(set) var busy = false
    private(set) var waiting: Double?

    /// The place to seek now, or nil when a seek is on its way and the place waits for it.
    mutating func want(_ place: Double) -> Double? {
        guard busy else {
            busy = true
            return place
        }
        waiting = place
        return nil
    }

    /// The seek on its way has landed: the place to seek next, or nil when there is none.
    mutating func landed() -> Double? {
        defer { waiting = nil }
        busy = waiting != nil
        return waiting
    }
}
