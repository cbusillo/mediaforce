import SwiftUI

@main
struct MediaforceApp: App {
    @NSApplicationDelegateAdaptor(AppDelegate.self) private var appDelegate

    var body: some Scene {
        // One window, so the menu bar can bring it forward instead of opening another.
        Window(Words.appName, id: MainWindow.id) {
            MainWindow(model: appDelegate.model)
        }
        .windowResizability(.contentMinSize)
        // The kind page's player, full screen.
        Window(Words.fullScreen, id: TheaterWindow.id) {
            TheaterWindow(model: appDelegate.model)
        }
        .windowStyle(.hiddenTitleBar)
        .defaultLaunchBehavior(.suppressed)
        .restorationBehavior(.disabled)
        MenuBarExtra {
            MenuBarPanel(model: appDelegate.model)
        } label: {
            MenuBarLabel(model: appDelegate.model)
        }
        .menuBarExtraStyle(.window)
        Settings {
            SettingsWindow(model: appDelegate.model)
        }
    }
}
