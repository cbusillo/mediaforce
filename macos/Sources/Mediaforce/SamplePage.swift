import AVKit
import Combine
import SwiftUI

struct SamplePage: View {
    let prefix: String
    let model: MainWindowModel
    let openItem: (String) -> Void
    @State private var facts: SampleFacts?

    var body: some View {
        let names = self.names
        PageFrame(title: names.season, subtitle: names.show) {
            SampleWork(prefix: prefix, model: model, facts: $facts, openItem: openItem)
        }
    }

    /// The season names the page and its show is the subtitle, as on the season's own page.
    private var names: (season: String, show: String) {
        if let facts { return Words.seasonTitle(facts.show, facts.season) }
        let samples = (model.facts?.samples.waitingOnYou ?? []) + (model.facts?.samples.approvedNotStarted ?? [])
        if let sample = samples.first(where: { $0.prefix == prefix }) { return Words.seasonTitle(sample.show, sample.season) }
        // Until its facts arrive, the page is named by its folders.
        let folders = prefix.split(separator: "/").map(String.init)
        return (folders.last ?? prefix, folders.count > 2 ? folders[folders.count - 2] : "")
    }
}

/// A season's sample and what can be done with it: the headline, the comparison, its facts and the actions. The
/// sample page shows it under the season's name; a kind's page shows it with the season named in the headline.
struct SampleWork: View {
    static let pollInterval: Duration = .seconds(5)

    let prefix: String
    let model: MainWindowModel
    @Binding var facts: SampleFacts?
    var namesSeason = false
    let openItem: (String) -> Void
    // After an action, what else the page around it reads again.
    var acted: () async -> Void = {}
    @State private var unreadable = false
    @State private var working = false
    @State private var refreshes = 0
    @State private var approval: SampleApproval?
    @State private var confirming: String?
    // The sample can't be made until a size is chosen, so the page offers the sizes right there.
    @State private var askingSize = false

    var body: some View {
        VStack(alignment: .leading, spacing: 18) {
            if let facts {
                // On a kind's page the headline names the season, so it opens the season's page.
                Group {
                    if namesSeason {
                        Button(Words.sampleHeadline(facts, namesSeason: true, now: .now)) { openItem(prefix) }
                            .buttonStyle(QuietLinkStyle(primary: true))
                    } else {
                        Text(Words.sampleHeadline(facts, namesSeason: false, now: .now))
                    }
                }
                .font(.title3.weight(.semibold))
                if let failure = Words.sampleFailure(facts.failure), facts.state == "needs_help" {
                    Text(failure)
                        .foregroundStyle(.secondary)
                }
                if facts.isInProgress {
                    ProgressView()
                        .controlSize(.small)
                }
                if !facts.moments.isEmpty {
                    SampleComparison(moments: facts.moments, client: model.client)
                }
                SampleFactsGrid(facts: facts)
                if let coverage = Words.sampleCoverage(facts) {
                    Text(coverage)
                        .foregroundStyle(.secondary)
                        .fixedSize(horizontal: false, vertical: true)
                }
                SampleActions(
                    facts: facts, working: working, askingSize: askingSize, approve: startApproval, make: { make(size: $0) },
                    compress: compress, openItem: { openItem(prefix) }
                )
            } else if unreadable {
                Text(Words.staleNotice(.failing, lastHeard: nil))
                    .foregroundStyle(.secondary)
            } else {
                ProgressView()
            }
        }
        .task(id: refreshes) { await watch() }
        .confirmationDialog(
            confirming.flatMap(Words.sampleConfirmation)?.title ?? "",
            isPresented: Binding(get: { confirming != nil }, set: { if !$0 { confirming = nil } }),
            presenting: confirming
        ) { code in
            Button(Words.approveSample) {
                approval?.confirmed.append(String(code.dropFirst("confirm_".count)))
                approve()
            }
            Button(Words.cancel, role: .cancel) {}
        } message: { code in
            Text(Words.sampleConfirmation(code)?.message ?? "")
        }
    }

    private func watch() async {
        repeat {
            do {
                facts = try await model.client.sample(prefix)
                unreadable = false
                dropApprovalIfChanged()
            } catch {
                unreadable = facts == nil
            }
            guard facts?.isInProgress == true else { return }
            try? await Task.sleep(for: Self.pollInterval)
        } while !Task.isCancelled
    }

    private func startApproval() {
        approval = SampleApproval(reviewedDraftHash: facts?.draftHash ?? "")
        approve()
    }

    private func dropApprovalIfChanged() {
        guard let approval, !approval.stillReviewed(facts) else { return }
        self.approval = nil
        if confirming != nil {
            confirming = nil
            model.notice = .sampleChanged
        }
    }

    private func approve() {
        dropApprovalIfChanged()
        guard let approval else {
            model.notice = .sampleChanged
            return
        }
        act {
            let result = try await $0.approveSample(prefix, confirm: approval.confirmed, draftHash: approval.reviewedDraftHash)
            if let code = result.code, Words.sampleConfirmation(code) != nil {
                confirming = code
                return nil
            }
            if result.code == "sample_changed" {
                self.approval = nil
                return .sampleChanged
            }
            return result.ok ? compressingNotice(result) : .sampleRefused
        }
    }

    private func compress() {
        act {
            let result = try await $0.compressSeason(prefix)
            return result.ok ? compressingNotice(result) : .sampleRefused
        }
    }

    private func compressingNotice(_ result: SampleActionResult) -> ActionNotice? {
        if result.compressing == true { return nil }
        return result.needsChoice ? .compressNeedsChoice : .approvedNotCompressing
    }

    private func make(size: String?) {
        act {
            let result = try await $0.makeSample(prefix, size: size)
            askingSize = result.code == "choose_size" && facts?.sizeChoices.isEmpty == false
            if result.ok || askingSize { return nil }
            return result.code == "choose_balance" ? .chooseBalance : .sampleRefused
        }
    }

    private func act(_ action: @escaping @MainActor (MediaforceClient) async throws -> ActionNotice?) {
        working = true
        Task {
            do {
                model.notice = try await action(model.client)
            } catch {
                model.notice = .unanswered
            }
            working = false
            refreshes += 1
            await model.refresh()
            await acted()
        }
    }
}

struct SampleFactsGrid: View {
    let facts: SampleFacts

    var body: some View {
        Grid(alignment: .leadingFirstTextBaseline, horizontalSpacing: 18, verticalSpacing: 6) {
            ForEach(Words.sampleFacts(facts), id: \.label) { fact in
                GridRow {
                    Text(fact.label)
                        .foregroundStyle(.secondary)
                    Text(fact.value)
                        .monospacedDigit()
                }
            }
        }
    }
}

struct SampleActions: View {
    let facts: SampleFacts
    let working: Bool
    let askingSize: Bool
    let approve: () -> Void
    let make: (String?) -> Void
    let compress: () -> Void
    let openItem: () -> Void

    var body: some View {
        let actions = facts.actions
        VStack(alignment: .leading, spacing: 10) {
            if actions.contains(.chooseSize) || (askingSize && !facts.sizeChoices.isEmpty) {
                Text(Words.chooseSizeHeadline)
                    .font(.headline)
                HStack(spacing: 10) {
                    ForEach(facts.sizeChoices) { choice in
                        Button(Words.sizeChoice(choice)) { make(choice.key) }
                    }
                }
            }
            HStack(spacing: 10) {
                if actions.contains(.approve) {
                    Button(Words.approveSample, action: approve)
                        .buttonStyle(.borderedProminent)
                }
                if actions.contains(.startCompressing) {
                    Button(Words.startCompressing, action: compress)
                        .buttonStyle(.borderedProminent)
                }
                // Checking, replacing, retrying and motion choices live on the season's page.
                if let next = actions.first(where: SampleFacts.seasonPageActions.contains) {
                    Button(Words.workAction(next), action: openItem)
                        .buttonStyle(.borderedProminent)
                }
                if actions.contains(.makeSample) {
                    Button(Words.makeSample) { make(nil) }
                        .buttonStyle(.borderedProminent)
                }
                if actions.contains(.makeAgain) {
                    Button(Words.makeSampleAgain) { make(nil) }
                }
                if actions.contains(.tryAnotherSize) {
                    Menu(Words.tryAnotherSize) {
                        ForEach(facts.sizeChoices) { choice in
                            Button(Words.sizeChoice(choice)) { make(choice.key) }
                        }
                    }
                    .fixedSize()
                }
            }
        }
        .disabled(working)
    }
}

enum HearSide: Hashable {
    case original, sample
}

@MainActor
@Observable
final class ComparisonPlayers {
    let original = AVPlayer()
    let sample = AVPlayer()
    private(set) var playing = false
    var hear: HearSide = .original {
        didSet { applySound() }
    }

    init() {
        applySound()
    }

    func load(_ moment: SampleFacts.Moment, client: MediaforceClient) {
        pause()
        original.replaceCurrentItem(with: AVPlayerItem(url: client.reviewMedia(moment.original.url)))
        sample.replaceCurrentItem(with: AVPlayerItem(url: client.reviewMedia(moment.sample.url)))
    }

    func toggle() {
        if playing {
            pause()
            return
        }
        if original.currentItem.map({ $0.currentTime() >= $0.duration }) == true {
            original.seek(to: .zero)
        }
        sample.seek(to: original.currentTime(), toleranceBefore: .zero, toleranceAfter: .zero)
        original.play()
        sample.play()
        playing = true
    }

    func startOver() {
        pause()
        original.seek(to: .zero)
        sample.seek(to: .zero)
    }

    func pause() {
        original.pause()
        sample.pause()
        playing = false
    }

    private func applySound() {
        original.isMuted = hear != .original
        sample.isMuted = hear != .sample
    }
}

struct SampleComparison: View {
    let moments: [SampleFacts.Moment]
    let client: MediaforceClient
    @State private var players = ComparisonPlayers()
    @State private var moment = 0

    var body: some View {
        let current = moments[min(moment, moments.count - 1)]
        VStack(alignment: .leading, spacing: 10) {
            HStack(alignment: .top, spacing: 12) {
                ClipView(label: Words.original, detail: Words.clipSize(current.original), player: players.original)
                ClipView(label: Words.sample, detail: Words.clipSize(current.sample), player: players.sample)
            }
            HStack(spacing: 14) {
                Button {
                    players.toggle()
                } label: {
                    Label(players.playing ? Words.pause : Words.play, systemImage: players.playing ? "pause.fill" : "play.fill")
                }
                .keyboardShortcut(.space, modifiers: [])
                Button(Words.startOver, systemImage: "backward.end.fill") { players.startOver() }
                Picker(Words.hear, selection: Bindable(players).hear) {
                    Text(Words.original).tag(HearSide.original)
                    Text(Words.sample).tag(HearSide.sample)
                }
                .pickerStyle(.segmented)
                .fixedSize()
                if moments.count > 1 {
                    Picker(Words.momentChoice, selection: $moment) {
                        ForEach(moments.indices, id: \.self) { index in
                            Text(Words.momentTime(moments[index].atSeconds)).tag(index)
                        }
                    }
                    .fixedSize()
                }
            }
        }
        .task(id: current) { players.load(current, client: client) }
        .onReceive(NotificationCenter.default.publisher(for: AVPlayerItem.didPlayToEndTimeNotification).receive(on: RunLoop.main)) { note in
            if (note.object as? AVPlayerItem) === players.original.currentItem { players.pause() }
        }
        .onDisappear { players.pause() }
    }
}

struct ClipView: View {
    let label: String
    let detail: String
    let player: AVPlayer

    var body: some View {
        VStack(alignment: .leading, spacing: 5) {
            HStack(alignment: .firstTextBaseline) {
                Text(label)
                    .font(.headline)
                Spacer()
                Text(detail)
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .monospacedDigit()
            }
            PlayerSurface(player: player)
                .aspectRatio(16 / 9, contentMode: .fit)
                .background(Color.black)
                .clipShape(RoundedRectangle(cornerRadius: 5, style: .continuous))
        }
        .frame(maxWidth: .infinity)
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
