import AVKit
import SwiftUI

/// One kind of content, opened from under "How it should look" in the sidebar: the player leads, the decision is one
/// line under it, and the shows the kind covers are the player's choices. The window's title names the kind.
/// A kind with no setting yet offers a sample of its biggest show instead, then shows that sample as it is made.
struct KindPage: View {
    let kind: KindsFacts.Kind
    let leftOut: Int
    let model: MainWindowModel
    let openItem: (String) -> Void
    let openSample: (String) -> Void
    let openLeftOut: () -> Void
    @State private var chosen: String?
    @State private var players = ItemPlayers()
    @State private var sample: SampleFacts?
    @State private var asking = false

    var body: some View {
        let setting = kind.settings.first { $0.show == chosen } ?? kind.firstSetting
        let offering = kind.approval == nil && kind.settings.isEmpty
        let offer: ((KindsFacts.Show) -> Void)? = offering && kind.sampleSeason == nil && !asking ? { makeSample($0) } : nil
        ScrollView {
            VStack(alignment: .leading, spacing: 22) {
                if kind.shows.isEmpty && kind.settings.isEmpty {
                    Text(Words.kindEmpty(kind.kind)).foregroundStyle(.secondary)
                } else if offering, let season = kind.sampleSeason {
                    SampleWork(prefix: season, model: model, facts: $sample, namesSeason: true, openItem: openItem,
                               acted: { await model.refreshKinds() })
                        .id(season)
                } else if offering, let biggest = kind.shows.first {
                    Button(Words.makeSampleOf(biggest.name)) { makeSample(biggest) }
                        .buttonStyle(.borderedProminent)
                        .disabled(asking)
                } else if let episode = setting?.libraryItemId {
                    KindPlayer(players: players, model: model)
                        .task(id: episode) {
                            players.opensAt = KindPlayer.moments[0]
                            players.load(episode, compressed: true, originalBackup: true, client: model.client)
                        }
                } else if setting != nil {
                    Text(Words.kindNothingToWatch).foregroundStyle(.secondary)
                }
                if !offering {
                    KindDecision(kind: kind, setting: setting, model: model)
                }
                KindShows(kind: kind, playing: offering ? kind.sampleShow : setting?.show, leftOut: leftOut, posters: model.posters,
                          choose: { chosen = $0 }, open: openItem, openLeftOut: openLeftOut,
                          offer: offer)
            }
            .frame(maxWidth: 1000, alignment: .leading)
            .padding(.horizontal, 34)
            .padding(.vertical, 20)
            .frame(maxWidth: .infinity, alignment: .leading)
        }
        .scrollBounceBehavior(.basedOnSize)
        .background(Color(nsColor: .textBackgroundColor))
        .navigationTitle(Words.kindName(kind.kind))
        .navigationSubtitle(Words.kindSubtitle(kind))
        .onChange(of: kind.kind) {
            chosen = nil
            sample = nil
        }
        // The page stays when another kind opens, so a kind with nothing to watch must not keep the last one playing.
        .onChange(of: setting?.libraryItemId) { _, episode in if episode == nil { players.pause() } }
        .task { await model.refreshKinds() }
        .onDisappear { players.pause() }
    }

    /// Asks for the show's sample at its own size goal. A refusal opens the season's sample page, which says what the
    /// sample needs first.
    private func makeSample(_ show: KindsFacts.Show) {
        asking = true
        Task {
            do {
                if try await !model.client.makeSample(show.sampleSeason, size: nil, balance: nil).ok {
                    openSample(show.sampleSeason)
                }
            } catch {
                model.notice = .unanswered
            }
            await model.refreshKinds()
            asking = false
        }
    }
}

/// The decision, one plain line under the player: whose setting it would use and the one button.
struct KindDecision: View {
    let kind: KindsFacts.Kind
    let setting: KindsFacts.Setting?
    let model: MainWindowModel
    @State private var working = false
    @State private var refusal: String?

    var body: some View {
        HStack(alignment: .firstTextBaseline, spacing: 14) {
            if let approval = kind.approval {
                Label(Words.kindDecided(approval), systemImage: "checkmark")
                    .labelStyle(KindCheckLabel())
                Spacer(minLength: 12)
                Button(Words.stopUsingKind) { act { try await $0.stopKind(kind.kind) } }
            } else if let setting {
                Text(Words.showSetting(setting.name, setting))
                Spacer(minLength: 12)
                if kind.oneTime {
                    Text(Words.decidedShowByShow(kind.kind)).foregroundStyle(.secondary)
                } else {
                    Button(Words.useForKind(kind.kind)) { act { try await $0.approveKind(kind.kind, from: setting.fromPrefix) } }
                        .buttonStyle(.borderedProminent)
                }
            }
        }
        .disabled(working)
        .alert(Words.kindRefusedTitle, isPresented: Binding(get: { refusal != nil }, set: { if !$0 { refusal = nil } })) {
            Button(Words.ok) {}
        } message: {
            Text(refusal ?? "")
        }
    }

    private func act(_ action: @escaping @Sendable (MediaforceClient) async throws -> KindActionResult) {
        working = true
        Task {
            do {
                let result = try await action(model.client)
                if !result.ok { refusal = Words.kindRefusal(result.code) }
            } catch {
                model.notice = .unanswered
            }
            await model.refreshKinds()
            working = false
        }
    }
}

/// A check in the accent colour before the decision, as Finder marks a chosen item; never a traffic-light green.
struct KindCheckLabel: LabelStyle {
    func makeBody(configuration: Configuration) -> some View {
        HStack(alignment: .firstTextBaseline, spacing: 6) {
            configuration.icon.foregroundStyle(Color.accentColor).fontWeight(.semibold)
            configuration.title
        }
    }
}

/// The shows of a kind, poster first with its name and a line under it. The shows already approved on this kind
/// come first: one with an episode to watch plays in the player and carries a play mark. Every other show opens its
/// own page. The seasons no kind covers are one link at the end of the row's heading. While the kind waits for its
/// first sample, each show but the biggest, which the page's button names, offers one under its size.
struct KindShows: View {
    static let width: CGFloat = 96

    let kind: KindsFacts.Kind
    let playing: String?
    let leftOut: Int
    let posters: PosterStore
    let choose: (String) -> Void
    let open: (String) -> Void
    let openLeftOut: () -> Void
    let offer: ((KindsFacts.Show) -> Void)?

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack(alignment: .firstTextBaseline) {
                Text(Words.showsOfThisKind).font(.headline)
                Spacer()
                if leftOut > 0 {
                    Button(Words.seasonsWithoutKind(leftOut), action: openLeftOut).buttonStyle(.link)
                }
            }
            ScrollView(.horizontal) {
                LazyHStack(alignment: .top, spacing: 14) {
                    ForEach(kind.settings) { setting in
                        let watchable = setting.libraryItemId != nil
                        // A show approved on one season can still have others this kind covers; it shows once.
                        let covered = kind.shows.first { $0.prefix == setting.show }
                        tile(setting.show, name: setting.name, detail: covered.map { Words.size($0.waitingBytes) } ?? Words.approvedShow,
                             watchable: watchable) {
                            watchable ? choose(setting.show) : open(setting.show)
                        }
                    }
                    ForEach(kind.shows.filter { show in !kind.settings.contains { $0.show == show.prefix } }) { show in
                        VStack(alignment: .leading, spacing: 2) {
                            tile(show.prefix, name: show.name, detail: Words.size(show.waitingBytes), watchable: false) { open(show.prefix) }
                            if let offer {
                                // The biggest show's link stays hidden, keeping every tile one height in the lazy row.
                                Button(Words.makeASample) { offer(show) }
                                    .buttonStyle(.link)
                                    .font(.caption)
                                    .help(Words.makeSampleOf(show.name))
                                    .opacity(show == kind.shows.first ? 0 : 1)
                                    .disabled(show == kind.shows.first)
                            }
                        }
                    }
                }
            }
            .scrollIndicators(.never)
        }
    }

    private func tile(_ prefix: String, name: String, detail: String, watchable: Bool, action: @escaping () -> Void) -> some View {
        Button(action: action) {
            VStack(alignment: .leading, spacing: 4) {
                poster(prefix, name: name, watchable: watchable)
                Text(name).font(.callout).lineLimit(1)
                Text(detail).font(.caption).foregroundStyle(.secondary).monospacedDigit()
            }
            .frame(width: Self.width, alignment: .leading)
        }
        .buttonStyle(.plain)
        .help(watchable ? Words.watchShow(name) : Words.openShow(name))
    }

    private func poster(_ prefix: String, name: String, watchable: Bool) -> some View {
        MediaPoster(row: MediaRow(id: prefix, name: name, kind: .tv, detail: "", bytes: 0, saved: nil, state: .readyToCompress),
                    posters: posters, width: Self.width)
            .overlay {
                if prefix == playing {
                    RoundedRectangle(cornerRadius: 6, style: .continuous).strokeBorder(Color.accentColor, lineWidth: 3)
                }
            }
            .overlay(alignment: .bottomLeading) {
                if watchable {
                    Image(systemName: "play.fill")
                        .font(.system(size: 9, weight: .bold))
                        .foregroundStyle(.white)
                        .frame(width: 22, height: 22)
                        .background(.black.opacity(0.6), in: Circle())
                        .padding(6)
                }
            }
    }
}

enum KindPlayerLayout: String, CaseIterable, Identifiable {
    case sideBySide, wipe, oneAtATime
    var id: String { rawValue }
}

/// The original and the compressed episode on one clock: side by side, a wipe dragged across one picture, or one at
/// a time. Three moments to jump between, whose sound to hear, frame stepping, actual size and full screen.
struct KindPlayer: View {
    // Where each moment falls, as a share of the episode.
    static let moments = [0.2, 0.5, 0.8]

    let players: ItemPlayers
    let model: MainWindowModel
    @AppStorage("kindPlayerLayout") private var layout = KindPlayerLayout.wipe
    @State private var view = KindPictureView()
    @Environment(\.openWindow) private var openWindow

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            KindPicture(players: players, layout: layout, view: $view)
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
                    model.theater = Theater(players: players, layout: layout)
                    openWindow(id: TheaterWindow.id)
                }
                .keyboardShortcut("f", modifiers: [])
                .help(Words.fullScreenHelp)
            }
            .labelStyle(.iconOnly)
            .disabled(players.preparing || players.failed)
            HStack(spacing: 14) {
                Picker(Words.viewLabel, selection: $layout) {
                    ForEach(KindPlayerLayout.allCases) { Text(Words.playerLayout($0)).tag($0) }
                }
                .pickerStyle(.segmented)
                .labelsHidden()
                .fixedSize()
                // Buttons, not a selection, so the moment the picture is nearest to can be jumped back to.
                HStack(spacing: 4) {
                    Text(Words.momentChoice)
                    ForEach(Self.moments.indices, id: \.self) { index in
                        let at = (players.duration * Self.moments[index]).rounded(.down)
                        Button(Words.momentTime(at)) { players.seek(to: at) }
                            .buttonStyle(.bordered)
                            .tint(index == currentMoment ? .accentColor : nil)
                    }
                }
                Picker(Words.hear, selection: Bindable(players).hearOriginal) {
                    Text(Words.original).tag(true)
                    Text(Words.compressed).tag(false)
                }
                .pickerStyle(.segmented)
                .fixedSize()
                Spacer()
                Toggle(Words.actualSize, isOn: $view.actualSize)
                    .toggleStyle(.button)
                    .help(Words.actualSizeHelp)
            }
            .controlSize(.small)
            .disabled(players.preparing || players.failed)
        }
    }

    // The moment the picture is nearest to, so the choice follows the timeline.
    private var currentMoment: Int {
        guard players.duration > 0 else { return 0 }
        let share = players.position / players.duration
        return Self.moments.indices.min { abs(Self.moments[$0] - share) < abs(Self.moments[$1] - share) } ?? 0
    }
}

/// How the picture is looked at: where the wipe sits, which side shows one at a time, and actual size.
struct KindPictureView: Equatable {
    var split: CGFloat = 0.5
    var showingOriginal = false
    var actualSize = false
}

/// The picture itself in the chosen layout. In actual size every pixel shows, and both sides pan together.
struct KindPicture: View {
    let players: ItemPlayers
    let layout: KindPlayerLayout
    @Binding var view: KindPictureView
    @State private var pan = ScrollPosition()
    @State private var otherPan = ScrollPosition()
    // Where the picture was last panned to, by either side.
    @State private var panned = CGPoint.zero

    var body: some View {
        Group {
            switch layout {
            case .sideBySide:
                HStack(spacing: 8) {
                    framed(Words.original) { pannable(position: $pan, follows: $otherPan) { PlayerSurface(player: players.original) } }
                    framed(Words.compressed) { pannable(position: $otherPan, follows: $pan) { PlayerSurface(player: players.main) } }
                }
            case .wipe:
                framed(nil) { pannable(position: $pan, follows: nil) { wipe } }
            case .oneAtATime:
                framed(view.showingOriginal ? Words.original : Words.compressed) {
                    pannable(position: $pan, follows: nil) {
                        PlayerSurface(player: view.showingOriginal ? players.original : players.main)
                    }
                }
                .onTapGesture { view.showingOriginal.toggle() }
                .help(Words.oneAtATimeHelp)
            }
        }
        .overlay {
            if players.failed {
                Text(Words.couldNotPlayEpisode).foregroundStyle(.white)
            } else if players.preparing {
                ProgressView(Words.gettingEpisodeReady)
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
                    FrameLabel(text: Words.original)
                    Spacer()
                    FrameLabel(text: Words.compressed)
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

/// What the full-screen player shows: the page's own players, so it continues where the page was.
struct Theater {
    let players: ItemPlayers
    let layout: KindPlayerLayout
}

/// The player alone, full screen. Escape or closing it returns to the page, still on the same moment.
struct TheaterWindow: View {
    static let id = "theater"

    let model: MainWindowModel
    @State private var view = KindPictureView()
    @Environment(\.dismissWindow) private var dismissWindow

    var body: some View {
        ZStack {
            Color.black
            if let theater = model.theater {
                KindPicture(players: theater.players, layout: theater.layout, view: $view)
                    .padding(theater.layout == .sideBySide ? 12 : 0)
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
