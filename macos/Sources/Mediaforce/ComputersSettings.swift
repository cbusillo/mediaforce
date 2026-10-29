import SwiftUI

/// What the server knows about each computer's setup, from `/api/hosts`: whether it can be used now and which of
/// its fixes the server offers. The buttons follow the same rules the web page uses.
struct HostsFacts: Decodable, Sendable {
    let hosts: [Host]

    struct Host: Decodable, Sendable {
        let key: String
        let available: Bool
        let setupSupported: Bool
        let setupRequiresPassword: Bool
        let trustResetSupported: Bool
        let issues: [Ignored]
        let recovery: Recovery?

        struct Recovery: Decodable, Sendable {
            let status: String
            let reason: String?
        }

        /// Offered while the server is recovering a computer it can't use.
        var canCheckAgain: Bool { !available && (recovery.map { $0.status != "ready" } ?? false) }
        var canStart: Bool { !available && setupSupported }
        /// Setting up again is offered unless the computer is ready with nothing to fix.
        var canSetUp: Bool { setupSupported && !(available && !setupRequiresPassword && issues.isEmpty) }
    }

    /// An issue the server lists; only how many there are matters here.
    struct Ignored: Decodable, Sendable {
        init(from decoder: Decoder) {}
    }
}

struct HostActionResult: Decodable, Sendable {
    let ok: Bool
    let message: String?
}

enum ComputerAction: CaseIterable {
    case checkAgain, start, setUp, trustAgain
}

/// The one fix a computer's state calls for, shown first and prominent when the server offers it.
enum ComputerFix {
    static func primary(for state: String) -> ComputerAction? {
        switch state {
        case "unreachable", "new_name", "not_found", "remote_login_off": .checkAgain
        case "storage_unreachable", "unreadable_reply", "python_missing", "password_needed": .setUp
        case "identity_changed": .trustAgain
        default: nil
        }
    }

    static func offered(_ host: HostsFacts.Host) -> [ComputerAction] {
        ComputerAction.allCases.filter { action in
            switch action {
            case .checkAgain: host.canCheckAgain
            case .start: host.canStart
            case .setUp: host.canSetUp
            case .trustAgain: host.trustResetSupported
            }
        }
    }
}

/// A computer's state and its fixes, above its settings. The password is used once and never kept.
struct ComputerStatus: View {
    let name: String
    let state: String?
    let needsYou: Bool
    let host: HostsFacts.Host?
    let act: (ComputerAction, String?) async -> HostActionResult?
    @State private var password = ""
    @State private var working: ComputerAction?
    @State private var result: HostActionResult?

    var body: some View {
        let actions = host.map(ComputerFix.offered) ?? []
        let primary = state.flatMap(ComputerFix.primary(for:)).flatMap { actions.contains($0) ? $0 : nil }
        VStack(alignment: .leading, spacing: 10) {
            // The decision: the state as one plain line, the fixes beside it, and the password a fix needs just before it.
            DecisionLine {
                HStack(alignment: .firstTextBaseline, spacing: 8) {
                    if needsYou { WaitingMark() }
                    Text(state.map(Words.computerState) ?? Words.computerNotHeardFrom)
                }
            } buttons: {
                if working != nil { ProgressView().controlSize(.small) }
                if host?.setupRequiresPassword == true, actions.contains(.setUp) {
                    SecureField(Words.accountPassword, text: $password, prompt: Text(Words.accountPasswordPrompt(name)))
                        .textFieldStyle(.roundedBorder)
                        .labelsHidden()
                        .frame(width: 180)
                        .onSubmit { if !password.isEmpty, working == nil { Task { await run(.setUp) } } }
                }
                ForEach(actions, id: \.self) { action in
                    let button = Button(Words.computerAction(action, needsPassword: host?.setupRequiresPassword == true)) {
                        Task { await run(action) }
                    }
                    .disabled(working != nil || (action == .setUp && host?.setupRequiresPassword == true && password.isEmpty))
                    if action == primary {
                        button.buttonStyle(.borderedProminent)
                    } else {
                        button
                    }
                }
            }
            if host?.recovery?.status == "action_required", let reason = host?.recovery?.reason {
                Text(reason)
                    .foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            }
            if let help = state.flatMap(Words.computerFixHelp) {
                Text(help)
                    .foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            }
            if let result, let message = result.message, !message.isEmpty {
                Label(message, systemImage: result.ok ? "checkmark" : "exclamationmark.triangle")
                    .foregroundStyle(result.ok ? AnyShapeStyle(.secondary) : AnyShapeStyle(Color.orange))
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
        .padding(.vertical, 4)
    }

    private func run(_ action: ComputerAction) async {
        working = action
        let sent = action == .setUp && !password.isEmpty ? password : nil
        password = ""
        result = await act(action, sent)
        working = nil
    }
}
