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
        MenuBarExtra {
            MenuBarPanel(model: appDelegate.model)
        } label: {
            Image(nsImage: MenuBarGlyph.image(waiting: appDelegate.model.facts.map(Attention.waitingOnYou) ?? 0 > 0))
        }
        .menuBarExtraStyle(.window)
        Settings {
            SettingsWindow(model: appDelegate.model)
        }
    }
}
