from collections import defaultdict

import pytest


def pytest_terminal_summary(terminalreporter: pytest.TerminalReporter) -> None:
    durations: dict[str, float] = defaultdict(float)
    for reports in terminalreporter.stats.values():
        for report in reports:
            if type(report) is pytest.TestReport:
                durations[report.nodeid.split("::", 1)[0]] += report.duration
    terminalreporter.section("test time by file (setup, call and teardown)")
    for path, duration in sorted(durations.items(), key=lambda entry: entry[1], reverse=True):
        terminalreporter.write_line(f"{duration:.3f}s {path}")
