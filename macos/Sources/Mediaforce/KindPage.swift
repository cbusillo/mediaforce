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
    @State private var players = ComparePlayers()
    @State private var sample: SampleFacts?
    @State private var asking = false

    var body: some View {
        let setting = kind.settings.first { $0.show == chosen } ?? kind.firstSetting ?? kind.sampleSetting
        let offering = kind.approval == nil && chosen == nil && kind.showsSampleFirst
        let offer: ((KindsFacts.Show) -> Void)? = offering && kind.offersSample && !asking ? { show in if let season = show.sampleSeason { makeSample(season) } } : nil
        let showSample: (() -> Void)? = chosen != nil || (kind.approval == nil && !kind.showsSampleFirst) ? { selectSample() } : nil
        ScrollView {
            VStack(alignment: .leading, spacing: 22) {
                if let size = kind.selectedSizeMb, let options = kind.sizeOptionsMb, kind.approval == nil {
                    Picker("Size for all cartoons per 45 minutes", selection: Binding(
                        get: { size },
                        set: { value in groupAction(refusal: ActionNotice(title: "Size for all cartoons was not saved",
                                                                          message: "Check the current size and try again.")) {
                            try await $0.chooseKindSize(kind.kind, megabytes: value)
                        } }
                    )) {
                        ForEach(options, id: \.self) { value in
                            Text("\(value.formatted()) MB").tag(value)
                        }
                    }
                    .fixedSize()
                    .disabled(asking || kind.sampleInProgress == true)
                }
                if kind.shows.isEmpty && kind.settings.isEmpty && kind.seasonApprovedShows?.isEmpty != false && kind.approval == nil && kind.reviewSeason == nil {
                    Text(Words.kindEmpty(kind.kind)).foregroundStyle(.secondary)
                } else if offering, let season = kind.reviewSeason,
                          kind.selectedSizeMb == nil || !Words.sampleNeedsRemaking(kind.sampleStatus) {
                    SampleWork(prefix: season, model: model, facts: $sample, namesSeason: true, allowsSeasonChanges: kind.selectedSizeMb == nil, openItem: openItem)
                        .id(season)
                } else if offering, let biggest = kind.firstSampleShow, let season = biggest.sampleSeason {
                    if asking {
                        ProgressView(Words.preparingSample).controlSize(.small)
                    }
                    Button(Words.makeSampleOf(biggest.name)) { makeSample(season) }
                        .buttonStyle(.borderedProminent)
                        .disabled(asking)
                } else if kind.approval != nil, chosen == nil {
                    if let moments = kind.approval?.sampleMoments, !moments.isEmpty {
                        ComparePlayer(players: players, model: model, moments: .clips(moments),
                                      names: PictureNames(compressed: Words.sample),
                                      gettingReady: Words.gettingSampleReady, couldNotPlay: Words.couldNotPlaySample)
                    } else {
                        Text(Words.couldNotPlaySample).foregroundStyle(.secondary)
                    }
                } else if let episode = setting?.libraryItemId {
                    ComparePlayer(players: players, model: model)
                        .task(id: episode) {
                            players.opensAt = ComparePlayer.moments[0]
                            players.load(episode, compressed: true, originalBackup: true, client: model.client)
                        }
                } else if setting != nil {
                    Text(Words.kindNothingToWatch).foregroundStyle(.secondary)
                }
                if !offering || kind.sampleSetting != nil {
                    KindDecision(kind: kind, setting: setting, model: model, openItem: openItem,
                                 showSample: showSample)
                        .disabled(asking)
                }
                if let titles = kind.animationTitles, !titles.isEmpty {
                    AnimationResults(titles: titles, model: model)
                }
                KindShows(kind: kind, playing: offering ? kind.sampleShow : (kind.approval != nil && chosen == nil ? nil : setting?.show), leftOut: leftOut, posters: model.posters,
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
            players.pause()
            chosen = nil
            sample = nil
        }
        // The page stays when another kind opens, so a kind with nothing to watch must not keep the last one playing.
        .onChange(of: setting?.libraryItemId) { _, episode in if episode == nil { players.pause() } }
        .task { await model.refreshKinds() }
        .onDisappear { players.pause() }
    }

    private func selectSample() {
        guard kind.approval == nil, let season = kind.reviewSeason else {
            chosen = nil
            return
        }
        if Words.sampleNeedsRemaking(kind.sampleStatus) { makeSample(season) }
        else if kind.showsSampleFirst { chosen = nil }
        else { openSample(season) }
    }

    private func makeSample(_ prefix: String) {
        if kind.selectedSizeMb != nil {
            groupAction(refusal: .sampleRefused) { try await $0.makeKindSample(kind.kind, prefix: prefix) }
        } else {
            asking = true
            Task {
                if await model.makeSample(prefix)?.ok != true { openSample(prefix) }
                await model.refreshKinds()
                asking = false
            }
        }
    }

    private func groupAction(refusal: ActionNotice, _ action: @escaping @MainActor (MediaforceClient) async throws -> KindActionResult) {
        asking = true
        Task {
            do {
                let result = try await action(model.client)
                if !result.ok, result.code != "already_preparing" {
                    model.notice = refusal.explained(by: result.message)
                }
            } catch {
                model.notice = .unanswered
            }
            await model.refreshKinds()
            asking = false
        }
    }
}

/// The decision, one plain line under the player: whose setting it would use, which opens that show, and the one button.
struct KindDecision: View {
    let kind: KindsFacts.Kind
    let setting: KindsFacts.Setting?
    let model: MainWindowModel
    let openItem: (String) -> Void
    @State private var working = false
    @State private var refusal: String?
    var showSample: (() -> Void)?

    var body: some View {
        HStack(alignment: .firstTextBaseline, spacing: 14) {
            if let approval = kind.approval {
                Label {
                    Button(Words.kindDecided(approval)) { openItem(approval.showPrefix) }
                        .buttonStyle(QuietLinkStyle(primary: true))
                } icon: {
                    Image(systemName: "checkmark")
                }
                .labelStyle(KindCheckLabel())
                Spacer(minLength: 12)
                if let limit = model.kinds?.tallestPicture, limit > 0 {
                    Picker(Words.fourK, selection: Binding(
                        get: { approval.keepFullHeight ?? false },
                        set: { keep in act { try await $0.chooseKindPicture(kind.kind, keepFullHeight: keep) } }
                    )) {
                        Text(Words.bringDownTo(limit)).tag(false)
                        Text(Words.keepFourK).tag(true)
                    }
                    .fixedSize()
                }
                Button(kind.kind == "cartoon" ? "Stop using for all cartoons" : Words.stopUsingKind) { act { try await $0.stopKind(kind.kind) } }
            } else if let setting {
                Button(Words.showSetting(setting.name, setting)) { openItem(setting.show) }
                    .buttonStyle(QuietLinkStyle(primary: true))
                Spacer(minLength: 12)
                if kind.oneTime {
                    Text(Words.decidedShowByShow(kind.kind)).foregroundStyle(.secondary)
                } else {
                    Button(Words.useForKind(kind.kind)) { act { try await $0.approveKind(kind.kind, from: setting.fromPrefix) } }
                        .buttonStyle(.borderedProminent)
                }
            }
            if let showSample, let title = kind.sampleButtonTitle {
                Button(title, action: showSample)
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
                Text(Words.titlesOfThisKind).font(.headline)
                Spacer()
                if leftOut > 0 {
                    Button(Words.seasonsWithoutKind(leftOut), action: openLeftOut).buttonStyle(QuietLinkStyle())
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
                    ForEach((kind.seasonApprovedShows ?? []).filter { prefix in !kind.shows.contains { $0.prefix == prefix } }, id: \.self) { prefix in
                        tile(prefix, name: Words.lastFolder(prefix), detail: Words.ownSampleApproved, watchable: false) { open(prefix) }
                    }
                    ForEach(kind.shows.filter { show in !kind.settings.contains { $0.show == show.prefix } }) { show in
                        VStack(alignment: .leading, spacing: 2) {
                            tile(show.prefix, name: show.name, detail: Words.kindTitleDetail(show, approvedShows: kind.seasonApprovedShows), watchable: false) { open(show.prefix) }
                            if let offer, show.sampleSeason != nil {
                                Button(Words.makeASample) { offer(show) }
                                    .buttonStyle(QuietLinkStyle())
                                    .font(.caption)
                                    .help(Words.makeSampleOf(show.name))
                                    .opacity(show.sampleSeason == kind.reviewSeason || (kind.reviewSeason == nil && show == kind.firstSampleShow) ? 0 : 1)
                                    .disabled(show.sampleSeason == kind.reviewSeason || (kind.reviewSeason == nil && show == kind.firstSampleShow))
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
        MediaPoster(titlePrefix: prefix, name: name, kind: .tv, posters: posters, width: Self.width)
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

private struct AnimationResults: View {
    let titles: [KindsFacts.AnimationTitle]
    let model: MainWindowModel
    @State private var reviewing: String?
    @State private var saving = false
    @State private var players = ComparePlayers()

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            Text("Animation looks").font(.headline)
            ForEach(titles.sorted { ($0.moment != nil || $0.error != nil) && ($1.moment == nil && $1.error == nil) }) { title in
                Divider()
                HStack {
                    VStack(alignment: .leading, spacing: 3) {
                        Text(title.name)
                        Text(title.explanation).foregroundStyle(.secondary)
                    }
                    Spacer()
                    if let error = title.error {
                        Button("Retry test") { choose(title, look: "retry") }.help(error).disabled(saving)
                    } else if title.moment != nil {
                        Button("Compare") { reviewing = title.id }
                    }
                }
                .help(title.kbps.map { "The test used \($0.formatted(.number.precision(.fractionLength(0)))) kilobits per second." } ?? title.explanation)
                if reviewing == title.id, let moment = title.moment {
                    ComparePlayer(players: players, model: model, moments: .clips([moment]), names: PictureNames(compressed: Words.sample))
                        .id(title.source)
                    HStack {
                        Button("Use cartoon look") { choose(title, look: "cartoon") }
                        Button("Use live action look") { choose(title, look: "live_action") }
                    }
                    .disabled(saving)
                }
            }
        }
        .onDisappear { players.pause() }
    }

    private func choose(_ title: KindsFacts.AnimationTitle, look: String) {
        saving = true
        Task {
            defer { saving = false }
            do {
                if try await model.client.chooseAnimationLook(title, look: look).ok {
                    players.pause()
                    reviewing = nil
                } else {
                    model.notice = ActionNotice(title: "Choice could not be saved", message: "Check the current result for this title.")
                }
            } catch {
                model.notice = .unanswered
            }
            await model.refreshKinds()
            await model.refresh()
        }
    }
}
