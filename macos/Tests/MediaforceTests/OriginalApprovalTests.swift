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
                       "checks": ["exception_matches": ["codec:av1"], "exception_reason": "Independently acquired AV1 download"]]],
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
        let deadline = ContinuousClock.now + .seconds(5)
        while grant == nil && ContinuousClock.now < deadline { await Task.yield() }
        guard grant != nil else { operation.cancel(); XCTFail("Authentication did not start"); return }
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
    func testNativeReceiptEncodingMatchesTheBackendFixture() async throws {
        let resource = try XCTUnwrap(Bundle(for: OriginalApprovalTests.self).url(forResource: "original-confirmation", withExtension: "json"))
        let bytes = try Data(contentsOf: resource)
        var expected = try XCTUnwrap(JSONSerialization.jsonObject(with: bytes) as? [String: Any])
        let original = try originalApprovalFixture()
        let directory = FileManager.default.temporaryDirectory.appending(path: UUID().uuidString)
        defer { try? FileManager.default.removeItem(at: directory) }
        try await OriginalApprovalStore(directory: directory, authenticate: { _ in true }).confirm(original)
        let emitted = try Data(contentsOf: directory.appending(path: "\(original.previewId).json"))
        let actual = try XCTUnwrap(JSONSerialization.jsonObject(with: emitted) as? [String: Any])
        expected["preview_id"] = original.previewId
        expected["manifest_hash"] = original.manifestHash
        expected["canonical_json"] = original.canonicalJson
        expected["challenge"] = original.challenge
        expected["owner_uid"] = getuid()
        expected["confirmed_at"] = actual["confirmed_at"]
        XCTAssertNotNil(ISO8601DateFormatter().date(from: try XCTUnwrap(actual["confirmed_at"] as? String)))
        XCTAssertEqual(actual as NSDictionary, expected as NSDictionary)
    }

    func testDismissalKeepsTheDisplayedMemberProblemsAndMatchesTheBackendFixture() throws {
        let resource = try XCTUnwrap(Bundle(for: OriginalApprovalTests.self).url(forResource: "original-dismissal", withExtension: "json"))
        let fixture = try Data(contentsOf: resource)
        let problemList = try JSONDecoder.mediaforce.decode(OriginalDismissal.self, from: fixture)
        var preview = try originalApprovalFixture()
        var response = try XCTUnwrap(JSONSerialization.jsonObject(with: fixture) as? [String: Any])
        response["preview_id"] = preview.previewId
        response["manifest_hash"] = preview.manifestHash
        response["canonical_json"] = preview.canonicalJson
        response["challenge"] = preview.challenge
        preview = try JSONDecoder.mediaforce.decode(OriginalPreview.self, from: JSONSerialization.data(withJSONObject: response))
        XCTAssertFalse(try preview.fileList().items.isEmpty)
        XCTAssertTrue(try XCTUnwrap(preview.problem).contains(try XCTUnwrap(problemList.problems.first).reason))
        let directory = FileManager.default.temporaryDirectory.appending(path: UUID().uuidString)
        defer { try? FileManager.default.removeItem(at: directory) }
        let store = OriginalApprovalStore(directory: directory, authenticate: { _ in
            XCTFail("Dismissing a problem must not confirm an original")
            return true
        })
        try store.dismiss(preview)
        let file = directory.appending(path: "\(preview.previewId).dismissed")
        let actual = try XCTUnwrap(JSONSerialization.jsonObject(with: Data(contentsOf: file)) as? [String: Any])
        var expected = try XCTUnwrap(JSONSerialization.jsonObject(with: fixture) as? [String: Any])
        expected["manifest_hash"] = preview.manifestHash
        XCTAssertEqual(actual as NSDictionary, expected as NSDictionary)
        let next = OriginalProblem(itemId: 99, relPath: "tv/Fixture/other.mkv", reason: "A different file needs attention")
        preview.problems = [next]
        try store.dismiss(preview)
        let saved = try JSONDecoder.mediaforce.decode(OriginalDismissal.self, from: Data(contentsOf: file))
        XCTAssertEqual(Set(saved.problems), Set(problemList.problems + [next]))
        preview.problems = []
        try store.dismiss(preview)
        let request = try JSONDecoder.mediaforce.decode(OriginalDismissal.self, from: Data(contentsOf: file))
        XCTAssertTrue(request.requestDismissed)
        XCTAssertEqual(Set(request.problems), Set(saved.problems))
        XCTAssertFalse(FileManager.default.fileExists(atPath: directory.appending(path: "\(preview.previewId).json").path))
    }

}
