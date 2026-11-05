import SwiftUI

// PROTOTYPE for cbusillo/mediaforce#1296: three layouts of Needs you for the Director to choose between.
// Not wired into the app. Every row comes from the same Attention.waitingItems the real page uses.
enum NeedsYouProposal: String {
    case a, b, c
}

struct NeedsYouProposalPage: View {
    let proposal: NeedsYouProposal
    var empty = false
    let facts: MainWindowFacts
    let posters: PosterStore

    private var items: [AttentionNotice] { empty ? [] : Attention.waitingItems(facts) }
    private var widget: WaitingItems { WaitingItems(items: items, posters: posters, open: { _ in }) }

    var body: some View {
        switch proposal {
        case .a: PageFrame(title: Words.needsYou, subtitle: "\(items.count) waiting") { grouped }
        case .b: PageFrame(title: Words.needsYou, subtitle: "\(items.count) waiting") { masterDetail }
        case .c: PageFrame(title: Words.needsYou, subtitle: "\(items.count) waiting") { register }
        }
    }

    // MARK: Shared

    private var status: some View {
        VStack(alignment: .leading, spacing: 4) {
            Text(Words.workHeadline(facts)).font(.headline)
            if let explanation = Words.workExplanation(facts) {
                Text(explanation).foregroundStyle(.secondary).frame(maxWidth: 640, alignment: .leading)
            }
        }
    }

    /// One calm empty state for all three layouts. It never says "all clear" when work is stopped.
    private var emptyState: some View {
        VStack(alignment: .leading, spacing: 18) {
            HStack(spacing: 14) {
                Image(systemName: "checkmark.circle").font(.system(size: 34)).foregroundStyle(.secondary).accessibilityHidden(true)
                VStack(alignment: .leading, spacing: 2) {
                    Text("Nothing needs you").font(.title2.weight(.semibold))
                    Text("Mediaforce will mark this page and the sidebar when something does.").foregroundStyle(.secondary)
                }
            }
            status
            Divider()
            FinishedShelf(seasons: Array(facts.recentlyFinishedSeasons.prefix(6)), posters: posters, open: { _ in })
        }
    }

    private func group(_ item: AttentionNotice) -> String {
        if item.id.hasPrefix("computer:") { return "Computers" }
        if item.id == "backups" || item.id.hasPrefix("original:") { return "Cleanup" }
        return "Shows"
    }

    private func reasonLine(_ item: AttentionNotice) -> some View {
        Text(item.body).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
    }

    // MARK: A — grouped decisions

    @ViewBuilder private var grouped: some View {
        if items.isEmpty {
            emptyState
        } else {
            HStack(alignment: .firstTextBaseline, spacing: 14) {
                Text("\(items.count)").font(.system(size: 44, weight: .semibold)).monospacedDigit()
                Text(items.count == 1 ? "decision is waiting for you" : "decisions are waiting for you").font(.title3)
                Spacer()
            }
            status.foregroundStyle(.secondary)
            ForEach(["Shows", "Computers", "Cleanup"], id: \.self) { name in
                let rows = items.filter { group($0) == name }
                if !rows.isEmpty {
                    VStack(alignment: .leading, spacing: 10) {
                        Text("\(name) · \(rows.count)").font(.subheadline.weight(.semibold)).foregroundStyle(.secondary)
                        Divider()
                        ForEach(rows, id: \.id) { item in
                            HStack(alignment: .center, spacing: 16) {
                                widget.artwork(item, width: 58)
                                VStack(alignment: .leading, spacing: 5) {
                                    HStack(spacing: 6) { WaitingMark(); Text(item.title).font(.title3.weight(.semibold)) }
                                    reasonLine(item)
                                }
                                Spacer(minLength: 16)
                                if let place = item.opens {
                                    Button(widget.actionTitle(place)) {}
                                        .buttonStyle(.borderedProminent).controlSize(.large)
                                }
                            }
                            .padding(.vertical, 6)
                            Divider()
                        }
                    }
                }
            }
        }
    }

    // MARK: B — pick on the left, decide on the right

    @ViewBuilder private var masterDetail: some View {
        if items.isEmpty {
            emptyState
        } else {
            let selected = items[0]
            HStack(alignment: .top, spacing: 0) {
                VStack(alignment: .leading, spacing: 0) {
                    ForEach(items, id: \.id) { item in
                        HStack(spacing: 10) {
                            widget.artwork(item, width: 28)
                            VStack(alignment: .leading, spacing: 2) {
                                Text(item.title).font(.headline).lineLimit(1)
                                Text(group(item)).font(.caption).foregroundStyle(.secondary)
                            }
                            Spacer(minLength: 0)
                            WaitingMark()
                        }
                        .padding(.horizontal, 14).padding(.vertical, 9)
                        .background(item.id == selected.id ? Color.accentColor.opacity(0.14) : .clear)
                        .overlay(alignment: .leading) {
                            Rectangle().fill(item.id == selected.id ? Color.accentColor : .clear).frame(width: 3)
                        }
                        Divider()
                    }
                    Spacer(minLength: 0)
                }
                .frame(width: 340)
                Divider()
                VStack(alignment: .leading, spacing: 18) {
                    HStack(alignment: .top, spacing: 22) {
                        widget.artwork(selected, width: 110)
                        VStack(alignment: .leading, spacing: 10) {
                            Text(selected.title).font(.title.weight(.semibold))
                            Text(selected.body).font(.title3).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
                            if let place = selected.opens {
                                Button(widget.actionTitle(place)) {}.buttonStyle(.borderedProminent).controlSize(.large).padding(.top, 6)
                            }
                        }
                    }
                    Divider()
                    status.foregroundStyle(.secondary)
                    Spacer(minLength: 0)
                }
                .padding(.horizontal, 28)
                .frame(maxWidth: .infinity, alignment: .topLeading)
            }
            .frame(maxWidth: .infinity, minHeight: 520, alignment: .topLeading)
            .border(Color.primary.opacity(0.14))
        }
    }

    // MARK: C — one dense register, same idiom as Library and Media

    @ViewBuilder private var register: some View {
        if items.isEmpty {
            emptyState
        } else {
            VStack(alignment: .leading, spacing: 0) {
                HStack(spacing: 12) {
                    Text("Waiting on").frame(width: 300, alignment: .leading)
                    Text("Why").frame(maxWidth: .infinity, alignment: .leading)
                    Text("Do this").frame(width: 190, alignment: .trailing)
                }
                .font(.caption.weight(.semibold)).foregroundStyle(.secondary).padding(.vertical, 8)
                Divider()
                ForEach(items, id: \.id) { item in
                    HStack(alignment: .center, spacing: 12) {
                        HStack(spacing: 10) {
                            widget.artwork(item, width: 28)
                            WaitingMark()
                            Text(item.title).font(.headline).lineLimit(2)
                        }
                        .frame(width: 300, alignment: .leading)
                        reasonLine(item).frame(maxWidth: .infinity, alignment: .leading)
                        Group {
                            if let place = item.opens { Button(widget.actionTitle(place)) {}.buttonStyle(.bordered) }
                        }
                        .frame(width: 190, alignment: .trailing)
                    }
                    .padding(.vertical, 8)
                    Divider()
                }
            }
            status.foregroundStyle(.secondary).padding(.top, 6)
        }
    }
}
