import AppKit

@main
enum MediaforceMain {
    @MainActor
    static func main() {
        guard CommandLine.arguments.dropFirst().first == "render" else {
            MediaforceApp.main()
            return
        }
        NSApplication.shared.setActivationPolicy(.prohibited)
        Task { @MainActor in
            do {
                let options = try RenderOptions(arguments: Array(CommandLine.arguments.dropFirst(2)))
                try await RenderCommand.run(options)
                exit(0)
            } catch {
                FileHandle.standardError.write(Data("Render failed: \(error)\n".utf8))
                exit(1)
            }
        }
        NSApp.run()
    }
}
