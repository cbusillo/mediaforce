import SwiftUI

struct SamplePage: View {
    let prefix: String
    let model: MainWindowModel
    let openItem: (String) -> Void
    var namePage: (String) -> Void = { _ in }
    @State private var facts: SampleFacts?

    var body: some View {
        let names = self.names
        PageFrame(title: names.season, subtitle: names.show) {
            SampleWork(prefix: prefix, model: model, facts: $facts, openItem: openItem)
        }
        .onChange(of: names.season, initial: true) { _, title in namePage(title) }
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
    @State private var players = ComparePlayers()

    var body: some View {
        VStack(alignment: .leading, spacing: 18) {
            if let facts {
                if !facts.moments.isEmpty {
                    ComparePlayer(players: players, model: model, moments: .clips(facts.moments),
                                  names: PictureNames(compressed: Words.sample),
                                  gettingReady: Words.gettingSampleReady, couldNotPlay: Words.couldNotPlaySample)
                }
                // The decision, under the player.
                DecisionLine {
                    // On a kind's page the headline names the season, so it opens the season's page.
                    if namesSeason {
                        Button(Words.sampleHeadline(facts, namesSeason: true, now: .now)) { openItem(prefix) }
                            .buttonStyle(QuietLinkStyle(primary: true))
                    } else {
                        Text(Words.sampleHeadline(facts, namesSeason: false, now: .now))
                    }
                } buttons: {
                    if facts.isInProgress {
                        ProgressView()
                            .controlSize(.small)
                    }
                    SampleActions(
                        facts: facts, working: working, approve: startApproval, make: { make(size: $0) },
                        compress: compress, openItem: { openItem(prefix) }
                    )
                }
                if let failure = Words.sampleFailure(facts.failure), facts.state == "needs_help" {
                    Text(failure)
                        .foregroundStyle(.secondary)
                }
                if facts.actions.contains(.chooseSize) || (askingSize && !facts.sizeChoices.isEmpty) {
                    SizeChoices(facts: facts, working: working) { make(size: $0) }
                }
                SampleFactsGrid(facts: facts)
                if let coverage = Words.sampleCoverage(facts) {
                    Text(coverage)
                        .foregroundStyle(.secondary)
                        .fixedSize(horizontal: false, vertical: true)
                }
            } else if unreadable {
                Text(Words.staleNotice(.failing, lastHeard: nil))
                    .foregroundStyle(.secondary)
            } else {
                ProgressView()
            }
        }
        .task(id: refreshes) { await watch() }
        .onDisappear { players.pause() }
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

/// The sizes to choose from when the sample can't be made until one is chosen.
struct SizeChoices: View {
    let facts: SampleFacts
    let working: Bool
    let make: (String?) -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            Text(Words.chooseSizeHeadline)
                .font(.headline)
            HStack(spacing: 10) {
                ForEach(facts.sizeChoices) { choice in
                    Button(Words.sizeChoice(choice)) { make(choice.key) }
                }
            }
        }
        .disabled(working)
    }
}

/// The sample's buttons, in its decision line.
struct SampleActions: View {
    let facts: SampleFacts
    let working: Bool
    let approve: () -> Void
    let make: (String?) -> Void
    let compress: () -> Void
    let openItem: () -> Void

    var body: some View {
        let actions = facts.actions
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
            }
        }
        .disabled(working)
    }
}
