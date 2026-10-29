import SwiftUI

struct NeedsYouPage: View {
    let facts: MainWindowFacts
    let open: (Place) -> Void

    var body: some View {
        PageFrame(title: Words.needsYou, subtitle: "\(Attention.waitingOnYou(facts)) waiting") {
            let items = Attention.waitingItems(facts)
            if items.isEmpty {
                Text("Nothing needs you right now.")
                    .foregroundStyle(.secondary)
            }
            ForEach(items, id: \.id) { item in
                DecisionLine {
                    VStack(alignment: .leading, spacing: 4) {
                        Text(item.title).font(.headline)
                        Text(item.body).foregroundStyle(.secondary)
                    }
                } buttons: {
                    if let place = item.opens {
                        Button(actionTitle(place)) { open(place) }
                    }
                }
                Divider()
            }
        }
    }

    private func actionTitle(_ place: Place) -> String {
        switch place {
        case .sample: "Open sample"
        case .item: "Open season"
        case .computer: "Open computer"
        case .spaceSaved: "Review original backups"
        default: "Open"
        }
    }
}
