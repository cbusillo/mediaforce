import SwiftUI

/// The server's answer to a Stop: done, or a refusal code the app words.
struct StopFileResult: Decodable, Sendable {
    let ok: Bool
    let code: String?
}

extension MediaforceClient {
    /// Stops the run of one file that started at `run`; it waits again and starts over later. A refusal answers 409 with a
    /// code: the file isn't running, started again, or is already finishing.
    func stopFile(_ id: String, run: String) async throws -> StopFileResult {
        var request = URLRequest(url: baseURL.appending(path: "api/encode-queue/running").appending(path: id).appending(path: "stop"))
        request.httpMethod = "POST"
        request.httpBody = try JSONSerialization.data(withJSONObject: ["run": run])
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        let (data, response) = try await session.data(for: request)
        guard [200, 409].contains((response as? HTTPURLResponse)?.statusCode) else {
            throw URLError(.badServerResponse)
        }
        return try JSONDecoder.mediaforce.decode(StopFileResult.self, from: data)
    }
}

/// Stop for one running file, beside it wherever it is shown running. Losing half a file or more asks first.
struct StopFileButton: View {
    let id: String
    let run: String
    let title: String
    let progress: Double?
    let model: MainWindowModel
    @State private var confirming = false

    static func asksFirst(_ progress: Double?) -> Bool { (progress ?? 0) >= 0.5 }

    var body: some View {
        if model.stopping.contains(id) {
            Text(Words.stoppingFile).foregroundStyle(.secondary)
        } else {
            Button(Words.stopFile, systemImage: "stop.circle") {
                if Self.asksFirst(progress) { confirming = true } else { Task { await model.stop(id, run: run) } }
            }
            .help(Words.stopFileHelp)
            .confirmationDialog(Words.stopFileQuestion(title), isPresented: $confirming) {
                Button(Words.stopFile, role: .destructive) { Task { await model.stop(id, run: run) } }
            } message: {
                Text(Words.stopFileConsequence(progress))
            }
        }
    }
}

extension ActionNotice {
    static func notStopped(_ code: String?) -> ActionNotice {
        let message = switch code {
        case "started_again": "That file already started again."
        case "finishing": "That file is already finishing."
        default: "That file isn't running anymore."
        }
        return ActionNotice(title: "Nothing was stopped", message: message)
    }
}

extension Words {
    static let stopFile = "Stop"
    static let stoppingFile = "Stopping. It will wait and start over."
    static let stopFileHelp = "Stops this file now. It goes back to waiting and starts over later."

    static func stopFileQuestion(_ title: String) -> String { "Stop \(title)?" }

    static func stopFileConsequence(_ progress: Double?) -> String {
        let done = progress.map { "It is \(Int(($0 * 100).rounded()))% done. " } ?? ""
        return "\(done)It goes back to waiting and starts over from the beginning later."
    }
}
