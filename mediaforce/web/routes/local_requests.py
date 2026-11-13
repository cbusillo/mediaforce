from urllib.parse import urlsplit

from fastapi import Request
from fastapi.responses import JSONResponse

LOOPBACK_NAMES = frozenset({"127.0.0.1", "localhost", "::1"})
DEFAULT_HTTP_PORT = 80


def refusal_unless_this_mac(request: Request) -> JSONResponse | None:
    """Refuses a request a web page could have sent, so no page, even one behind a rebound DNS name, can set a key,
    move where a key is sent or make the server send one.

    The Mac app names this server by its loopback address and sends no Origin; a browser names the page's host and
    sends the page's origin on every request that writes.
    """
    server = request.scope.get("server")
    bound_port = server[1] if isinstance(server, tuple) else None
    origin = request.headers.get("origin")
    if _names_this_server(f"//{request.headers.get('host', '')}", bound_port, scheme="") and (
        origin is None or _names_this_server(origin, bound_port, scheme="http")
    ):
        return None
    return JSONResponse({"ok": False, "code": "not_this_mac"}, status_code=403)


def _names_this_server(address: str, bound_port: int | None, *, scheme: str) -> bool:
    try:
        parts = urlsplit(address)
        port = parts.port or DEFAULT_HTTP_PORT
    except ValueError:
        return False
    return parts.scheme == scheme and parts.hostname in LOOPBACK_NAMES and port == bound_port
