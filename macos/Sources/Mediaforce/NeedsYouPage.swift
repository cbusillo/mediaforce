import SwiftUI

struct NeedsYouPage: View {
    let facts: MainWindowFacts
    let posters: PosterStore
    let open: (Place) -> Void

    var body: some View {
        PageFrame(title: Words.needsYou, subtitle: "\(Attention.waitingOnYou(facts)) waiting") {
            let items = Attention.waitingItems(facts)
            if items.isEmpty {
                Text("Nothing needs you right now.")
                    .foregroundStyle(.secondary)
            }
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
    }

    @ViewBuilder
    private func artwork(_ item: AttentionNotice) -> some View {
        switch item.opens {
        case .sample(let prefix), .item(let prefix):
            MediaPoster(titlePrefix: MediaPoster.titlePrefix(prefix), name: item.title,
                        kind: MediaKind(rawValue: String(prefix.split(separator: "/").first ?? "")) ?? .other,
                        posters: posters, width: 44)
        default:
            Image(systemName: item.opens == .spaceSaved ? "trash" : "desktopcomputer")
                .font(.title2)
                .foregroundStyle(.secondary)
                .frame(width: 44, height: 66)
                .accessibilityHidden(true)
        }
    }

    private func actionTitle(_ place: Place) -> String {
        switch place {
        case .sample: "Open sample"
        case .item: "Open"
        case .computer: "Open computer"
        case .spaceSaved: "Review original backups"
        default: "Open"
        }
    }
}
