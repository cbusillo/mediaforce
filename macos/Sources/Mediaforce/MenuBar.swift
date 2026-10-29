import AppKit
import SwiftUI

/// The menu bar panel: a Mediaforce switch that turns work on and off, a line saying what it is doing, what waits
/// for the Director with each line opening its page, then plain rows to look for new files, open the window and
/// open Settings.
struct MenuBarPanel: View {
    let model: MainWindowModel
    @Environment(\.openWindow) private var openWindow
    @Environment(\.openSettings) private var openSettings

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            if let facts = model.facts {
                header(facts)
                let lines = Attention.menuLines(facts)
                if !lines.isEmpty {
                    Divider().padding(.horizontal, 10)
                    // The status line already says "Waiting for you" when nothing else is happening.
                    if Words.menuStatus(facts) != Words.waitingForYou {
                        Text(Words.waitingForYou)
                            .font(.system(size: 12, weight: .semibold))
                            .foregroundStyle(.secondary)
                            .padding(.horizontal, 14)
                            .padding(.top, 8)
                            .padding(.bottom, 2)
                    } else {
                        Spacer().frame(height: 6)
                    }
                    ForEach(lines, id: \.id) { line in
                        Button { open(line.opens) } label: {
                            WaitingLine(notice: line, posters: model.posters)
                        }
                        .buttonStyle(MenuRowStyle())
                    }
                }
            } else {
                Text(Words.menuNotConnected)
                    .foregroundStyle(.secondary)
                    .padding(.horizontal, 14)
                    .padding(.vertical, 12)
            }
            Divider().padding(.horizontal, 10).padding(.vertical, 5)
            Button(Words.lookForNewFilesNow) {
                Task { await model.perform { try await $0.lookForNewFiles() } }
            }
            .buttonStyle(MenuRowStyle())
            .disabled(model.connection != .reachable)
            Button(Words.openMediaforce) { open(nil) }
                .buttonStyle(MenuRowStyle())
            SettingsLink { Text(Words.settingsEllipsis) }
                .buttonStyle(MenuRowStyle())
                .simultaneousGesture(TapGesture().onEnded { NSApp.activate() })
        }
        .padding(.bottom, 6)
        .frame(width: 370)
        // Once the panel has shown a failed action, it doesn't keep saying so.
        .onDisappear { model.notice = nil }
    }

    private func header(_ facts: MainWindowFacts) -> some View {
        VStack(alignment: .leading, spacing: 2) {
            HStack {
                Text(Words.appName).font(.headline)
                Spacer()
                // Shows the requested state until the server confirms it.
                Toggle(Words.appName, isOn: Binding(
                    get: { model.switching ?? !facts.work.pausedByOwner },
                    set: { on in Task { await model.setRunning(on) } }
                ))
                .toggleStyle(.switch)
                .labelsHidden()
                .disabled(model.switching != nil || model.connection != .reachable)
                .help(facts.work.pausedByOwner ? Words.resumeHelp : Words.pauseHelp(facts.work))
            }
            // Facts kept from before the server stopped answering are not said as if current.
            if model.connection != .reachable {
                Text(Words.staleNotice(model.connection, lastHeard: model.lastHeard))
                    .foregroundStyle(.secondary)
                    .lineLimit(3)
            } else if let place = facts.statusOpens {
                Button(Words.menuStatus(facts)) { open(place) }
                    .buttonStyle(QuietLinkStyle())
                    .lineLimit(2)
            } else {
                Text(Words.menuStatus(facts))
                    .foregroundStyle(.secondary)
                    .lineLimit(2)
            }
            // The main window may be closed, so an action that went wrong is said here too.
            if let notice = model.notice {
                Text(notice.title).foregroundStyle(.orange).lineLimit(2)
            }
            Button(Words.savedSoFar(facts.spaceSaved.bytes)) { open(.spaceSaved) }
                .buttonStyle(QuietLinkStyle())
                .monospacedDigit()
        }
        .font(.callout)
        .padding(.horizontal, 14)
        .padding(.top, 12)
        .padding(.bottom, 10)
    }

    /// Brings the main window forward, on a page when one is named, and closes the panel.
    private func open(_ place: Place?) {
        NSApp.keyWindow?.close()
        if case .computer(let key) = place {
            model.settingsRequest = SettingsRequest(computer: key)
            openSettings()
        } else {
            if let place { model.requestedPlace = place }
            openWindow(id: MainWindow.id)
        }
        NSApp.activate()
    }
}

/// One thing waiting for the Director: a small poster for a show, or a symbol for a computer or the backups.
struct WaitingLine: View {
    let notice: AttentionNotice
    let posters: PosterStore

    var body: some View {
        HStack(spacing: 9) {
            Group {
                if let show = showPrefix, let image = posters.image(for: show) {
                    Image(nsImage: image).resizable().aspectRatio(contentMode: .fill)
                } else {
                    Image(systemName: symbol).foregroundStyle(.secondary)
                }
            }
            .frame(width: 20, height: 30)
            .clipShape(RoundedRectangle(cornerRadius: 3, style: .continuous))
            Text(notice.title).lineLimit(2)
            Spacer(minLength: 0)
        }
        .frame(minHeight: 34)
    }

    private var showPrefix: String? {
        switch notice.opens {
        case .sample(let prefix), .item(let prefix): MediaPoster.titlePrefix(prefix)
        default: nil
        }
    }

    private var symbol: String {
        switch notice.opens {
        case .spaceSaved: "trash"
        case .computer: "desktopcomputer"
        default: "film"
        }
    }
}

/// A row as in a menu bar panel: full width, highlighted under the pointer.
struct MenuRowStyle: ButtonStyle {
    func makeBody(configuration: Configuration) -> some View {
        MenuRow(configuration: configuration)
    }

    private struct MenuRow: View {
        let configuration: Configuration
        @State private var hovering = false
        @Environment(\.isEnabled) private var isEnabled

        var body: some View {
            configuration.label
                .font(.system(size: 13))
                .frame(maxWidth: .infinity, minHeight: 22, alignment: .leading)
                .padding(.horizontal, 9)
                .background {
                    RoundedRectangle(cornerRadius: 5, style: .continuous)
                        .fill(Color.primary.opacity(hovering && isEnabled ? (configuration.isPressed ? 0.16 : 0.09) : 0))
                }
                .padding(.horizontal, 5)
                .contentShape(Rectangle())
                .foregroundStyle(isEnabled ? .primary : .secondary)
                .onHover { hovering = $0 }
        }
    }
}

/// The menu bar glyph: the compress mark, with a dot while something waits for the Director.
enum MenuBarGlyph {
    static func image(waiting: Bool) -> NSImage {
        let symbol = NSImage(systemSymbolName: "arrow.down.right.and.arrow.up.left", accessibilityDescription: Words.appName)?
            .withSymbolConfiguration(.init(pointSize: 13, weight: .semibold)) ?? NSImage()
        let size = NSSize(width: symbol.size.width + 5, height: max(symbol.size.height, 16))
        let image = NSImage(size: size, flipped: false) { _ in
            symbol.draw(at: NSPoint(x: 0, y: (size.height - symbol.size.height) / 2), from: .zero, operation: .sourceOver, fraction: 1)
            if waiting {
                NSColor.black.setFill()
                NSBezierPath(ovalIn: NSRect(x: size.width - 6, y: size.height - 6, width: 6, height: 6)).fill()
            }
            return true
        }
        // A template image, so the menu bar draws it in its own colour in light, dark and when highlighted.
        image.isTemplate = true
        return image
    }
}

extension MainWindowFacts {
    var statusOpens: Place? {
        if work.pausedByOwner || work.stopReasons.contains("storage_unavailable") { return nil }
        if work.stopReasons.contains("no_computer_ready") { return .computer(nil) }
        if work.runningCount > 0 { return work.runningSeasons.first.map { .item($0.prefix) } }
        if work.waitingCount > 0 { return nil }
        if work.checksPaused { return .library }
        return nil
    }
}

extension Attention {
    /// What the menu bar lists: the same waiting notices the app sends, with the backups when there are any.
    static func menuLines(_ facts: MainWindowFacts) -> [AttentionNotice] {
        waitingNotices(facts) + (facts.originalBackups.count > 0 ? [backupsNotice(facts.originalBackups)] : [])
    }
}
