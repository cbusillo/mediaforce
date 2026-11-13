import AVKit
import SwiftUI
import XCTest
@testable import Mediaforce

@MainActor
final class ComparePlayerTests: XCTestCase {
    func testScrollingEitherPictureReachesThePageWithoutSeeking() async throws {
        let url = try await clip()
        defer { try? FileManager.default.removeItem(at: url) }
        let players = ComparePlayers()
        players.load(compressed: url, original: url)
        try await wait { !players.preparing && abs(players.main.currentTime().seconds - players.position) < 0.01 }
        XCTAssertFalse(players.failed)
        for player in [players.original, players.main] {
            let host = NSHostingView(rootView: PlayerSurface(players: players, player: player))
            host.frame = NSRect(x: 0, y: 0, width: 320, height: 180)
            let window = NSWindow(contentRect: host.frame, styleMask: [.borderless], backing: .buffered, defer: false)
            window.isReleasedWhenClosed = false
            defer { window.close() }
            window.contentView = host
            host.layoutSubtreeIfNeeded()
            let surface = try XCTUnwrap(picture(in: host))
            window.setContentSize(NSSize(width: 480, height: 270))
            host.layoutSubtreeIfNeeded()
            XCTAssertEqual(surface.layer?.bounds.size, surface.bounds.size)
            XCTAssertTrue(surface.layer?.animationKeys()?.isEmpty ?? true)
            var pixel = [UInt8](repeating: 0, count: 4)
            try pixel.withUnsafeMutableBytes { bytes in
                let context = try XCTUnwrap(CGContext(data: bytes.baseAddress, width: 1, height: 1,
                                                      bitsPerComponent: 8, bytesPerRow: 4,
                                                      space: CGColorSpaceCreateDeviceRGB(),
                                                      bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue))
                surface.layer?.render(in: context)
            }
            XCTAssertEqual(pixel, [0, 0, 0, 255], "Empty picture areas must cover the other side of a wipe")
            let page = ScrollReceiver()
            surface.nextResponder = page
            let target = surface.hitTest(NSPoint(x: surface.bounds.midX, y: surface.bounds.midY)) ?? surface
            let before = player.currentTime()
            for delta in [-12, 12] {
                let event = try XCTUnwrap(CGEvent(scrollWheelEvent2Source: nil, units: .pixel,
                                                  wheelCount: 2, wheel1: Int32(delta), wheel2: Int32(delta), wheel3: 0))
                event.setIntegerValueField(.scrollWheelEventIsContinuous, value: 1)
                event.setIntegerValueField(.scrollWheelEventScrollPhase, value: 2)
                target.scrollWheel(with: try XCTUnwrap(NSEvent(cgEvent: event)))
            }
            try await Task.sleep(for: .milliseconds(100))
            XCTAssertEqual(page.scrolls, 2)
            XCTAssertEqual(player.currentTime(), before)
            XCTAssertFalse(players.playing)
        }
    }

    func testSharedSeekAndFrameStepMoveBothPicturesTogether() async throws {
        let url = try await clip()
        defer { try? FileManager.default.removeItem(at: url) }
        let players = ComparePlayers()
        players.load(compressed: url, original: url)
        try await wait {
            !players.preparing && abs(players.main.currentTime().seconds - players.position) < 0.001 &&
            abs(players.original.currentTime().seconds - players.position) < 0.001
        }
        XCTAssertFalse(players.failed)
        for position in [1.0, 3.0, 2.0] {
            players.seek(to: position)
            try await wait {
                abs(players.main.currentTime().seconds - position) < 0.01 &&
                abs(players.original.currentTime().seconds - position) < 0.01
            }
        }
        players.step(by: 1)
        try await wait {
            players.main.currentTime().seconds > 2 &&
            abs(players.main.currentTime().seconds - players.original.currentTime().seconds) < 0.001
        }
        XCTAssertFalse(players.playing)
    }

    func testNeitherPictureShowsUntilBothSidesAreOnTheOpeningMoment() async throws {
        let url = try await clip()
        defer { try? FileManager.default.removeItem(at: url) }
        let players = ComparePlayers()
        let opening = 5 * players.opensAt
        players.load(compressed: url, original: url)
        var shown: [(Double, Double)] = []
        try await wait(every: .milliseconds(1)) {
            if players.pictureShown { shown.append((players.main.currentTime().seconds, players.original.currentTime().seconds)) }
            return !players.preparing
        }
        XCTAssertFalse(players.failed)
        XCTAssertTrue(players.pictureShown)
        XCTAssertFalse(shown.isEmpty)
        for (compressed, original) in shown {
            XCTAssertEqual(compressed, opening, accuracy: 0.001)
            XCTAssertEqual(original, opening, accuracy: 0.001)
        }
    }

    func testAJumpHoldsBothPicturesUntilBothSidesAreOnTheNewFrame() async throws {
        let url = try await clip()
        defer { try? FileManager.default.removeItem(at: url) }
        let players = ComparePlayers()
        let pictures = [PictureView(player: players.original), PictureView(player: players.main)]
        pictures.forEach(players.join)
        players.load(compressed: url, original: url)
        try await wait { !players.preparing }
        for target in [3.0, 1.5] {
            players.seek(to: target)
            XCTAssertFalse(players.pictureShown)
            XCTAssertTrue(pictures.allSatisfy(\.holding))
            var shownOff = 0
            try await wait(every: .milliseconds(1)) {
                guard players.pictureShown else { return false }
                if abs(players.main.currentTime().seconds - target) > 0.001 || abs(players.original.currentTime().seconds - target) > 0.001 {
                    shownOff += 1
                }
                return true
            }
            XCTAssertEqual(shownOff, 0, "The pictures showed again before both sides were on \(target)")
            XCTAssertFalse(pictures.contains(where: \.holding))
        }
    }

    func testDraggingKeepsBothSidesOnOneFrameAndLettingGoLandsOnTheExactFrame() async throws {
        let url = try await clip(keyframeEvery: 24)
        defer { try? FileManager.default.removeItem(at: url) }
        let players = ComparePlayers()
        players.load(compressed: url, original: url)
        try await wait { !players.preparing }
        var apart = 0
        for place in [1.5, 2.5, 3.5] {
            players.drag(to: place)
            try await Task.sleep(for: .milliseconds(5))
            if players.pictureShown, players.main.currentTime() != players.original.currentTime() { apart += 1 }
        }
        try await wait(every: .milliseconds(1)) {
            if players.pictureShown, players.main.currentTime() != players.original.currentTime() { apart += 1 }
            return players.pictureShown && abs(players.main.currentTime().seconds - 3.5) <= 0.5
        }
        XCTAssertEqual(apart, 0)
        players.endDrag()
        try await wait {
            players.pictureShown && abs(players.main.currentTime().seconds - 3.5) < 0.001 &&
            abs(players.original.currentTime().seconds - 3.5) < 0.001
        }
        XCTAssertEqual(players.position, 3.5)
    }

    func testADraggedScrubberSeeksOnlyTheNewestPlaceOnceTheSeekOnItsWayLands() {
        var chase = Chase()
        XCTAssertEqual(chase.want(1), 1)
        XCTAssertNil(chase.want(2))
        XCTAssertNil(chase.want(3))
        XCTAssertEqual(chase.landed(), 3)
        XCTAssertNil(chase.landed())
        XCTAssertEqual(chase.want(4), 4)
    }

    func testPanningEitherSideAtActualSizeMovesBothInTheSameStep() throws {
        let pan = Pan()
        let picture = CGSize(width: 800, height: 600)
        let host = NSHostingView(rootView: HStack(spacing: 8) {
            PanningPicture(size: picture, pan: pan, content: Color.red)
            PanningPicture(size: picture, pan: pan, content: Color.blue)
        })
        host.frame = NSRect(x: 0, y: 0, width: 408, height: 150)
        let window = NSWindow(contentRect: host.frame, styleMask: [.borderless], backing: .buffered, defer: false)
        window.isReleasedWhenClosed = false
        defer { window.close() }
        window.contentView = host
        host.layoutSubtreeIfNeeded()
        let sides = panningViews(in: host)
        XCTAssertEqual(sides.count, 2)
        for side in sides {
            XCTAssertEqual(side.contentView.bounds.origin, NSPoint(x: 300, y: 225), "Both sides open on the middle of the picture")
        }

        for (moved, other) in [(sides[0], sides[1]), (sides[1], sides[0])] {
            let before = moved.contentView.bounds.origin
            let event = try XCTUnwrap(CGEvent(scrollWheelEvent2Source: nil, units: .pixel,
                                              wheelCount: 2, wheel1: -30, wheel2: -40, wheel3: 0))
            event.setIntegerValueField(.scrollWheelEventIsContinuous, value: 1)
            event.setIntegerValueField(.scrollWheelEventScrollPhase, value: 2)
            moved.scrollWheel(with: try XCTUnwrap(NSEvent(cgEvent: event)))
            let deadline = Date.now.addingTimeInterval(2)
            while moved.contentView.bounds.origin == before, Date.now < deadline {
                RunLoop.current.run(until: .now.addingTimeInterval(0.001))
            }
            XCTAssertNotEqual(moved.contentView.bounds.origin, before)
            // The moment one side has moved, the other is already there.
            XCTAssertEqual(other.contentView.bounds.origin, moved.contentView.bounds.origin)
        }
    }

    private func panningViews(in view: NSView) -> [PanningScrollView] {
        (view as? PanningScrollView).map { [$0] } ?? view.subviews.flatMap { panningViews(in: $0) }
    }

    private func wait(every interval: Duration = .milliseconds(20), file: StaticString = #filePath, line: UInt = #line,
                      _ condition: () -> Bool) async throws {
        let deadline = ContinuousClock.now + .seconds(5)
        while ContinuousClock.now < deadline {
            if condition() { return }
            try await Task.sleep(for: interval)
        }
        XCTFail("The local players did not reach the requested moment", file: file, line: line)
    }

    private func clip(keyframeEvery keyframes: Int? = nil) async throws -> URL {
        let url = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString).appendingPathExtension("mov")
        let writer = try AVAssetWriter(outputURL: url, fileType: .mov)
        var settings: [String: Any] = [AVVideoCodecKey: AVVideoCodecType.h264, AVVideoWidthKey: 16, AVVideoHeightKey: 16]
        if let keyframes { settings[AVVideoCompressionPropertiesKey] = [AVVideoMaxKeyFrameIntervalKey: keyframes] }
        let input = AVAssetWriterInput(mediaType: .video, outputSettings: settings)
        let adaptor = AVAssetWriterInputPixelBufferAdaptor(assetWriterInput: input, sourcePixelBufferAttributes: [
            kCVPixelBufferPixelFormatTypeKey as String: kCVPixelFormatType_32ARGB,
            kCVPixelBufferWidthKey as String: 16, kCVPixelBufferHeightKey as String: 16,
        ])
        writer.add(input)
        XCTAssertTrue(writer.startWriting())
        writer.startSession(atSourceTime: .zero)
        var buffer: CVPixelBuffer?
        XCTAssertEqual(CVPixelBufferCreate(nil, 16, 16, kCVPixelFormatType_32ARGB, nil, &buffer), kCVReturnSuccess)
        let pixels = try XCTUnwrap(buffer)
        CVPixelBufferLockBaseAddress(pixels, [])
        memset(CVPixelBufferGetBaseAddress(pixels), 0, CVPixelBufferGetDataSize(pixels))
        CVPixelBufferUnlockBaseAddress(pixels, [])
        for frame in 0..<120 {
            try await wait { input.isReadyForMoreMediaData }
            XCTAssertTrue(adaptor.append(pixels, withPresentationTime: CMTime(value: Int64(frame), timescale: 24)))
        }
        input.markAsFinished()
        await writer.finishWriting()
        XCTAssertEqual(writer.status, .completed)
        return url
    }

    private func picture(in view: NSView) -> NSView? {
        if view is AVPlayerView || view.layer is AVPlayerLayer { return view }
        return view.subviews.compactMap { picture(in: $0) }.first
    }

    private final class ScrollReceiver: NSResponder {
        var scrolls = 0
        override func scrollWheel(with event: NSEvent) { scrolls += 1 }
    }
}
