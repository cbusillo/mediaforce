import Foundation
import XCTest
@testable import Mediaforce

final class MenuBarTests: XCTestCase {
    /// The shared facts with their work replaced, and with nothing waiting for the owner unless asked.
    private func facts(work: String, ownerWaiting: Bool = false) throws -> MainWindowFacts {
        var json = try XCTUnwrap(JSONSerialization.jsonObject(with: Data(MainWindowTests.facts.utf8)) as? [String: Any])
        json["work"] = try JSONSerialization.jsonObject(with: Data(work.utf8))
        if !ownerWaiting {
            json["samples"] = ["waiting_on_you": [], "approved_not_started": []]
            json["computers"] = []
            json["original_backups"] = ["count": 0, "bytes": 0]
        }
        return try JSONDecoder.mediaforce.decode(MainWindowFacts.self, from: JSONSerialization.data(withJSONObject: json))
    }

    private func work(paused: Bool = false, checksPaused: Bool = false, reasons: [String] = [], running: Int = 0, waiting: Int = 0, seasons: [String] = []) -> String {
        let named = seasons.map { prefix in
            let parts = prefix.split(separator: "/").map(String.init)
            let season = parts.count > 2 ? parts[2].split(separator: " ").last.map(String.init) ?? "null" : "null"
            return """
            {"prefix": "\(prefix)", "show_prefix": "\(parts.prefix(2).joined(separator: "/"))", "show": "\(parts[1])",
             "season_folder": \(parts.count > 2 ? "\"\(parts[2])\"" : "null"), "season": \(season)}
            """
        }
        return """
        {"state": "idle", "paused_by_owner": \(paused), "checks_paused": \(checksPaused), "stop_reasons": [\(reasons.map { "\"\($0)\"" }.joined(separator: ","))],
         "running_count": \(running), "running_seasons": [\(named.joined(separator: ","))], "waiting_count": \(waiting), "storage": []}
        """
    }

    func testTheStatusLineSaysWhatMediaforceIsDoing() throws {
        XCTAssertEqual(Words.menuStatus(try facts(work: work(running: 1, seasons: ["tv/Futurama/Season 3"]))), "Compressing Futurama, Season 3")
        XCTAssertEqual(Words.menuStatus(try facts(work: work(running: 3, seasons: ["movies/Heat (1995)", "tv/ER/Season 1"]))),
                       "Compressing Heat (1995) and 1 more")
        XCTAssertEqual(Words.menuStatus(try facts(work: work(), ownerWaiting: true)), "Waiting for you")
        XCTAssertEqual(Words.menuStatus(try facts(work: work())), "Up to date")
        XCTAssertEqual(Words.menuStatus(try facts(work: work(paused: true, running: 1, seasons: ["tv/ER/Season 1"]))), "Paused")
        XCTAssertEqual(Words.menuStatus(try facts(work: work(reasons: ["no_computer_ready"], waiting: 2))), "Waiting for a computer")
    }

    func testPausedChecksChangeOnlyTheIdleStatus() throws {
        let idle = try facts(work: work())
        let pausedChecks = try facts(work: work(checksPaused: true))
        XCTAssertNotEqual(Words.workHeadline(idle), Words.workHeadline(pausedChecks))
        XCTAssertNotEqual(Words.menuStatus(idle), Words.menuStatus(pausedChecks))

        let running = try facts(work: work(checksPaused: true, running: 1, seasons: ["tv/ER/Season 1"]))
        XCTAssertEqual(Words.workHeadline(running), Words.workHeadline(try facts(work: work(running: 1))))
        XCTAssertEqual(Words.menuStatus(running), Words.menuStatus(try facts(work: work(running: 1, seasons: ["tv/ER/Season 1"]))))

        let waitingSamples = try facts(work: work(), ownerWaiting: true)
        let checksWithSamples = try facts(work: work(checksPaused: true), ownerWaiting: true)
        XCTAssertTrue(try XCTUnwrap(Words.workExplanation(checksWithSamples))
            .contains(try XCTUnwrap(Words.workExplanation(waitingSamples))))
    }

    func testPausedChecksDoNotBecomeASampleWaitingReason() throws {
        var json = try XCTUnwrap(JSONSerialization.jsonObject(with: Data(MainWindowTests.facts.utf8)) as? [String: Any])
        let samples = try XCTUnwrap((json["samples"] as? [String: Any])?["waiting_on_you"] as? [[String: Any]])
        var sample = try XCTUnwrap(samples.first)
        sample["state"] = "ready_to_make"
        json["samples"] = ["waiting_on_you": [], "approved_not_started": [sample]]
        json["work"] = try JSONSerialization.jsonObject(with: Data(work(checksPaused: true).utf8))
        let paused = try JSONDecoder.mediaforce.decode(MainWindowFacts.self, from: JSONSerialization.data(withJSONObject: json))
        json["work"] = try JSONSerialization.jsonObject(with: Data(work().utf8))
        let idle = try JSONDecoder.mediaforce.decode(MainWindowFacts.self, from: JSONSerialization.data(withJSONObject: json))

        XCTAssertEqual(Words.waitingReason(paused.samples.approvedNotStarted[0], paused),
                       Words.waitingReason(idle.samples.approvedNotStarted[0], idle))
    }

    func testTheStatusLineOpensWhatItNames() throws {
        XCTAssertEqual(try facts(work: work(running: 1, seasons: ["tv/Futurama/Season 3"])).statusOpens, .item("tv/Futurama/Season 3"))
        XCTAssertEqual(try facts(work: work(reasons: ["no_computer_ready"], waiting: 2)).statusOpens, .computer(nil))
        XCTAssertNil(try facts(work: work(paused: true, running: 1, seasons: ["tv/ER/Season 1"])).statusOpens)
        XCTAssertNil(try facts(work: work()).statusOpens)
        XCTAssertEqual(try facts(work: work(checksPaused: true)).statusOpens, .library)
        XCTAssertEqual(try facts(work: work(checksPaused: true, running: 1, seasons: ["tv/ER/Season 1"])).statusOpens,
                       .item("tv/ER/Season 1"))
        XCTAssertNil(try facts(work: work(checksPaused: true, running: 1)).statusOpens)
    }

    func testAComputerNeedingTheOwnerIsWaitingForYou() throws {
        var facts = try XCTUnwrap(JSONSerialization.jsonObject(with: Data(MainWindowTests.facts.utf8)) as? [String: Any])
        facts["work"] = try JSONSerialization.jsonObject(with: Data(work().utf8))
        facts["samples"] = ["waiting_on_you": [], "approved_not_started": []]
        facts["original_backups"] = ["count": 0, "bytes": 0]
        let decoded = try JSONDecoder.mediaforce.decode(MainWindowFacts.self, from: JSONSerialization.data(withJSONObject: facts))
        XCTAssertEqual(Words.menuStatus(decoded), "Waiting for you")
    }

    func testFactsFromAServerWithoutRunningSeasonsStillDecode() throws {
        let facts = try JSONDecoder.mediaforce.decode(MainWindowFacts.self, from: Data(MainWindowTests.facts.utf8))
        XCTAssertEqual(facts.work.runningSeasons.count, 0)
    }

    func testTheWaitingListOpensEachDecision() throws {
        let facts = try JSONDecoder.mediaforce.decode(MainWindowFacts.self, from: Data(MainWindowTests.facts.utf8))
        XCTAssertEqual(Attention.waitingItems(facts).count, Attention.waitingOnYou(facts))
        XCTAssertEqual(Attention.waitingItems(facts).map(\.opens), [.sample("tv/Lucifer/Season 2"), .computer("mini"), .spaceSaved])
    }
}
