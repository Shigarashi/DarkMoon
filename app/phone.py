"""Phone access: an optional second listener on your local network.

Off by default. When on, Darkmoon also listens on 0.0.0.0:<phone.port> so a
phone on the same Wi-Fi can use it. Every request from another device must
carry the pairing secret: scanning the QR code opens /?pair=<secret>, which
sets a cookie; without it every page and API call is refused. "Reset pairing"
issues a new secret and logs out every paired phone.
"""
from __future__ import annotations

import hmac
import logging
import secrets
import socket
import threading
import time
from typing import Any

from . import paths

log = logging.getLogger("darkmoon.phone")
PAIR_FILE = paths.DATA_DIR / "phone_pairing.txt"
COOKIE = "dm_pair"


def lan_ips() -> list[str]:
    ips: list[str] = []
    try:  # the "route to the internet" interface — no packets are sent
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("10.255.255.255", 1))
            ips.append(s.getsockname()[0])
    except OSError:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if ip not in ips and not ip.startswith("127."):
                ips.append(ip)
    except OSError:
        pass
    return ips or ["127.0.0.1"]


class PhoneAccess:
    def __init__(self, cfg):
        self.cfg = cfg
        self.app = None
        self.server = None
        self.thread: threading.Thread | None = None
        self.error = ""
        self._secret = self._load_secret()

    # Pairing ---------------------------------------------------------------
    def _load_secret(self) -> str:
        try:
            s = PAIR_FILE.read_text(encoding="utf-8").strip()
            if len(s) >= 20:
                return s
        except OSError:
            pass
        return self.reset_pairing()

    def reset_pairing(self) -> str:
        self._secret = secrets.token_urlsafe(18)
        try:
            PAIR_FILE.parent.mkdir(parents=True, exist_ok=True)
            PAIR_FILE.write_text(self._secret, encoding="utf-8")
        except OSError as e:
            log.warning("could not save pairing secret: %s", e)
        return self._secret

    def check(self, value: str | None) -> bool:
        return bool(value) and hmac.compare_digest(str(value), self._secret)

    @property
    def port(self) -> int:
        return int(self.cfg["phone"].get("port", 8766))

    def urls(self) -> list[str]:
        return [f"http://{ip}:{self.port}/pair?code={self._secret}" for ip in lan_ips()]

    # Listener ----------------------------------------------------------------
    @property
    def running(self) -> bool:
        return bool(self.server and self.thread and self.thread.is_alive() and self.server.started)

    def start(self) -> None:
        if self.running or self.app is None:
            return
        import uvicorn
        self.error = ""
        self.server = uvicorn.Server(uvicorn.Config(self.app, host="0.0.0.0", port=self.port, log_level="warning",
                                                    log_config=None, timeout_graceful_shutdown=1, lifespan="off"))

        def run():
            try:
                self.server.run()
            except SystemExit:
                self.error = f"could not listen on port {self.port} (in use?)"
            except Exception as e:  # noqa: BLE001
                self.error = str(e)

        self.thread = threading.Thread(target=run, daemon=True)
        self.thread.start()
        for _ in range(60):
            if self.server.started or not self.thread.is_alive():
                break
            time.sleep(0.05)
        if not self.server.started and not self.error:
            self.error = f"could not listen on port {self.port} (in use?)"
        log.info("phone access %s", "on" if self.running else f"failed: {self.error}")

    def stop(self) -> None:
        if self.server:
            self.server.should_exit = True
        if self.thread:
            self.thread.join(timeout=3)
        self.server, self.thread = None, None

    def status(self) -> dict[str, Any]:
        return {"enabled": bool(self.cfg["phone"].get("enabled")), "running": self.running, "port": self.port,
                "urls": self.urls() if self.running else [], "error": self.error}
