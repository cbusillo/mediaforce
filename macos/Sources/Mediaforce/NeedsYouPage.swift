import SwiftUI

/// Everything waiting for the Director, as one list. Each row is one thing: the space it covers, its picture, what it
/// asks in a few words, and the answers beside it. What stops work comes first, then the most space.
struct NeedsYouPage: View {
    let facts: MainWindowFacts
    let posters: PosterStore
    let model: MainWindowModel
    let open: (Place) -> Void
    // Each waiting season's files, and each size question's sample, read from their pages so a row can answer in place.
    @State private var seasonFiles: [String: ItemFacts] = [:]
    @State private var samples: [String: SampleFacts] = [:]
    @State private var working: Set<String> = []
    @State private var reads = 0
    @State private var originalPreview: OriginalPreview?
    @State private var releasing: HeldFile?

    var body: some View {
        let items = NeedsYouItem.items(facts, kinds: model.kinds, files: seasonFiles, samples: samples)
        PageFrame(title: Words.needsYou, subtitle: Words.needsYouCount(items.count)) {
            if items.isEmpty {
                VStack(alignment: .leading, spacing: 4) {
                    Label(Words.nothingNeedsYou, systemImage: "checkmark.circle")
                        .font(.title2.weight(.semibold))
                    if facts.spaceSaved.bytes > 0 {
                        Text(Words.savedSoFar(facts.spaceSaved.bytes))
                            .foregroundStyle(.secondary)
                            .padding(.leading, 30)
                    }
                }
                .padding(.bottom, 10)
                FinishedShelf(seasons: Array(facts.recentlyFinishedSeasons.prefix(6)), posters: posters, open: open)
            } else {
                VStack(alignment: .leading, spacing: 0) {
                    ForEach(Array(items.enumerated()), id: \.element.id) { index, item in
                        if index > 0 { Divider().padding(.leading, NeedsYouRow.textInset) }
                        NeedsYouRow(item: item, first: index == 0, posters: posters, working: working.contains(item.id)) {
                            perform($0, for: item)
                        }
                    }
                }
                .frame(maxWidth: NeedsYouRow.maxWidth, alignment: .leading)
            }
        }
        .sheet(item: $originalPreview) { OriginalApprovalView(preview: $0, model: model) }
        .confirmationDialog(
            releasing.map { Words.releaseHoldQuestion($0.file) } ?? "",
            isPresented: Binding(get: { releasing != nil }, set: { if !$0 { releasing = nil } }),
            presenting: releasing
        ) { held in
            Button(Words.releaseHoldButton(held.file)) { answerFile(.releaseHold, held.prefix, held.file, item: held.item) }
            Button(Words.cancel, role: .cancel) {}
        } message: { held in
            Text(Words.releaseHoldWarning(held.file))
        }
        .task(id: readKey) { await readDetails() }
    }

    // Read again when what waits changes or another file finishes somewhere.
    private var readKey: [String] {
        facts.work.seasonsNeedingYou.map { "\($0.prefix):\($0.state ?? ""):\($0.action ?? "")" }
            + NeedsYouItem.sizeQuestions(facts) + ["\(facts.lastChange?.id ?? 0)", "\(reads)"]
    }

    private func readDetails() async {
        var files: [String: ItemFacts] = [:]
        for season in facts.work.seasonsNeedingYou {
            files[season.prefix] = try? await model.client.item(season.prefix)
        }
        var read: [String: SampleFacts] = [:]
        for prefix in NeedsYouItem.sampleReads(facts) {
            read[prefix] = try? await model.client.sample(prefix)
        }
        guard !Task.isCancelled else { return }
        seasonFiles = files
        samples = read
    }

    private func perform(_ answer: NeedsYouAnswer, for item: NeedsYouItem) {
        switch answer.action {
        case .open(let place): open(place)
        case .originals(let preview): originalPreview = preview
        case .file(.releaseHold, let prefix, let file): releasing = HeldFile(item: item.id, prefix: prefix, file: file)
        case .file(let action, let prefix, let file): answerFile(action, prefix, file, item: item.id)
        case .useSettings(let kind, let from):
            act(item.id) {
                let result = try await $0.approveKind(kind, from: from)
                return result.ok ? nil : ActionNotice(title: Words.kindRefusedTitle, message: Words.kindRefusal(result.code))
            }
        case .makeSample(let prefix, let size):
            act(item.id) {
                // A sample that needs more than a size opens its page, which says what else to choose.
                if try await !$0.makeSample(prefix, size: size, balance: nil).ok { open(.sample(prefix)) }
                return nil
            }
        }
    }

    private func answerFile(_ action: ItemAction, _ prefix: String, _ file: ItemFacts.File, item: String) {
        act(item) {
            let result = try await $0.answer(action, in: prefix, for: file)
            return result.ok ? nil : ActionNotice(title: Words.actionRefused(action), message: Words.actionRefusal(result.code, result.message))
        }
    }

    private func act(_ item: String, _ action: @escaping @MainActor (MediaforceClient) async throws -> ActionNotice?) {
        working.insert(item)
        Task {
            do {
                if let notice = try await action(model.client) { model.notice = notice }
            } catch {
                model.notice = .unanswered
            }
            await model.refresh()
            await model.refreshKinds()
            reads += 1
            working.remove(item)
        }
    }

    private struct HeldFile: Identifiable {
        let item: String
        let prefix: String
        let file: ItemFacts.File
        var id: Int { file.id }
    }
}

/// One thing waiting for the Director: what it is, the space it covers, and each question it asks.
struct NeedsYouItem: Identifiable {
    enum Picture: Equatable {
        case posters([String])
        case symbol(String)
    }

    let id: String
    let title: String
    var context: String?
    var bytes: Int64?
    let picture: Picture
    let asks: [NeedsYouAsk]
    // Where its name leads, to look closer before answering.
    var opens: Place?
    // A computer or a confirmation that holds up other work comes before any space.
    var stopsWork = false
}

/// One question, as a short line, with a way to look first and the answers that settle it.
struct NeedsYouAsk: Identifiable {
    let line: String
    var look: NeedsYouAnswer?
    let answers: [NeedsYouAnswer]
    var id: String { line }
}

struct NeedsYouAnswer {
    enum Action {
        case open(Place)
        case useSettings(kind: String, from: String)
        case makeSample(prefix: String, size: String?)
        case file(ItemAction, prefix: String, file: ItemFacts.File)
        case originals(OriginalPreview)
    }

    let title: String
    let action: Action

    /// Answered on the page itself, rather than by opening another.
    var answersInPlace: Bool {
        if case .open = action { false } else { true }
    }
}

@MainActor
extension NeedsYouItem {
    /// The rows, one for each thing the sidebar and badge count.
    static func items(_ facts: MainWindowFacts, kinds: KindsFacts?, files: [String: ItemFacts] = [:],
                      samples: [String: SampleFacts] = [:]) -> [NeedsYouItem] {
        let rows = Attention.waitingItems(facts).filter(\.notifies).map { notice in
            item(notice, facts: facts, kinds: kinds, files: files, samples: samples)
        }
        return rows.enumerated().sorted { left, right in
            let a = left.element, b = right.element
            if a.stopsWork != b.stopsWork { return a.stopsWork }
            if (a.bytes ?? -1) != (b.bytes ?? -1) { return (a.bytes ?? -1) > (b.bytes ?? -1) }
            return left.offset < right.offset
        }.map(\.element)
    }

    /// The samples whose size choices or season size a row reads.
    static func sampleReads(_ facts: MainWindowFacts) -> [String] {
        facts.samples.waitingOnYou.map(\.prefix) + sizeQuestions(facts)
    }

    static func sizeQuestions(_ facts: MainWindowFacts) -> [String] {
        facts.work.kindsNeedingYou.filter { $0.settingsAvailable != true && $0.sampleReady != true && $0.sampleStatus == "choose_size" }
            .compactMap(\.sampleStartPrefix)
    }

    private static func item(_ notice: AttentionNotice, facts: MainWindowFacts, kinds: KindsFacts?,
                             files: [String: ItemFacts], samples: [String: SampleFacts]) -> NeedsYouItem {
        if let pending = facts.work.kindsNeedingYou.first(where: { notice.id.hasPrefix("kind:\($0.kind):") }) {
            return kindItem(notice, pending, kind: kinds?.kinds.first { $0.kind == pending.kind }, samples: samples)
        }
        if let sample = facts.samples.waitingOnYou.first(where: { notice.opens == .sample($0.prefix) }) {
            return sampleItem(notice, sample, facts: samples[sample.prefix])
        }
        if let season = facts.work.seasonsNeedingYou.first(where: { notice.opens == .item($0.prefix) }) {
            return seasonItem(notice, season, files: files[season.prefix])
        }
        if let computer = facts.computers.first(where: { notice.id.hasPrefix("computer:\($0.key):") }) {
            return NeedsYouItem(id: notice.id, title: computer.name ?? computer.key, picture: .symbol(Words.computerSymbol(computer)),
                                asks: [NeedsYouAsk(line: Words.computerAsk(computer),
                                                   answers: [NeedsYouAnswer(title: Words.showComputer, action: .open(.computer(computer.key)))])],
                                opens: .computer(computer.key), stopsWork: true)
        }
        if let preview = facts.originalPreviews?.first(where: { notice.id.hasPrefix("original:\($0.id)") }) {
            return NeedsYouItem(id: notice.id, title: Words.originalFiles, picture: .symbol("doc.on.doc"),
                                asks: [NeedsYouAsk(line: Words.originalsAsk(preview),
                                                   answers: [NeedsYouAnswer(title: Words.reviewFileList, action: .originals(preview))])],
                                stopsWork: true)
        }
        if notice.id == Attention.backupsNoticeID {
            let backups = facts.originalBackups
            return NeedsYouItem(id: notice.id, title: Words.originalBackups, context: Words.count(backups.count, "file"),
                                bytes: backups.bytes, picture: .symbol("archivebox"),
                                asks: [NeedsYouAsk(line: Words.backupsAsk,
                                                   answers: [NeedsYouAnswer(title: Words.reviewAndDelete, action: .open(.spaceSaved))])],
                                opens: .spaceSaved)
        }
        return NeedsYouItem(id: notice.id, title: notice.title, picture: .symbol("questionmark.circle"),
                            asks: [NeedsYouAsk(line: notice.body, answers: notice.opens.map { [NeedsYouAnswer(title: Words.openItem, action: .open($0))] } ?? [])])
    }

    /// A group of shows decided together; a group whose shows are each decided on their own is shown by the show it asks about.
    private static func kindItem(_ notice: AttentionNotice, _ pending: MainWindowFacts.PendingKind, kind: KindsFacts.Kind?,
                                 samples: [String: SampleFacts]) -> NeedsYouItem {
        let group = Words.kindName(pending.kind)
        let season = pending.sampleSeason ?? pending.sampleStartPrefix
        let showPrefix = season.map(MediaPoster.titlePrefix)
        let showName = showPrefix.map(Words.lastFolder)
        let opens = notice.opens ?? .kind(pending.kind)
        let ask: NeedsYouAsk
        if pending.settingsAvailable == true {
            if let kind, let setting = kind.firstSetting {
                ask = NeedsYouAsk(line: Words.useSettingsAsk(setting), look: NeedsYouAnswer(title: Words.watchFirst, action: .open(.kind(kind.kind))),
                                  answers: [NeedsYouAnswer(title: Words.useForShows(kind.shows.count),
                                                           action: .useSettings(kind: kind.kind, from: setting.fromPrefix))])
            } else {
                ask = NeedsYouAsk(line: Words.useSettingsAsk(nil), answers: [NeedsYouAnswer(title: Words.chooseSettings, action: .open(.kind(pending.kind)))])
            }
        } else if pending.sampleReady ?? true {
            let line = pending.oneTime == true ? Words.sampleReadyAsk(season: season.map(Words.lastFolder))
                : Words.sampleReadyAsk(show: showName, season: season.map(Words.lastFolder))
            ask = NeedsYouAsk(line: line, answers: [NeedsYouAnswer(title: Words.watchSample, action: .open(opens))])
        } else {
            switch pending.sampleStatus {
            case "choose_size":
                let prefix = pending.sampleStartPrefix ?? ""
                ask = NeedsYouAsk(line: Words.firstSampleSizeAsk(season.map(Words.lastFolder)),
                                  answers: sizeAnswers(prefix, samples[prefix]) ?? [NeedsYouAnswer(title: Words.chooseSize, action: .open(opens))])
            case "choose_balance":
                ask = NeedsYouAsk(line: Words.firstSampleDetailAsk, answers: [NeedsYouAnswer(title: Words.chooseDetail, action: .open(opens))])
            case "pending_review", "completed":
                ask = NeedsYouAsk(line: Words.sampleCannotDecideGroup(showName),
                                  look: pending.sampleSeason.map { NeedsYouAnswer(title: Words.watchIt, action: .open(.sample($0))) },
                                  answers: [NeedsYouAnswer(title: Words.chooseAnotherShow, action: .open(.kind(pending.kind)))])
            default:
                ask = NeedsYouAsk(line: Words.firstSampleStopped, answers: [NeedsYouAnswer(title: Words.openSample, action: .open(opens))])
            }
        }
        if pending.oneTime == true, let showPrefix, let showName {
            return NeedsYouItem(id: notice.id, title: showName, context: group,
                                bytes: kind?.shows.first { $0.prefix == showPrefix }?.waitingBytes,
                                picture: .posters([showPrefix]), asks: [ask], opens: .kind(pending.kind))
        }
        return NeedsYouItem(id: notice.id, title: group, context: kind.map { Words.count($0.shows.count, "show") },
                            bytes: kind?.waitingBytes, picture: .posters(kind.map { $0.shows.prefix(3).map(\.prefix) } ?? []), asks: [ask],
                            opens: .kind(pending.kind))
    }

    private static func sampleItem(_ notice: AttentionNotice, _ sample: MainWindowFacts.Sample, facts: SampleFacts?) -> NeedsYouItem {
        let place = Place.sample(sample.prefix)
        let ask = if sample.state == "ready_to_compare" {
            NeedsYouAsk(line: Words.sampleReadyAsk(season: nil), answers: [NeedsYouAnswer(title: Words.watchSample, action: .open(place))])
        } else if sample.action == "choose_size" {
            NeedsYouAsk(line: Words.sampleSizeAsk(sample.failure),
                        answers: sizeAnswers(sample.prefix, facts) ?? [NeedsYouAnswer(title: Words.chooseSize, action: .open(place))])
        } else {
            NeedsYouAsk(line: Words.sampleFailure(sample.failure) ?? Words.sampleStopped,
                        answers: [NeedsYouAnswer(title: Words.makeSampleAgain, action: .makeSample(prefix: sample.prefix, size: nil))])
        }
        return NeedsYouItem(id: notice.id, title: sample.show, context: sample.season.map { "Season \($0)" },
                            bytes: facts?.seasonBytes, picture: .posters([sample.showPrefix]), asks: [ask], opens: place)
    }

    private static func seasonItem(_ notice: AttentionNotice, _ season: TVShowsFacts.Season, files: ItemFacts?) -> NeedsYouItem {
        let waiting = files?.waiting ?? []
        let asks = waiting.isEmpty
            ? [NeedsYouAsk(line: Words.sampleFailure(season.failure) ?? Words.tvSeasonState(season),
                           answers: [NeedsYouAnswer(title: Words.showSeason, action: .open(.item(season.prefix)))])]
            : waiting.map { fileAsk($0, season: season.prefix) }
        return NeedsYouItem(id: notice.id, title: season.showName, context: Words.seasonName(season),
                            bytes: waiting.isEmpty ? season.bytes : waiting.reduce(0) { $0 + ($1.originalBytes ?? $1.bytes) },
                            picture: .posters([MediaPoster.titlePrefix(season.prefix)]), asks: asks, opens: .item(season.prefix))
    }

    /// One file's question, answered on the page where Mediaforce takes the answer for that file.
    static func fileAsk(_ file: ItemFacts.File, season: String) -> NeedsYouAsk {
        if let question = file.sizeQuestion {
            return NeedsYouAsk(line: Words.sizeQuestionAsk(file, question), answers: [
                NeedsYouAnswer(title: Words.keepTheOriginal, action: .file(.keepOriginal, prefix: season, file: file)),
                NeedsYouAnswer(title: Words.allowLarger(question), action: .file(.allowLarger, prefix: season, file: file)),
            ])
        }
        if file.validation == "size_held" {
            return NeedsYouAsk(line: Words.sizeHeldAsk(file), answers: [
                NeedsYouAnswer(title: Words.keepThisCopy, action: .file(.keepThisCopy, prefix: season, file: file)),
                NeedsYouAnswer(title: Words.makeItAgain, action: .file(.makeAgain, prefix: season, file: file)),
            ])
        }
        if file.heldForUnconfirmedStop {
            return NeedsYouAsk(line: Words.heldFileAsk(file),
                               answers: [NeedsYouAnswer(title: Words.tryHeldFileAgain, action: .file(.releaseHold, prefix: season, file: file))])
        }
        return NeedsYouAsk(line: "\(Words.fileName(file)): \(Words.fileState(file)).",
                           answers: [NeedsYouAnswer(title: Words.showSeason, action: .open(.item(season)))])
    }

    private static func sizeAnswers(_ prefix: String, _ facts: SampleFacts?) -> [NeedsYouAnswer]? {
        guard let choices = facts?.sizeChoices, !choices.isEmpty else { return nil }
        return choices.map { NeedsYouAnswer(title: Words.sizeChoice($0), action: .makeSample(prefix: prefix, size: $0.key)) }
    }
}

/// A row: the space on the left so the biggest reads first, the picture, then the title and each question with its answers.
private struct NeedsYouRow: View {
    static let sizeWidth: CGFloat = 70
    static let pictureWidth: CGFloat = 44
    static let gap: CGFloat = 16
    static let maxWidth: CGFloat = 1040
    static var textInset: CGFloat { sizeWidth + pictureWidth + gap * 2 }

    let item: NeedsYouItem
    let first: Bool
    let posters: PosterStore
    let working: Bool
    let perform: (NeedsYouAnswer) -> Void

    var body: some View {
        HStack(alignment: .top, spacing: Self.gap) {
            Text(item.bytes.map(Words.size) ?? "")
                .font(.system(size: 15).monospacedDigit())
                .foregroundStyle(.secondary)
                .lineLimit(1)
                .frame(width: Self.sizeWidth, alignment: .trailing)
                .help(item.bytes == nil ? "" : Words.spaceCovered)
            NeedsYouPicture(picture: item.picture, posters: posters)
                .frame(width: Self.pictureWidth, alignment: .leading)
            VStack(alignment: .leading, spacing: 7) {
                HStack(alignment: .firstTextBaseline, spacing: 8) {
                    if let place = item.opens {
                        // The name opens its page, to watch or look closer before answering.
                        Button(item.title) { perform(NeedsYouAnswer(title: item.title, action: .open(place))) }
                            .buttonStyle(QuietLinkStyle(primary: true))
                            .font(.system(size: 15, weight: .semibold))
                            .pointerStyle(.link)
                    } else {
                        Text(item.title).font(.system(size: 15, weight: .semibold))
                    }
                    if let context = item.context {
                        Text(context).foregroundStyle(.secondary)
                    }
                }
                ForEach(item.asks) { ask in
                    DecisionLine {
                        Text(ask.line).fixedSize(horizontal: false, vertical: true)
                    } buttons: {
                        if let look = ask.look {
                            Button(look.title) { perform(look) }
                                .buttonStyle(QuietLinkStyle())
                                .padding(.trailing, 4)
                        }
                        ForEach(Array(ask.answers.enumerated()), id: \.offset) { index, answer in
                            // The page's one strong button: the first row's last answer, as a dialog's default sits last.
                            if first && ask.id == item.asks.first?.id && index == ask.answers.count - 1 {
                                Button(answer.title) { perform(answer) }.buttonStyle(.borderedProminent)
                            } else {
                                Button(answer.title) { perform(answer) }
                            }
                        }
                    }
                }
            }
            .disabled(working)
            .frame(maxWidth: .infinity, alignment: .leading)
        }
        .padding(.vertical, 14)
    }
}

/// A row's picture, all one size: a show's poster, a small stack for a group of shows, or a symbol for everything else.
private struct NeedsYouPicture: View {
    let picture: NeedsYouItem.Picture
    let posters: PosterStore

    var body: some View {
        switch picture {
        case .posters(let prefixes) where prefixes.count > 1:
            ZStack(alignment: .topLeading) {
                ForEach(Array(prefixes.enumerated().reversed()), id: \.offset) { index, prefix in
                    poster(prefix, width: 34)
                        .overlay(RoundedRectangle(cornerRadius: 3, style: .continuous)
                            .strokeBorder(Color(nsColor: .textBackgroundColor), lineWidth: 1))
                        .offset(x: CGFloat(index) * 5, y: CGFloat(index) * 6)
                }
            }
            .frame(width: 44, height: 66, alignment: .topLeading)
        case .posters(let prefixes):
            poster(prefixes.first ?? "", width: 44)
        case .symbol(let name):
            RoundedRectangle(cornerRadius: 4, style: .continuous)
                .fill(Color.primary.opacity(0.05))
                .frame(width: 44, height: 66)
                .overlay(Image(systemName: name).font(.system(size: 19)).foregroundStyle(.secondary))
                .accessibilityHidden(true)
        }
    }

    private func poster(_ prefix: String, width: CGFloat) -> some View {
        MediaPoster(titlePrefix: prefix, name: Words.lastFolder(prefix),
                    kind: MediaKind(rawValue: String(prefix.split(separator: "/").first ?? "")) ?? .other,
                    posters: posters, width: width)
    }
}
