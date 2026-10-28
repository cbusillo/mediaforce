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
                    ComparePlayer(players: players, model: model)
                        .task(id: episode) {
                            players.opensAt = ComparePlayer.moments[0]
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
                if try await !model.client.makeSample(show.sampleSeason, size: nil).ok { openSample(show.sampleSeason) }
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
