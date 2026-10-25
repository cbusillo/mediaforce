import SwiftUI

/// Where Mediaforce on this Mac stands when the app can't use it, told apart by what the app can see itself.
enum ServerSituation: Equatable {
    case checking
    // This Mac has no Mediaforce login item: a first launch on a new Mac.
    case notSetUp
    // The login item is there, but Mediaforce doesn't answer.
    case notRunning
    // Mediaforce answers, but with an error or a reply the app can't read.
    case inTrouble

    init(_ state: ServerState, serviceInstalled: Bool) {
        switch state {
        case .checking, .reachable: self = .checking
        case .unreachable: self = serviceInstalled ? .notRunning : .notSetUp
        case .failing: self = .inTrouble
        }
    }
}

/// One calm card in the middle of the window: what is happening in one plain sentence, and the one thing to do.
/// A small Details disclosure appears only when there is real trouble, saying what failed and when.
struct ServerStatusView: View {
    let state: ServerState
    var lastHeard: Date?
    var problem: ServerProblem?
    // Read each time the window draws this card, so setting Mediaforce up while it is open shows at the next check.
    var serviceInstalled = BackgroundService.isInstalled
    let retry: () -> Void
    @State private var working = false
    @State private var serviceRefusal: String?
    @State private var confirmingRestart = false
    @State private var showingSetUp = false

    var body: some View {
        let situation = ServerSituation(state, serviceInstalled: serviceInstalled)
        VStack(spacing: 14) {
            Image(systemName: Words.serverSymbol(situation))
                .font(.system(size: 44, weight: .light))
                .foregroundStyle(.secondary)
                .frame(height: 56)
            Text(Words.serverHeadline(situation))
                .font(.system(size: 24, weight: .bold))
                .multilineTextAlignment(.center)
            Text(Words.serverSentence(situation))
                .multilineTextAlignment(.center)
                .foregroundStyle(.secondary)
                .frame(maxWidth: 440)
            if situation != .checking {
                actions(situation)
                    .controlSize(.large)
                    .padding(.top, 6)
            }
            if showingSetUp, situation == .notSetUp {
                SetUpSteps()
            }
            Text(Words.serverLastHeard(situation, lastHeard))
                .font(.callout)
                .foregroundStyle(.tertiary)
                .padding(.top, 4)
            if situation == .inTrouble || serviceRefusal != nil {
                DisclosureGroup(Words.details) {
                    Text(Words.serverDetails(problem: problem, refusal: serviceRefusal))
                        .font(.callout)
                        .foregroundStyle(.secondary)
                        .textSelection(.enabled)
                        .frame(maxWidth: .infinity, alignment: .leading)
                        .padding(.top, 4)
                }
                .frame(maxWidth: 440)
            }
        }
        .padding(40)
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .background(Color(nsColor: .textBackgroundColor))
        .confirmationDialog(Words.restartQuestion, isPresented: $confirmingRestart) {
            Button(Words.restartMediaforce) { Task { await run(BackgroundService.restart) } }
            Button(Words.cancel, role: .cancel) {}
        } message: {
            Text(Words.restartMessage)
        }
    }

    @ViewBuilder
    private func actions(_ situation: ServerSituation) -> some View {
        HStack(spacing: 10) {
            switch situation {
            case .notSetUp:
                Button(Words.howToSetUp) { showingSetUp.toggle() }
                    .buttonStyle(.borderedProminent)
            case .notRunning:
                Button(Words.startMediaforce) { Task { await run(BackgroundService.start) } }
                    .buttonStyle(.borderedProminent)
            case .inTrouble:
                Button(Words.restartMediaforce) { confirmingRestart = true }
                    .buttonStyle(.borderedProminent)
            case .checking:
                EmptyView()
            }
            if situation != .notSetUp {
                Button(Words.checkAgain, action: retry)
            }
            if working { ProgressView().controlSize(.small) }
        }
        .disabled(working)
    }

    /// Starts or restarts the service, then checks again once it has had a moment to come up.
    private func run(_ action: @escaping @Sendable () async -> String?) async {
        working = true
        serviceRefusal = await action()
        if serviceRefusal == nil {
            try? await Task.sleep(for: .seconds(3))
            retry()
        }
        working = false
    }
}

/// How to set Mediaforce up on a Mac, shown under the card when asked for.
struct SetUpSteps: View {
    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            Text(Words.setUpStepsIntro)
            ForEach(Words.setUpCommands, id: \.self) { command in
                Text(command)
                    .font(.body.monospaced())
                    .textSelection(.enabled)
                    .padding(.horizontal, 10)
                    .padding(.vertical, 6)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .background(.quaternary.opacity(0.6), in: RoundedRectangle(cornerRadius: 6, style: .continuous))
            }
            Text(Words.setUpStepsAfter)
                .foregroundStyle(.secondary)
        }
        .font(.callout)
        .frame(maxWidth: 440, alignment: .leading)
        .padding(.top, 4)
    }
}
