"""Development cleanup custody, also loadable without importing the application."""

from collections.abc import Callable
import importlib.util
import os
from pathlib import Path
import sys

if __package__:
    from . import _process_deadline
else:
    # Unfinished application edits must not prevent stopping an existing tree.
    spec = importlib.util.spec_from_file_location(
        "_mediaforce_dev_deadline", Path(__file__).with_name("_process_deadline.py"),
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load native development custody")
    _process_deadline = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = _process_deadline
    spec.loader.exec_module(_process_deadline)


class DevelopmentCustodyLostError(RuntimeError):
    pass


class DevelopmentProcessTree:
    def __init__(self, pid: int, owns_root: Callable[[], bool]) -> None:
        if pid <= 1 or pid == os.getpid():
            raise ValueError("invalid development process root")
        self._tree = _process_deadline._process_tree(external_root=True)
        try:
            self._tree.add_root(pid)
            if not owns_root():
                raise RuntimeError("development process ownership changed; preserving it")
            self._tree.refresh()
        except BaseException:
            self._tree.close()
            raise

    def stop(self) -> None:
        result = _process_deadline._terminate_tree(self._tree, lambda: None)
        if not result.succeeded:
            raise RuntimeError(result.reason or "development process cleanup is unproven")

    def finished(self) -> bool:
        self._tree.refresh()
        if self._tree.compromised and not self._tree.live():
            raise DevelopmentCustodyLostError(
                self._tree.ownership_failure_reason or "development process ownership was compromised"
            )
        return not self._tree.live() and not self._tree.compromised

    def close(self) -> None:
        self._tree.close()


def stop_existing_process_tree(pid: int, owns_root: Callable[[], bool]) -> None:
    tree = DevelopmentProcessTree(pid, owns_root)
    try:
        tree.stop()
    finally:
        tree.close()
