import errno
import signal
from unittest.mock import Mock, call, patch

import pytest

from mediaforce.core import _process_deadline as custody
from mediaforce.core._process_deadline import (
    _AuditToken,
    _DarwinProcessIdentity,
    _UniqueProcessInfo,
)
from mediaforce.core.process_control import ManagedProcessController


@pytest.mark.parametrize("first_signal_succeeds", [True, False])
def test_termination_repeats_graceful_signals_then_forces_with_sticky_failure(
        first_signal_succeeds: bool,
) -> None:
    tree = Mock(compromised=False, signal_failure_reason="first signal failed")
    tree.signal_all.side_effect = [first_signal_succeeds, True, True]
    tree.live.side_effect = [True, False]
    reap = Mock()
    term_end = custody._TERM_GRACE_SECONDS
    with patch.object(custody.time, "monotonic", side_effect=[0, 0, term_end, term_end, term_end]):
        result = custody._terminate_tree(tree, reap)

    assert result.succeeded is first_signal_succeeds
    assert result.reason == (None if first_signal_succeeds else "first signal failed")
    assert tree.signal_all.call_args_list == [
        call(signal.SIGTERM), call(signal.SIGTERM), call(signal.SIGKILL),
    ]
    assert tree.refresh.call_count == 2
    assert reap.call_count == 3


def test_termination_does_not_force_an_empty_but_compromised_tree() -> None:
    tree = Mock(compromised=True, signal_failure_reason=None)
    tree.signal_all.return_value = True
    tree.live.return_value = False
    with patch.object(custody.time, "monotonic", return_value=0):
        result = custody._terminate_tree(tree, Mock())

    assert not result.succeeded
    assert result.reason == "managed process ownership was compromised"
    tree.signal_all.assert_called_once_with(signal.SIGTERM)


def test_termination_reports_survivors_after_both_grace_periods() -> None:
    tree = Mock(compromised=False, signal_failure_reason="identity unavailable")
    tree.signal_all.return_value = True
    tree.live.return_value = True
    term_end = custody._TERM_GRACE_SECONDS
    kill_end = term_end + custody._KILL_GRACE_SECONDS
    with patch.object(custody.time, "monotonic", side_effect=[0, term_end, term_end, kill_end]):
        result = custody._terminate_tree(tree, Mock())

    assert not result.succeeded
    assert result.reason == "managed process tree remained live after SIGKILL grace: identity unavailable"
    assert tree.signal_all.call_args_list == [call(signal.SIGTERM), call(signal.SIGKILL)]


def _darwin_identity() -> _DarwinProcessIdentity:
    info = _UniqueProcessInfo()
    info.unique_id = 789
    return _DarwinProcessIdentity(
        serial=1, pid=12345, parent_serial=None, token=_AuditToken(), unique_info=info,
    )


@pytest.mark.parametrize("retry_state", list(custody._DarwinSignalState))
def test_darwin_esrch_retry_refreshes_identity_before_signalling(
        retry_state: custody._DarwinSignalState,
) -> None:
    tree = custody._DarwinProcessTree.__new__(custody._DarwinProcessTree)
    tree._libc = Mock()
    native_signal = tree._libc.proc_signal_with_audittoken
    native_signal.side_effect = [errno.ESRCH, 0]
    identity = _darwin_identity()
    states = [custody._DarwinSignalState.SIGNALABLE, retry_state]
    with (
        patch.object(tree, "_refresh_signal_token", side_effect=states) as refresh,
        patch.object(tree, "_mark_exited") as mark_exited,
        patch.object(tree, "_same_identity_alive") as alive,
    ):
        succeeded = tree._signal_identity(identity, signal.SIGTERM, 10.0)

    assert succeeded is (retry_state is not custody._DarwinSignalState.UNSIGNALABLE)
    assert refresh.call_args_list == [call(identity, retry_deadline=10.0)] * 2
    assert native_signal.call_count == (2 if retry_state is custody._DarwinSignalState.SIGNALABLE else 1)
    assert mark_exited.called is (retry_state is custody._DarwinSignalState.EXITED)
    alive.assert_not_called()


@pytest.mark.parametrize("same_identity_alive", [True, False])
def test_darwin_repeated_esrch_checks_native_identity_without_bare_pid_signal(
        same_identity_alive: bool,
) -> None:
    tree = custody._DarwinProcessTree.__new__(custody._DarwinProcessTree)
    tree._libc = Mock()
    tree._libc.proc_signal_with_audittoken.return_value = errno.ESRCH
    tree._signal_failure_reason = None
    identity = _darwin_identity()
    with (
        patch.object(tree, "_refresh_signal_token", return_value=custody._DarwinSignalState.SIGNALABLE),
        patch.object(tree, "_same_identity_alive", return_value=same_identity_alive),
        patch.object(tree, "_mark_exited") as mark_exited,
        patch.object(custody.os, "kill") as kill,
    ):
        succeeded = tree._signal_identity(identity, signal.SIGKILL, 10.0)

    assert succeeded is not same_identity_alive
    assert mark_exited.called is not same_identity_alive
    assert (tree.signal_failure_reason is not None) is same_identity_alive
    assert tree._libc.proc_signal_with_audittoken.call_count == 2
    kill.assert_not_called()


@pytest.mark.parametrize("failure", [RuntimeError("cleanup interrupted"), KeyboardInterrupt()])
def test_parent_loss_keeps_custody_after_cleanup_raises(failure: BaseException) -> None:
    tree = Mock()
    tree.live.return_value = False
    with (
        patch.object(custody, "_terminate_tree", side_effect=[failure, custody._TerminationResult(True)]) as terminate,
        patch.object(custody.time, "sleep") as sleep,
    ):
        custody._terminate_tree_after_parent_loss(tree, Mock())

    assert terminate.call_count == 2
    sleep.assert_called_once_with(custody._TREE_POLL_SECONDS)
    tree.live.assert_called_once_with()


def test_nested_activity_guards_preserve_order_and_restore_outer_guard() -> None:
    controller = ManagedProcessController()
    calls: list[str] = []

    def outer_guard() -> None:
        calls.append("outer")

    def inner_guard() -> None:
        calls.append("inner")

    with controller.activity_guard(outer_guard):
        with controller.activity_guard(inner_guard):
            controller.throw_if_cancelled()
        controller.throw_if_cancelled()
    controller.throw_if_cancelled()
    assert calls == ["outer", "inner", "outer"]
