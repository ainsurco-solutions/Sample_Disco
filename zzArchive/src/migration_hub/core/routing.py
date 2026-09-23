from __future__ import annotations

import re

class UnroutableSourceError(Exception):

    def __init__(self, *, source_path: str, server: str | None, known: list[str]) -> None:
        self.source_path = source_path
        self.server = server
        self.known = known
        where = f"server {server!r}" if server else "no recognisable server"
        listed = ", ".join(known) if known else "(nothing)"
        super().__init__(
            f'cannot route "{source_path}": {where}, and the configured '
            f"mapping covers {listed}. Add it to databridge_instances, "
            "or check the path."
        )

_UNC_SERVER = re.compile(r"^[\\/]{2}([^\\/]+)")

_DRIVE_LETTER = re.compile(r"^[A-Za-z]:")

def source_server(source_path: str) -> str | None:
    if _DRIVE_LETTER.match(source_path):
        return None
    match = _UNC_SERVER.match(source_path)
    return match.group(1).lower() if match else None

def resolve_instance(
    *, source_path: str, mapping: dict[str, str] | None
) -> str:
    known = sorted(mapping or {})
    server = source_server(source_path)
    if server is None:
        raise UnroutableSourceError(source_path=source_path, server=None, known=known)

    folded = {k.lower(): v for k, v in (mapping or {}).items()}
    instance = folded.get(server)
    if instance is None:
        raise UnroutableSourceError(source_path=source_path, server=server, known=known)
    return instance
