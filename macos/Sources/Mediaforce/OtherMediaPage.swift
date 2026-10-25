import SwiftUI

struct OtherMediaPage: View {
    let model: MainWindowModel
    @State private var search = ""
    @State private var stateGroup: LibraryStateGroup?
    @State private var selected: OtherMediaFacts.Item.ID?
    @State private var sortOrder = [KeyPathComparator(\OtherMediaFacts.Item.bytes, order: .reverse)]
    @Environment(\.openURL) private var openURL

    var body: some View {
        RefreshingPage(title: Words.otherMedia, load: { try await model.client.otherMedia() }) { (facts: OtherMediaFacts) in
            let items = visibleItems(facts)
            HStack(spacing: 12) {
                TextField(Words.searchOtherMedia, text: $search)
                    .textFieldStyle(.roundedBorder)
                    .frame(width: 260)
                LibraryStatePicker(selection: $stateGroup)
                Spacer()
                Text(Words.otherMediaCount(items.count, of: facts.items.count, files: facts.fileCount))
                    .foregroundStyle(.secondary)
                    .monospacedDigit()
            }
            if facts.truncated {
                Text(Words.otherMediaTruncated)
                    .foregroundStyle(.secondary)
            }
            OtherMediaTable(items: items, selected: $selected, sortOrder: $sortOrder)
            if items.isEmpty {
                Text(Words.noMatchingOtherMedia)
                    .foregroundStyle(.secondary)
            } else if let item = items.first(where: { $0.id == selected }) {
                OtherMediaDetail(item: item) { openURL(model.client.seasonPage(item.prefix)) }
            } else {
                Text(Words.chooseOtherMedia)
                    .foregroundStyle(.secondary)
            }
        }
    }

    private func visibleItems(_ facts: OtherMediaFacts) -> [OtherMediaFacts.Item] {
        facts.items
            .filter { $0.matches(search) && (stateGroup == nil || $0.stateGroup == stateGroup) }
            .sorted(using: sortOrder)
    }
}

struct OtherMediaTable: View {
    let items: [OtherMediaFacts.Item]
    @Binding var selected: OtherMediaFacts.Item.ID?
    @Binding var sortOrder: [KeyPathComparator<OtherMediaFacts.Item>]

    var body: some View {
        Table(items, selection: $selected, sortOrder: $sortOrder) {
            TableColumn(Words.nameColumn, value: \OtherMediaFacts.Item.title) { (item: OtherMediaFacts.Item) in
                Text(item.title)
            }
            TableColumn(Words.filesColumn, value: \OtherMediaFacts.Item.fileCount) { (item: OtherMediaFacts.Item) in
                Text("\(item.fileCount)")
                    .monospacedDigit()
                    .frame(maxWidth: .infinity, alignment: .trailing)
            }
            .width(50)
            TableColumn(Words.sizeColumn, value: \OtherMediaFacts.Item.bytes) { (item: OtherMediaFacts.Item) in
                Text(Words.size(item.bytes))
                    .monospacedDigit()
                    .frame(maxWidth: .infinity, alignment: .trailing)
            }
            .width(80)
            TableColumn(Words.estimatedSavedColumn, value: \OtherMediaFacts.Item.estimatedSavedSortBytes) { (item: OtherMediaFacts.Item) in
                Text(item.estimatedSavedBytes.map(Words.size) ?? Words.notKnownYet)
                    .monospacedDigit()
                    .foregroundStyle(item.estimatedSavedBytes == nil ? HierarchicalShapeStyle.secondary : HierarchicalShapeStyle.primary)
                    .frame(maxWidth: .infinity, alignment: .trailing)
            }
            .width(150)
            TableColumn(Words.stateColumn, value: \OtherMediaFacts.Item.stateRank) { (item: OtherMediaFacts.Item) in
                Text(Words.otherMediaState(item.state))
            }
            .width(140)
        }
        .tableStyle(.inset(alternatesRowBackgrounds: true))
        .frame(height: TVShowsTable.height)
    }
}

struct OtherMediaDetail: View {
    let item: OtherMediaFacts.Item
    let openInBrowser: () -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            Text(item.title)
                .font(.headline)
            Text(Words.otherMediaDetail(item))
                .foregroundStyle(.secondary)
            Button(Words.openInBrowser, action: openInBrowser)
                .controlSize(.small)
        }
    }
}
