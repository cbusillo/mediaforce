"""Attribute development launchers using native argument boundaries, not ps text."""

import ctypes
import os
from pathlib import Path
import re
import struct
import sys


def decode_darwin_arguments(data: bytes) -> list[str]:
    if len(data) < 4:
        raise ValueError("truncated process argument count")
    count = struct.unpack_from("=i", data)[0]
    if count <= 0 or count > len(data):
        raise ValueError("invalid process argument count")
    # KERN_PROCARGS2: argc, executable path, padding, then argc NUL strings.
    offset = data.index(b"\0", 4) + 1
    while offset < len(data) and data[offset] == 0:
        offset += 1
    arguments = []
    for _ in range(count):
        end = data.index(b"\0", offset)
        arguments.append(os.fsdecode(data[offset:end]))
        offset = end + 1
    return arguments


def darwin_arguments(pid: int) -> list[str]:
    libc = ctypes.CDLL("/usr/lib/libSystem.B.dylib", use_errno=True)
    libc.sysctlbyname.argtypes = [ctypes.c_char_p, ctypes.c_void_p,
                                 ctypes.POINTER(ctypes.c_size_t), ctypes.c_void_p, ctypes.c_size_t]
    libc.sysctlbyname.restype = ctypes.c_int
    libc.sysctl.argtypes = [ctypes.POINTER(ctypes.c_int), ctypes.c_uint, ctypes.c_void_p,
                           ctypes.POINTER(ctypes.c_size_t), ctypes.c_void_p, ctypes.c_size_t]
    libc.sysctl.restype = ctypes.c_int
    capacity = ctypes.c_int()
    size = ctypes.c_size_t(ctypes.sizeof(capacity))
    if libc.sysctlbyname(b"kern.argmax", ctypes.byref(capacity), ctypes.byref(size), None, 0):
        raise OSError(ctypes.get_errno(), "cannot read process argument capacity")
    if not 0 < capacity.value <= 16 * 1024 * 1024:
        raise ValueError("invalid process argument capacity")
    buffer = ctypes.create_string_buffer(capacity.value)
    size = ctypes.c_size_t(capacity.value)
    # CTL_KERN=1, KERN_PROCARGS2=49 (Darwin sys/sysctl.h).
    mib = (ctypes.c_int * 3)(1, 49, pid)
    if libc.sysctl(mib, 3, buffer, ctypes.byref(size), None, 0):
        raise OSError(ctypes.get_errno(), "cannot read process arguments")
    if size.value > capacity.value:
        raise ValueError("truncated process arguments")
    return decode_darwin_arguments(buffer.raw[:size.value])


def process_arguments(pid: int) -> list[str]:
    if sys.platform == "darwin":
        return darwin_arguments(pid)
    if sys.platform == "linux":
        data = Path(f"/proc/{pid}/cmdline").read_bytes()
        if not data:
            return []
        if not data.endswith(b"\0"):
            raise ValueError("truncated process arguments")
        return [os.fsdecode(part) for part in data[:-1].split(b"\0")]
    raise RuntimeError("native process arguments unavailable on this platform")


def matches_frontend(arguments: list[str], checkout: str, cwd: str) -> bool:
    if not arguments:
        return False
    frontend = checkout + "/frontend"
    # npm rewrites its title in argv storage. Only its known title and exact
    # frontend cwd qualify; never split arbitrary flattened interpreter text.
    if arguments[0] == "npm run dev" or arguments[0].startswith("npm run dev "):
        return cwd == frontend and not any(arguments[1:])
    executable = Path(arguments[0]).name
    script_index = 0
    if re.fullmatch(r"[Nn]ode|[Pp]ython([0-9]+([.][0-9]+)*t?)?", executable):
        script_index = 1
        if executable.lower() == "node":
            while script_index < len(arguments):
                option = arguments[script_index]
                if option == "--":
                    script_index += 1
                    break
                if option in {"--inspect", "--inspect-brk"} or option.startswith(("--inspect=", "--inspect-brk=")):
                    script_index += 1
                elif option.startswith("--max-old-space-size="):
                    if not option.partition("=")[2].isdigit():
                        return False
                    script_index += 1
                elif option == "--max-old-space-size" and script_index + 1 < len(arguments):
                    if not arguments[script_index + 1].isdigit():
                        return False
                    script_index += 2
                else:
                    break
    if script_index >= len(arguments):
        return False
    script = arguments[script_index]
    tail = arguments[script_index + 1:]
    if script in {"vite", frontend + "/node_modules/.bin/vite"}:
        return cwd == frontend
    if Path(script).name not in {"npm", "npm-cli.js"}:
        return False
    if tail[:2] == ["run", "dev"]:
        return cwd == frontend
    if len(tail) >= 4 and tail[0] == "--prefix" and tail[2:4] == ["run", "dev"]:
        if tail[1] == "frontend":
            return cwd == checkout
        return tail[1] == frontend and cwd in {checkout, frontend}
    return False


def main() -> int:
    pid = None
    try:
        candidate = int(sys.argv[1])
        checkout, cwd = sys.argv[2:4]
        if len(sys.argv) != 4 or candidate <= 1:
            raise ValueError("invalid frontend ownership arguments")
        pid = candidate
        return 0 if matches_frontend(process_arguments(pid), checkout, cwd) else 1
    except (FileNotFoundError, ProcessLookupError):
        return 1
    except (OSError, RuntimeError, ValueError, IndexError) as exc:
        # Darwin may return EINVAL after exit. Only an independent absence
        # check makes a failed argument read non-ownership; EINVAL alone cannot.
        if pid is not None:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                return 1
            except OSError:
                pass
        print(f"frontend: native argument ownership unknown: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
