import SwiftUI

// PROTOTYPE for cbusillo/mediaforce#1296, round two: Needs you drawn with the TV shows page's own parts.
// Render only, never wired into the app. Every item comes from Attention.waitingItems, as on the real page.
enum NeedsYouLayout: String {
    case shelf, tiles
}

struct NeedsYouRoundTwoPage: View {
    let layout: NeedsYouLayout
    var empty = false
    let facts: MainWindowFacts
    let posters: PosterStore
    var open: (Place) -> Void = { _ in }

    private var items: [AttentionNotice] { empty ? [] : Attention.waitingItems(facts) }
    private var computers: [AttentionNotice] { items.filter { $0.id.hasPrefix("computer:") } }
    private var cleanup: [AttentionNotice] { items.filter { $0.id == "backups" || $0.id.hasPrefix("original:") } }
    private var shows: [AttentionNotice] { items.filter { !computers.contains($0) && !cleanup.contains($0) } }

    var body: some View {
        PageFrame(title: Words.needsYou, subtitle: items.isEmpty ? "Nothing waiting" : "\(items.count) waiting") {
            if let status { Text(status).foregroundStyle(.secondary).frame(maxWidth: 640, alignment: .leading) }
            if items.isEmpty {
                VStack(alignment: .leading, spacing: 4) {
                    Text("Nothing is waiting for you").font(.title2.weight(.semibold))
                    Text("Mediaforce marks this page and the sidebar when something does.")
                        .font(.callout).foregroundStyle(.secondary)
                }
            } else {
                metrics
                if !shows.isEmpty { showSection }
                if !computers.isEmpty { computerSection }
                if !cleanup.isEmpty { cleanupSection }
            }
            FinishedShelf(seasons: Array(facts.recentlyFinishedSeasons.prefix(6)), posters: posters, open: open)
        }
    }

    // What Mediaforce is doing, without the sentences that point at this page's own items.
    private var status: String? {
        guard let explanation = Words.workExplanation(facts) else { return nil }
        let pointers = ["waiting in Needs you", "need you first", "waiting for you in Media"]
        let kept = explanation.split(separator: ". ").map(String.init).filter { sentence in
            !pointers.contains { sentence.contains($0) }
        }
        return kept.isEmpty ? nil : kept.joined(separator: ". ")
    }

    // Stat numbers in the TV shows page's own style: the value leads, its label recedes.
    private var metrics: some View {
        HStack(alignment: .top, spacing: 24) {
            metric("\(shows.count)", label: shows.count == 1 ? "Show to decide" : "Shows to decide")
            if !computers.isEmpty {
                metric("\(computers.count)", label: computers.count == 1 ? "Computer to check" : "Computers to check")
            }
            if facts.originalBackups.count > 0 {
                metric(Words.size(facts.originalBackups.bytes), label: "Ready to delete")
            }
            Spacer()
        }
    }

    private func metric(_ value: String, label: String) -> some View {
        VStack(alignment: .leading, spacing: 4) {
            Text(value).font(.title2.weight(.semibold)).monospacedDigit()
            Text(label).font(.callout).foregroundStyle(.secondary)
        }
    }

    private func heading(_ title: String, count: Int) -> some View {
        HStack(spacing: 6) {
            Text(title).font(.headline)
            Text("\(count)").foregroundStyle(.secondary).monospacedDigit()
        }
    }

    // MARK: Shows

    @ViewBuilder private var showSection: some View {
        VStack(alignment: .leading, spacing: 12) {
            heading("Shows", count: shows.count)
            switch layout {
            case .shelf:
                ScrollView(.horizontal) {
                    HStack(alignment: .top, spacing: 22) {
                        ForEach(Array(shows.enumerated()), id: \.element.id) { index, item in
                            shelfCard(item, primary: index == 0)
                        }
                    }
                }
                .scrollIndicators(.never)
            case .tiles:
                LazyVGrid(columns: [GridItem(.adaptive(minimum: 400, maximum: 520), spacing: 28, alignment: .topLeading)],
                          alignment: .leading, spacing: 22) {
                    ForEach(Array(shows.enumerated()), id: \.element.id) { index, item in
                        tile(item, primary: index == 0)
                    }
                }
            }
        }
    }

    private static let cardWidth: CGFloat = 148

    private func shelfCard(_ item: AttentionNotice, primary: Bool) -> some View {
        let card = Card(item, facts: facts)
        return VStack(alignment: .leading, spacing: 6) {
            Button { if let place = item.opens { open(place) } } label: {
                PosterView(show: card.name, image: card.posterPrefix.flatMap(posters.image(for:)),
                           headline: card.headline, detail: card.state, width: Self.cardWidth)
            }
            .buttonStyle(.plain)
            .pointerStyle(.link)
            .accessibilityLabel(item.title)
            Text(card.name).font(.callout.weight(.semibold)).lineLimit(1)
            Text(card.reason)
                .font(.caption).foregroundStyle(.secondary)
                .lineLimit(4)
                .frame(height: 58, alignment: .topLeading)
            action(card, primary: primary)
        }
        .frame(width: Self.cardWidth, alignment: .leading)
    }

    private func tile(_ item: AttentionNotice, primary: Bool) -> some View {
        let card = Card(item, facts: facts)
        return HStack(alignment: .top, spacing: 14) {
            Button { if let place = item.opens { open(place) } } label: {
                PosterView(show: card.name, image: card.posterPrefix.flatMap(posters.image(for:)),
                           headline: card.headline, detail: card.state, width: 96)
            }
            .buttonStyle(.plain)
            .pointerStyle(.link)
            .accessibilityLabel(item.title)
            VStack(alignment: .leading, spacing: 6) {
                Text(card.name).font(.headline)
                Text(card.state).font(.callout)
                Text(card.reason)
                    .foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
                action(card, primary: primary).padding(.top, 4)
            }
            .frame(maxWidth: .infinity, alignment: .leading)
        }
    }

    @ViewBuilder private func action(_ card: Card, primary: Bool) -> some View {
        if let place = card.item.opens {
            if primary {
                Button(card.action) { open(place) }.buttonStyle(.borderedProminent).controlSize(.small)
            } else {
                Button(card.action) { open(place) }.controlSize(.small)
            }
        }
    }

    // MARK: Computers and cleanup, as compact rows like the TV shows page's Computers

    private var computerSection: some View {
        VStack(alignment: .leading, spacing: 10) {
            heading("Computers", count: computers.count)
            VStack(alignment: .leading, spacing: 10) {
                ForEach(computers, id: \.id) { item in
                    let computer = facts.computers.first { item.id.hasPrefix("computer:\($0.key):") }
                    compactRow(symbol: computer.map(Words.computerSymbol) ?? "desktopcomputer",
                               name: computer.flatMap(\.name) ?? item.title,
                               detail: computer.map { $0.attentionReason ?? Words.computerState($0.state) } ?? item.body,
                               waiting: true, action: "Open computer", place: item.opens)
                }
            }
            .padding(.leading, 6)
        }
    }

    private var cleanupSection: some View {
        VStack(alignment: .leading, spacing: 10) {
            Text("Cleanup").font(.headline)
            VStack(alignment: .leading, spacing: 10) {
                ForEach(cleanup, id: \.id) { item in
                    if item.id == "backups" {
                        compactRow(symbol: "trash", name: "Original backups",
                                   detail: "\(Words.count(facts.originalBackups.count, "backup")), \(Words.size(facts.originalBackups.bytes)), ready to delete when you are",
                                   waiting: false, action: "Review original backups", place: item.opens)
                    } else {
                        compactRow(symbol: "doc.text.magnifyingglass", name: item.title, detail: item.body,
                                   waiting: false, action: "Review file list", place: item.opens)
                    }
                }
            }
            .padding(.leading, 6)
        }
    }

    private func compactRow(symbol: String, name: String, detail: String, waiting: Bool, action: String, place: Place?) -> some View {
        HStack(alignment: .center, spacing: 10) {
            Image(systemName: symbol)
                .foregroundStyle(.secondary)
                .frame(width: 22)
                .accessibilityHidden(true)
            VStack(alignment: .leading, spacing: 1) {
                Text(name)
                HStack(spacing: 4) {
                    if waiting { WaitingMark().accessibilityHidden(true) }
                    Text(detail).foregroundStyle(.secondary)
                }
                .font(.caption)
            }
            .frame(minWidth: 260, alignment: .leading)
            if let place {
                Button(action) { open(place) }.controlSize(.small)
            }
        }
    }
}

/// What one waiting show says on its card, drawn from the same facts as its notice.
@MainActor
private struct Card {
    let item: AttentionNotice
    let name: String
    let posterPrefix: String?
    let headline: String
    let state: String
    let reason: String
    let action: String

    init(_ item: AttentionNotice, facts: MainWindowFacts) {
        self.item = item
        let sample = facts.samples.waitingOnYou.first { item.opens == .sample($0.prefix) }
        let kind = facts.work.kindsNeedingYou.first { item.id.hasPrefix("kind:\($0.kind):") }
        let season = facts.work.seasonsNeedingYou.first { item.opens == .item($0.prefix) }
        if let sample {
            name = sample.show
            posterPrefix = sample.showPrefix
            headline = sample.season.map { "Season \($0)" } ?? sample.show
            state = Words.sampleState(sample)
            reason = Words.sampleFailure(sample.failure) ?? item.body
            action = Words.sampleAction(sample.action) ?? "Open sample"
        } else if let kind {
            name = Words.kindName(kind.kind)
            posterPrefix = kind.sampleSeason.map { MediaPoster.titlePrefix($0) }
            headline = kind.sampleSeason?.split(separator: "/").last.map(String.init) ?? name
            state = kind.sampleReady == true ? "Sample ready" : "Needs your choice"
            reason = item.body
            action = "Review kind"
        } else if let season {
            name = season.showName
            posterPrefix = MediaPoster.titlePrefix(season.prefix)
            headline = Words.season(season.showName, season.season)
            state = Words.tvSeasonState(season)
            reason = item.body
            action = "Open"
        } else {
            name = item.title
            posterPrefix = nil
            headline = item.title
            state = ""
            reason = item.body
            action = "Open"
        }
    }
}
