import SwiftUI

@main
struct MediaforceApp: App {
    @NSApplicationDelegateAdaptor(AppDelegate.self) private var appDelegate

    var body: some Scene {
        WindowGroup {
            MainWindow(model: appDelegate.model)
        }
        .windowResizability(.contentMinSize)
        Settings {
            SettingsWindow(client: appDelegate.model.client)
        }
    }
}
