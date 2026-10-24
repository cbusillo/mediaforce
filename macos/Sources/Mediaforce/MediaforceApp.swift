import SwiftUI

@main
struct MediaforceApp: App {
    @NSApplicationDelegateAdaptor(AppDelegate.self) private var appDelegate
    @State private var monitor = ServerMonitor(connection: ServerConnection(baseURL: ServerConnection.localServer))

    var body: some Scene {
        WindowGroup {
            ServerStatusView(monitor: monitor)
                .frame(minWidth: 560, minHeight: 280)
        }
        .windowResizability(.contentMinSize)
    }
}
