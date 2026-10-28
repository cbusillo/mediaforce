import SwiftUI

/// A page that names itself in the window's title bar, as Mail names a mailbox, and starts with its content.
struct PageFrame<Content: View>: View {
    let title: String
    var subtitle = ""
    @ViewBuilder let content: Content

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 18) {
                content
            }
            .padding(.horizontal, 34)
            .padding(.vertical, 20)
            .frame(maxWidth: .infinity, alignment: .leading)
        }
        .scrollBounceBehavior(.basedOnSize)
        .background(Color(nsColor: .textBackgroundColor))
        .navigationTitle(title)
        .navigationSubtitle(subtitle)
    }
}

// A page that reads its own facts while it is open, refreshing on the main window's interval and keeping what it last heard.
struct RefreshingPage<Facts: Sendable, Content: View>: View {
    let title: String
    var subtitle = ""
    // A subtitle that counts what the facts hold; the plain subtitle stands until they arrive.
    var factsSubtitle: ((Facts) -> String)?
    let load: @MainActor () async throws -> Facts
    @ViewBuilder let content: (Facts) -> Content
    @State private var facts: Facts?
    @State private var refreshFailed = false
    @State private var lastHeard: Date?

    var body: some View {
        PageFrame(title: title, subtitle: facts.flatMap { factsSubtitle?($0) } ?? subtitle) {
            if let facts {
                if refreshFailed {
                    StaleNotice(connection: .failing, lastHeard: lastHeard)
                        .padding(.horizontal, -34)
                }
                content(facts)
            } else if refreshFailed {
                Text(Words.staleNotice(.failing, lastHeard: nil))
                    .foregroundStyle(.secondary)
            } else {
                ProgressView()
            }
        }
        .task { await watch() }
    }

    private func watch() async {
        while !Task.isCancelled {
            do {
                facts = try await load()
                refreshFailed = false
                lastHeard = .now
            } catch {
                refreshFailed = true
            }
            try? await Task.sleep(for: MainWindowModel.refreshInterval)
        }
    }
}

struct LibraryStatePicker: View {
    @Binding var selection: LibraryStateGroup?

    var body: some View {
        Picker(Words.stateColumn, selection: $selection) {
            Text(Words.allStates).tag(LibraryStateGroup?.none)
            ForEach(LibraryStateGroup.allCases) { choice in
                Text(Words.libraryState(choice)).tag(Optional(choice))
            }
        }
        .fixedSize()
    }
}

