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
            let host = NSHostingView(rootView: PlayerSurface(player: player))
            host.frame = NSRect(x: 0, y: 0, width: 320, height: 180)
            let window = NSWindow(contentRect: host.frame, styleMask: [.borderless], backing: .buffered, defer: false)
            window.contentView = host
            host.layoutSubtreeIfNeeded()
            let surface = try XCTUnwrap(picture(in: host))
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
        try await wait { !players.preparing }
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
            players.main.currentTime() == players.original.currentTime()
        }
        XCTAssertFalse(players.playing)
    }

    private func wait(_ condition: () -> Bool) async throws {
        for _ in 0..<250 {
            if condition() { return }
            try await Task.sleep(for: .milliseconds(20))
        }
        XCTFail("The local players did not reach the requested moment")
    }

    private func clip() async throws -> URL {
        let url = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString).appendingPathExtension("mov")
        let writer = try AVAssetWriter(outputURL: url, fileType: .mov)
        let input = AVAssetWriterInput(mediaType: .video, outputSettings: [
            AVVideoCodecKey: AVVideoCodecType.h264, AVVideoWidthKey: 16, AVVideoHeightKey: 16,
        ])
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
