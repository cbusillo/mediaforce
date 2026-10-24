import SwiftUI

struct ServerStatusView: View {
    let state: ServerState
    let retry: () -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            Text("Mediaforce")
                .font(.system(size: 30, weight: .bold))
                .padding(.bottom, 18)

            HStack(alignment: .top, spacing: 14) {
                Image(systemName: symbol)
                    .font(.system(size: 24, weight: .regular))
                    .foregroundStyle(.secondary)
                    .frame(width: 32, alignment: .center)
                    .padding(.top, 1)
                VStack(alignment: .leading, spacing: 4) {
                    Text(headline)
                        .font(.title3.weight(.semibold))
                    Text(explanation)
                        .foregroundStyle(.secondary)
                        .fixedSize(horizontal: false, vertical: true)
                        .frame(maxWidth: 460, alignment: .leading)
                    if state != .reachable && state != .checking {
                        Button("Check now", action: retry)
                        .padding(.top, 10)
                    }
                }
                Spacer(minLength: 0)
            }
            Spacer(minLength: 0)
        }
        .padding(.horizontal, 34)
        .padding(.top, 24)
        .padding(.bottom, 24)
        .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)
        .background(Color(nsColor: .textBackgroundColor))
    }

    private var symbol: String {
        switch state {
        case .checking: "ellipsis.circle"
        case .reachable: "checkmark.circle"
        case .unreachable: "bolt.horizontal.circle"
        case .failing: "exclamationmark.circle"
        }
    }

    private var headline: String {
        switch state {
        case .checking: "Looking for Mediaforce on this Mac…"
        case .reachable: "Mediaforce is running on this Mac."
        case .unreachable: "Can't reach Mediaforce on this Mac."
        case .failing: "Mediaforce answered with an error."
        }
    }

    private var explanation: String {
        switch state {
        case .checking:
            "This only takes a moment."
        case .reachable:
            "Your library will appear here in a later version. For now this window shows that the app can reach Mediaforce."
        case .unreachable:
            "It isn't answering, so this window can't tell what it's doing. It checks again every few seconds and updates by itself."
        case .failing:
            "This window can't tell what it's doing until it answers properly. It checks again every few seconds and updates by itself."
        }
    }
}
