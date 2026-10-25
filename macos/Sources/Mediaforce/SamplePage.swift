import AVKit
import Combine
import SwiftUI

struct SamplePage: View {
    static let pollInterval: Duration = .seconds(5)

    let prefix: String
    let model: MainWindowModel
    @State private var facts: SampleFacts?
    @State private var unreadable = false
    @State private var working = false
    @State private var refreshes = 0
    @State private var confirmed: [String] = []
    @State private var confirming: String?
    @State private var choosingBalance = false
    @Environment(\.openURL) private var openURL

    var body: some View {
        PageFrame(title: title) {
            if let facts {
                Text(Words.sampleHeadline(facts))
                    .font(.title3.weight(.semibold))
                if let failure = Words.sampleFailure(facts.failure), facts.state == "needs_help" {
                    Text(failure)
                        .foregroundStyle(.secondary)
                }
                if facts.isBeingMade {
                    ProgressView()
                        .controlSize(.small)
                }
                if !facts.moments.isEmpty {
                    SampleComparison(moments: facts.moments, client: model.client)
                }
                SampleFactsGrid(facts: facts)
                SampleActions(facts: facts, working: working, approve: startApproval, make: { make(size: $0) })
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
                confirmed.append(String(code.dropFirst("confirm_".count)))
                approve()
            }
            Button(Words.cancel, role: .cancel) {}
        } message: { code in
            Text(Words.sampleConfirmation(code)?.message ?? "")
        }
        .alert(ActionNotice.chooseBalance.title, isPresented: $choosingBalance) {
            Button(Words.openInBrowser) { openURL(model.client.seasonPage(prefix)) }
            Button(Words.cancel, role: .cancel) {}
        } message: {
            Text(ActionNotice.chooseBalance.message)
        }
    }

    private var title: String {
        if let facts { return Words.season(facts.show, facts.season) }
        let samples = (model.facts?.samples.waitingOnYou ?? []) + (model.facts?.samples.approvedNotStarted ?? [])
        if let sample = samples.first(where: { $0.prefix == prefix }) { return Words.season(sample.show, sample.season) }
        return prefix.split(separator: "/").dropFirst().joined(separator: ", ")
    }

    private func watch() async {
        repeat {
            do {
                facts = try await model.client.sample(prefix)
                unreadable = false
            } catch {
                unreadable = facts == nil
            }
            guard facts?.isBeingMade == true else { return }
            try? await Task.sleep(for: Self.pollInterval)
        } while !Task.isCancelled
    }

    private func startApproval() {
        confirmed = []
        approve()
    }

    private func approve() {
        let confirm = confirmed
        let draftHash = facts?.draftHash ?? ""
        act {
            let result = try await $0.approveSample(prefix, confirm: confirm, draftHash: draftHash)
            if let code = result.code, Words.sampleConfirmation(code) != nil {
                confirming = code
                return nil
            }
            if !result.ok { return .sampleRefused }
            return result.compressing == true ? nil : .approvedNotCompressing
        }
    }

    private func make(size: String?) {
        act {
            let result = try await $0.makeSample(prefix, size: size)
            if result.ok { return nil }
            switch result.code {
            case "choose_balance":
                choosingBalance = true
                return nil
            case "choose_size": return .chooseSize
            default: return .sampleRefused
            }
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
    let approve: () -> Void
    let make: (String?) -> Void

    var body: some View {
        let actions = facts.actions
        VStack(alignment: .leading, spacing: 10) {
            if actions.contains(.chooseSize) {
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
