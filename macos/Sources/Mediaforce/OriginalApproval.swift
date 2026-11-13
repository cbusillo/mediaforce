import CryptoKit
import Foundation
import LocalAuthentication
import SwiftUI

struct OriginalPreview: Decodable, Identifiable, Sendable {
    let previewId: String
    let manifestHash: String
    let canonicalJson: String
    let challenge: String
    var problems: [OriginalProblem]? = nil
    var problem: String? {
        guard let problems, !problems.isEmpty else { return nil }
        return problems.map { problem in
            problem.relPath.map { "\($0): \(problem.reason)" } ?? problem.reason
        }.joined(separator: " ")
    }
    var waiting: String? = nil
    var id: String { previewId }
    var validIdentifier: Bool { previewId.count == 32 && previewId.allSatisfy { $0.isHexDigit && !$0.isUppercase } }

    func fileList() throws -> OriginalFileList {
        let bytes = Data(canonicalJson.utf8)
        let hash = SHA256.hash(data: bytes).map { String(format: "%02x", $0) }.joined()
        let list = try JSONDecoder.mediaforce.decode(OriginalFileList.self, from: bytes)
        guard hash == manifestHash, list.previewId == previewId, list.challenge == challenge,
              list.ownerUid == getuid(), !list.items.isEmpty,
              Set(list.items.map(\.itemId)).count == list.items.count,
              validIdentifier else {
            throw OriginalApprovalError("This file list changed. Prepare it again before confirming.")
        }
        return list
    }
}

struct OriginalProblem: Codable, Hashable, Sendable {
    let itemId: Int?
    let relPath: String?
    let reason: String
}

struct OriginalDismissal: Codable {
    let manifestHash: String
    let requestDismissed: Bool
    let problems: [OriginalProblem]
}

struct OriginalFileList: Decodable, Sendable {
    let previewId: String
    let challenge: String
    let ownerUid: UInt32
    let assertion: String
    let items: [File]

    struct File: Decodable, Identifiable, Sendable {
        let itemId: Int
        let relPath: String
        let identity: Identity
        let checks: Checks
        let supersedesId: String?
        var id: Int { itemId }
    }

    struct Identity: Decodable, Sendable {
        let path: String
        let sha256: String
        let sizeBytes: Int64
    }

    struct Checks: Decodable, Sendable {
        let exceptionMatches: [String]?
        let exceptionReason: String?
    }
}

struct OriginalApprovalError: LocalizedError {
    let message: String
    init(_ message: String) { self.message = message }
    var errorDescription: String? { message }
}

struct OriginalConfirmation: Codable {
    let previewId: String
    let manifestHash: String
    let canonicalJson: String
    let challenge: String
    let ownerUid: UInt32
    let authentication: String
    let confirmedAt: String
}

@MainActor
struct OriginalApprovalStore {
    var directory = FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask)[0]
        .appending(path: "\(BackgroundService.isProof ? BackgroundService.label : "mediaforce")/mac-app/original-approvals")
    var authenticate: @MainActor (String) async throws -> Bool = authenticateOwner

    static func authenticateOwner(_ reason: String) async throws -> Bool {
        let context = LAContext()
        defer { context.invalidate() }
        return try await context.evaluatePolicy(.deviceOwnerAuthentication, localizedReason: reason)
    }

    func dismiss(_ preview: OriginalPreview) throws {
        guard preview.validIdentifier else { throw OriginalApprovalError("This request cannot be dismissed.") }
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        let file = directory.appending(path: "\(preview.previewId).dismissed")
        let saved = try? JSONDecoder.mediaforce.decode(OriginalDismissal.self, from: Data(contentsOf: file))
        let previous = saved?.manifestHash == preview.manifestHash ? saved : nil
        let dismissal = OriginalDismissal(manifestHash: preview.manifestHash,
            requestDismissed: previous?.requestDismissed == true || (preview.problems ?? []).isEmpty,
            problems: Array(Set((previous?.problems ?? []) + (preview.problems ?? []))))
        try JSONEncoder.mediaforce.encode(dismissal).write(to: file, options: .atomic)
    }

    func confirm(_ preview: OriginalPreview) async throws {
        try Task.checkCancellation()
        let list = try preview.fileList()
        let file = directory.appending(path: "\(preview.previewId).json")
        if FileManager.default.fileExists(atPath: file.path) {
            let saved = try JSONDecoder.mediaforce.decode(OriginalConfirmation.self, from: Data(contentsOf: file))
            guard saved.previewId == preview.previewId, saved.manifestHash == preview.manifestHash,
                  saved.canonicalJson == preview.canonicalJson, saved.challenge == preview.challenge,
                  saved.ownerUid == list.ownerUid, saved.authentication == "deviceOwnerAuthentication" else {
                throw OriginalApprovalError("The saved approval differs from this file list.")
            }
            return
        }
        guard try await authenticate("Confirm \(Words.count(list.items.count, "original file")) from the list you reviewed.") else {
            throw OriginalApprovalError("The original files were not confirmed.")
        }
        try Task.checkCancellation()
        let receipt = OriginalConfirmation(previewId: preview.previewId, manifestHash: preview.manifestHash,
            canonicalJson: preview.canonicalJson, challenge: preview.challenge, ownerUid: list.ownerUid,
            authentication: "deviceOwnerAuthentication", confirmedAt: Date.now.ISO8601Format())
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        try JSONEncoder.mediaforce.encode(receipt).write(to: file, options: .atomic)
    }
}

struct OriginalApprovalView: View {
    let preview: OriginalPreview
    let model: MainWindowModel
    @Environment(\.dismiss) private var dismiss
    @State private var saving = false
    @State private var problem: String?

    var body: some View {
        PageFrame(title: "Confirm original files") {
            if let problem = preview.problem {
                Text(problem)
                if let waiting = preview.waiting { Text(waiting).foregroundStyle(.secondary) }
                Button("Close") { dismiss() }
                Button("Dismiss these problems") { Task { await dismissRequest() } }
            } else if let waiting = preview.waiting {
                Text(waiting)
                Button("Close") { dismiss() }
            } else if let list = try? preview.fileList() {
                Text(list.assertion)
                Text("Confirm that \(Words.count(list.items.count, "file")) \(list.items.count == 1 ? "was" : "were") acquired independently, before Mediaforce compressed \(list.items.count == 1 ? "it" : "them").")
                    .foregroundStyle(.secondary)
                ForEach(list.items) { file in
                    VStack(alignment: .leading, spacing: 6) {
                        Text(file.relPath).font(.headline)
                        Text(file.identity.path).textSelection(.enabled)
                        Text(Words.size(file.identity.sizeBytes)).foregroundStyle(.secondary)
                        if let reason = file.checks.exceptionReason {
                            Text("Separate format exception").font(.headline)
                            ForEach(file.checks.exceptionMatches ?? [], id: \.self) { match in
                                let parts = match.split(separator: ":", maxSplits: 1)
                                if parts.count == 2 {
                                    Text(parts[0] == "codec" ? "Video format: \(parts[1].uppercased())" : "Encoding software: \(parts[1])")
                                }
                            }
                            Text(reason)
                        }
                        if file.supersedesId != nil { Text("Replaces the earlier original-file decision.") }
                        DisclosureGroup("Content check") {
                            Text(file.identity.sha256).font(.caption.monospaced()).textSelection(.enabled)
                        }
                    }
                    Divider()
                }
                DisclosureGroup("File list details") {
                    Text(preview.manifestHash).font(.caption.monospaced()).textSelection(.enabled)
                }
                if let problem { Text(problem).foregroundStyle(.red) }
                HStack {
                    Button("Cancel") { dismiss() }.keyboardShortcut(.cancelAction).disabled(saving)
                    Button("Dismiss request") { Task { await dismissRequest() } }.disabled(saving)
                    Button(saving ? "Confirming…" : "Confirm these originals") {
                        saving = true
                    }
                    .disabled(saving)
                    .buttonStyle(.borderedProminent)
                }
            } else {
                Text("This file list changed. Prepare it again before confirming.")
                Button("Close") { dismiss() }
                Button("Dismiss request") { Task { await dismissRequest() } }
            }
        }
        .frame(minWidth: 660, minHeight: 420)
        .interactiveDismissDisabled(saving)
        .task(id: saving) { if saving { await confirm() } }
    }

    @MainActor
    private func dismissRequest() async {
        do {
            try OriginalApprovalStore().dismiss(preview)
            await model.refresh()
            dismiss()
        } catch {
            problem = error.localizedDescription
        }
    }

    @MainActor
    private func confirm() async {
        defer { saving = false }
        do {
            try await OriginalApprovalStore().confirm(preview)
            try Task.checkCancellation()
            await model.refresh()
            dismiss()
        } catch {
            problem = error.localizedDescription
        }
    }
}
