import SwiftUI

struct NeedsYouPage: View {
    let facts: MainWindowFacts
    let posters: PosterStore
    let model: MainWindowModel
    let open: (Place) -> Void
    @State private var originalPreview: OriginalPreview?

    var body: some View {
        PageFrame(title: Words.needsYou, subtitle: "\(Attention.waitingItems(facts).count) waiting") {
            let items = Attention.waitingItems(facts).filter { !$0.id.hasPrefix("original:") }
            let previews = facts.originalPreviews ?? []
            if items.isEmpty && previews.isEmpty {
                Text("Nothing needs you right now.")
                    .foregroundStyle(.secondary)
            }
            WaitingItems(items: items, posters: posters, open: open)
            ForEach(previews) { preview in
                DecisionLine {
                    if let problem = preview.problem {
                        Text(problem)
                    } else if let waiting = preview.waiting {
                        Text(waiting)
                    } else if let list = try? preview.fileList() {
                        Text("\(list.items.count) original files need your confirmation")
                    } else {
                        Text("An original-file request needs preparing again")
                    }
                } buttons: {
                    Button("Review file list") { originalPreview = preview }
                }
            }
        }
        .sheet(item: $originalPreview) { OriginalApprovalView(preview: $0, model: model) }
    }
}

struct WaitingItems: View {
    let items: [AttentionNotice]
    let posters: PosterStore
    let open: (Place) -> Void

    var body: some View {
            ForEach(items, id: \.id) { item in
                HStack(alignment: .top, spacing: 14) {
                    artwork(item)
                    VStack(alignment: .leading, spacing: 8) {
                        HStack(alignment: .firstTextBaseline, spacing: 6) {
                            WaitingMark()
                            if let place = item.opens {
                                Button(item.title) { open(place) }
                                    .buttonStyle(QuietLinkStyle(primary: true))
                            } else {
                                Text(item.title)
                            }
                        }
                        .font(.headline)
                        DecisionLine {
                            Text(item.body).foregroundStyle(.secondary)
                        } buttons: {
                            if let place = item.opens {
                                Button(actionTitle(place)) { open(place) }
                            }
                        }
                    }
                    .frame(maxWidth: .infinity, alignment: .leading)
                }
                Divider()
            }
    }

    @ViewBuilder
    private func artwork(_ item: AttentionNotice) -> some View {
        switch item.opens {
        case .sample(let prefix), .item(let prefix):
            MediaPoster(titlePrefix: MediaPoster.titlePrefix(prefix), name: item.title,
                        kind: MediaKind(rawValue: String(prefix.split(separator: "/").first ?? "")) ?? .other,
                        posters: posters, width: 44)
        default:
            Image(systemName: symbol(item.opens))
                .font(.title2)
                .foregroundStyle(.secondary)
                .frame(width: 44, height: 66)
                .accessibilityHidden(true)
        }
    }

    private func symbol(_ place: Place?) -> String {
        switch place {
        case .kind: "film"
        case .spaceSaved: "trash"
        default: "desktopcomputer"
        }
    }

    private func actionTitle(_ place: Place) -> String {
        switch place {
        case .kind: "Review kind"
        case .sample: "Open sample"
        case .item: "Open"
        case .computer: "Open computer"
        case .spaceSaved: "Review original backups"
        default: "Open"
        }
    }
}
