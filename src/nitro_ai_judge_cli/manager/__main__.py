"""Manager image entrypoint."""

from __future__ import annotations

import os
import ssl

from aiohttp import web

from .app import create_app


def main() -> None:
    bind = os.environ.get("NAIJ_MANAGER_BIND", "0.0.0.0")
    port = int(os.environ.get("NAIJ_MANAGER_PORT", "51123"))
    cert = os.environ.get("NAIJ_MANAGER_TLS_CERT")
    key = os.environ.get("NAIJ_MANAGER_TLS_KEY")
    context = None
    if cert and key:
        context = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
        context.load_cert_chain(cert, key)
    web.run_app(create_app(), host=bind, port=port, ssl_context=context, access_log=None)


if __name__ == "__main__":
    main()
