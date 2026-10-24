import SwiftUI

struct ServerStatusView: View {
    let monitor: ServerMonitor

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
                    if monitor.state != .reachable && monitor.state != .checking {
                        Button("Check now") {
                            Task { await monitor.refresh() }
                        }
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
        .task { await monitor.watch() }
    }

    private var symbol: String {
        switch monitor.state {
        case .checking: "ellipsis.circle"
        case .reachable: "checkmark.circle"
        case .unreachable: "bolt.horizontal.circle"
        case .failing: "exclamationmark.circle"
        }
    }

    private var headline: String {
        switch monitor.state {
        case .checking: "Looking for Mediaforce on this Mac…"
        case .reachable: "Mediaforce is running on this Mac."
        case .unreachable: "Mediaforce isn't running on this Mac."
        case .failing: "Mediaforce is running but isn't answering properly."
        }
    }

    private var explanation: String {
        switch monitor.state {
        case .checking:
            "This only takes a moment."
        case .reachable:
            "Your library will appear here in a later version. For now this window shows that the app can reach Mediaforce."
        case .unreachable:
            "Nothing is being compressed until it starts again. This window checks every few seconds and updates by itself."
        case .failing:
            "It answered with an error. This window checks every few seconds and updates by itself."
        }
    }
}
