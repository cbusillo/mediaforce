import SwiftUI

/// A page that names itself in the window's title bar, as Mail names a mailbox, and starts with its content.
struct PageFrame<Content: View>: View {
    let title: String
    var subtitle = ""
    var scrolls = true
    @ViewBuilder let content: Content

    var body: some View {
        Group {
            if scrolls {
                ScrollView { document }
            } else {
                document
            }
        }
        .scrollBounceBehavior(.basedOnSize)
        .background(Color(nsColor: .textBackgroundColor))
        .navigationTitle(title)
        .navigationSubtitle(subtitle)
    }

    private var document: some View {
        VStack(alignment: .leading, spacing: 18) {
            content
        }
        .padding(.horizontal, 34)
        .padding(.vertical, 20)
        .frame(maxWidth: .infinity, maxHeight: scrolls ? nil : .infinity, alignment: .topLeading)
    }
}

/// What a refreshing page last heard. A page whose store outlives it, like Activity's in the main window's model, shows
/// that at once when it opens again and refreshes behind it.
@MainActor
@Observable
final class PageFacts<Facts: Sendable> {
    var facts: Facts?
    var refreshFailed = false
    var lastHeard: Date?
}

// A page that reads its own facts while it is open, refreshing on the main window's interval and keeping what it last heard.
struct RefreshingPage<Facts: Sendable, Content: View>: View {
    let title: String
    let subtitle: String
    let load: @MainActor () async throws -> Facts
    @ViewBuilder let content: (Facts) -> Content
    @State private var heard: PageFacts<Facts>

    init(title: String, subtitle: String, heard: PageFacts<Facts> = PageFacts(), load: @escaping @MainActor () async throws -> Facts,
         @ViewBuilder content: @escaping (Facts) -> Content) {
        self.title = title
        self.subtitle = subtitle
        self.load = load
        self.content = content
        _heard = State(initialValue: heard)
    }

    var body: some View {
        PageFrame(title: title, subtitle: subtitle) {
            if let facts = heard.facts {
                if heard.refreshFailed {
                    StaleNotice(connection: .failing, lastHeard: heard.lastHeard)
                        .padding(.horizontal, -34)
                }
                content(facts)
            } else if heard.refreshFailed {
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
                heard.facts = try await load()
                heard.refreshFailed = false
                heard.lastHeard = .now
            } catch {
                heard.refreshFailed = true
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

/// A decision as one plain line with its buttons beside it, as on a kind's page. When the buttons leave the line too
/// little room, they move under it instead of squeezing it.
struct DecisionLine<Line: View, Buttons: View>: View {
    @ViewBuilder let line: Line
    @ViewBuilder let buttons: Buttons

    var body: some View {
        ViewThatFits(in: .horizontal) {
            HStack(alignment: .firstTextBaseline, spacing: 8) {
                line
                Spacer(minLength: 12)
                HStack(spacing: 8) { buttons }.fixedSize()
            }
            VStack(alignment: .leading, spacing: 8) {
                line.fixedSize(horizontal: false, vertical: true)
                HStack(spacing: 8) { buttons }.fixedSize()
            }
        }
    }
}
