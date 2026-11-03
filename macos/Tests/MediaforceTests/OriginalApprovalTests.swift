import CryptoKit
import Foundation
import XCTest
@testable import Mediaforce

func originalApprovalFixture() throws -> OriginalPreview {
        let id = UUID().uuidString.replacingOccurrences(of: "-", with: "").lowercased()
        let challenge = UUID().uuidString
        let body: [String: Any] = [
            "preview_id": id, "challenge": challenge, "owner_uid": getuid(), "assertion": "Independent acquisition",
            "items": [["item_id": 42, "rel_path": "tv/Show/episode.mkv", "attached": true,
                       "identity": ["path": "/fixture/episode.mkv", "sha256": "sha256:source", "size_bytes": 400000000],
                       "checks": [:]]],
        ]
        let data = try JSONSerialization.data(withJSONObject: body, options: [.sortedKeys, .withoutEscapingSlashes])
        let hash = SHA256.hash(data: data).map { String(format: "%02x", $0) }.joined()
        return OriginalPreview(previewId: id, manifestHash: hash, canonicalJson: String(decoding: data, as: UTF8.self), challenge: challenge)
}

@MainActor
final class OriginalApprovalTests: XCTestCase {

    func testAuthenticationFailureNeverRecordsAnApproval() async throws {
        let directory = FileManager.default.temporaryDirectory.appending(path: UUID().uuidString)
        let store = OriginalApprovalStore(directory: directory, authenticate: { _ in false })
        do {
            try await store.confirm(originalApprovalFixture())
            XCTFail("An unauthenticated original was recorded")
        } catch {
            XCTAssertFalse(FileManager.default.fileExists(atPath: directory.path))
        }
    }

    func testChangedFileListNeverReachesAuthentication() async throws {
        let original = try originalApprovalFixture()
        let changed = OriginalPreview(previewId: original.previewId, manifestHash: original.manifestHash,
                                      canonicalJson: original.canonicalJson.replacingOccurrences(of: "episode.mkv", with: "other.mkv"),
                                      challenge: original.challenge)
        var authenticated = false
        let directory = FileManager.default.temporaryDirectory.appending(path: UUID().uuidString)
        let store = OriginalApprovalStore(directory: directory, authenticate: { _ in authenticated = true; return true })
        do {
            try await store.confirm(changed)
            XCTFail("A changed file list was accepted")
        } catch {
            XCTAssertFalse(authenticated)
            XCTAssertFalse(FileManager.default.fileExists(atPath: directory.path))
        }
    }

    func testCancelledAuthenticationCannotRecordAnApprovalEvenIfPromptReturnsSuccess() async throws {
        let original = try originalApprovalFixture()
        let directory = FileManager.default.temporaryDirectory.appending(path: UUID().uuidString)
        var grant: CheckedContinuation<Bool, Never>?
        let store = OriginalApprovalStore(directory: directory, authenticate: { _ in
            await withCheckedContinuation { grant = $0 }
        })
        let operation = Task { try await store.confirm(original) }
        while grant == nil { await Task.yield() }
        operation.cancel()
        grant?.resume(returning: true)
        do {
            try await operation.value
            XCTFail("A cancelled confirmation was recorded")
        } catch {
            XCTAssertFalse(FileManager.default.fileExists(atPath: directory.path))
        }
    }

    func testApprovalKeepsExactlyTheReviewedListAndRetryReusesItsRecord() async throws {
        let original = try originalApprovalFixture()
        let directory = FileManager.default.temporaryDirectory.appending(path: UUID().uuidString)
        defer { try? FileManager.default.removeItem(at: directory) }
        var count = 0
        let store = OriginalApprovalStore(directory: directory, authenticate: { _ in count += 1; return true })
        XCTAssertEqual(try original.fileList().items.map(\.relPath), ["tv/Show/episode.mkv"])
        try await store.confirm(original)
        let path = directory.appending(path: "\(original.previewId).json")
        let bytes = try Data(contentsOf: path)
        let saved = try JSONDecoder.mediaforce.decode(OriginalConfirmation.self, from: bytes)
        XCTAssertEqual(saved.canonicalJson, original.canonicalJson)
        XCTAssertEqual(saved.manifestHash, original.manifestHash)
        XCTAssertEqual(saved.challenge, original.challenge)
        try await store.confirm(original)
        XCTAssertEqual(count, 1)
        XCTAssertEqual(try Data(contentsOf: path), bytes)
    }
}
