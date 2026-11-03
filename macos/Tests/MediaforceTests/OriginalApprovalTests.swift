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

    func testFailedRequestKeepsItsProblemWithoutChangingTheReviewedList() throws {
        let original = try originalApprovalFixture()
        let problem = "This file changed during its original-file check."
        let bytes = try JSONSerialization.data(withJSONObject: ["preview_id": original.previewId,
            "manifest_hash": original.manifestHash, "canonical_json": original.canonicalJson,
            "challenge": original.challenge, "problem": problem])
        let failed = try JSONDecoder.mediaforce.decode(OriginalPreview.self, from: bytes)
        XCTAssertEqual(failed.problem, problem)
        XCTAssertEqual(try failed.fileList().items.map(\.itemId), try original.fileList().items.map(\.itemId))
    }

    func testApplySendsExactApprovalReferenceAndAcceptsQueuedResponse() async throws {
        let configuration = URLSessionConfiguration.ephemeral
        configuration.protocolClasses = [OriginalApplyServer.self]
        let session = URLSession(configuration: configuration)
        defer { session.invalidateAndCancel() }
        let preview = try originalApprovalFixture()
        let client = MediaforceClient(baseURL: URL(string: "http://mediaforce.invalid")!, session: session)
        OriginalApplyServer.configure(preview)
        try await withThrowingTaskGroup(of: Void.self) { group in
            group.addTask { try await client.applyOriginalPreview(preview) }
            group.addTask { try await Task.sleep(for: .seconds(2)); throw URLError(.timedOut) }
            defer { group.cancelAll() }
            _ = try await group.next()
        }
    }

}

private final class OriginalApplyServer: URLProtocol, @unchecked Sendable {
    private static let lock = NSLock()
    nonisolated(unsafe) private static var expected: [String: String] = [:]
    static func configure(_ preview: OriginalPreview) {
        lock.lock()
        defer { lock.unlock() }
        expected = ["preview_id": preview.previewId, "manifest_hash": preview.manifestHash]
    }
    override class func canInit(with request: URLRequest) -> Bool { true }
    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }
    override func startLoading() {
        let body = request.httpBody ?? request.httpBodyStream.flatMap { stream -> Data? in
            stream.open()
            defer { stream.close() }
            var bytes = Data()
            var buffer = [UInt8](repeating: 0, count: 4096)
            while stream.hasBytesAvailable {
                let size = stream.read(&buffer, maxLength: buffer.count)
                if size <= 0 { break }
                bytes.append(contentsOf: buffer.prefix(size))
            }
            return bytes
        }
        let fields = body.flatMap { try? JSONSerialization.jsonObject(with: $0) as? [String: String] }
        Self.lock.lock()
        XCTAssertEqual(fields, Self.expected)
        Self.lock.unlock()
        client?.urlProtocol(self, didReceive: HTTPURLResponse(url: request.url!, statusCode: 202,
            httpVersion: nil, headerFields: nil)!, cacheStoragePolicy: .notAllowed)
        client?.urlProtocol(self, didLoad: Data("{}".utf8))
        client?.urlProtocolDidFinishLoading(self)
    }
    override func stopLoading() {}
}
