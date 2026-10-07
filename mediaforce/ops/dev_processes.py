"""Stop a development tree using the media runner's native identity custody."""

import subprocess
import sys

from mediaforce.core.process_control import stop_existing_process_tree


def main() -> int:
    try:
        pid = int(sys.argv[1])
        script, component = sys.argv[2:4]
        if component not in {"backend", "frontend"} or len(sys.argv) != 4:
            raise ValueError("invalid development process stop arguments")

        def owns_root() -> bool:
            result = subprocess.run(
                ["/bin/bash", script, "check-owner", component, str(pid)],
                check=False, stdout=subprocess.DEVNULL,
            )
            if result.returncode not in {0, 1}:
                raise RuntimeError("development process ownership unknown; preserving it")
            return result.returncode == 0

        stop_existing_process_tree(pid, owns_root)
        return 0
    except (OSError, RuntimeError, ValueError, IndexError) as exc:
        print(f"development stop: {exc}; PID bookkeeping retained", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
