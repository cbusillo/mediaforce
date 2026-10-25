import Foundation
import XCTest
@testable import Mediaforce

final class ServerStatusTests: XCTestCase {
    func testEachSituationIsToldApartByWhatTheAppCanSee() {
        XCTAssertEqual(ServerSituation(.unreachable, serviceInstalled: false), .notSetUp)
        XCTAssertEqual(ServerSituation(.unreachable, serviceInstalled: true), .notRunning)
        XCTAssertEqual(ServerSituation(.failing, serviceInstalled: true), .inTrouble)
        XCTAssertEqual(ServerSituation(.checking, serviceInstalled: false), .checking)
    }

    func testNotSetUpIsSaidPlainly() {
        XCTAssertEqual(Words.serverHeadline(.notSetUp), "Mediaforce isn't set up on this Mac")
        XCTAssertEqual(Words.serverLastHeard(.notSetUp, .now), "This Mac has never been set up to run Mediaforce.")
        let heard = Date.now
        XCTAssertEqual(Words.serverLastHeard(.notRunning, heard),
                       "Last heard from Mediaforce today at \(heard.formatted(date: .omitted, time: .shortened)). Checking again every 30 seconds.")
    }

    func testDetailsSayWhatFailedAndWhen() {
        let at = Date(timeIntervalSince1970: 1_792_960_000)
        let details = Words.serverDetails(problem: ServerProblem(kind: .errorReply, at: at), refusal: "Bootstrap failed: 5")
        XCTAssertTrue(details.hasPrefix("Mediaforce answered with an error at \(at.formatted(date: .omitted, time: .standard))."))
        XCTAssertTrue(details.hasSuffix("macOS refused to start it: Bootstrap failed: 5"))
        XCTAssertEqual(Words.serverDetails(problem: nil, refusal: nil), "Nothing more is known yet.")
    }
}
