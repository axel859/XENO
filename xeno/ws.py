"""Minimaler WebSocket-Client (RFC 6455).

Python bringt keinen mit, und eine Fremdbibliothek wollte ich mir nicht
einhandeln - die Abhaengigkeitsfreiheit hat sich bisher ausgezahlt: kein
Installationsschritt, und beim Sprung auf Python 3.14 lief alles sofort.

Umgesetzt ist genau der Teil des Standards, den ein Client braucht:
Handshake mit Pruefung der Server-Antwort, Rahmen lesen und schreiben,
Maskierung ausgehender Daten (fuer Clients vorgeschrieben), zusammengesetzte
Nachrichten und automatische Antwort auf Ping.

Nicht umgesetzt: Kompression und Erweiterungen - beides wird beim Handshake
gar nicht erst angeboten.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import socket
import ssl
import struct
from typing import Any
from urllib.parse import urlparse

#: Feste Kennung aus dem Standard, mit der der Server seine Antwort bildet.
_ACCEPT_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

OP_CONTINUATION = 0x0
OP_TEXT = 0x1
OP_BINARY = 0x2
OP_CLOSE = 0x8
OP_PING = 0x9
OP_PONG = 0xA


class WebSocketError(RuntimeError):
    """Verbindung fehlgeschlagen oder Gegenstelle hat geschlossen."""


def build_frame(payload: bytes, opcode: int = OP_TEXT, mask: bytes | None = None) -> bytes:
    """Baut einen Rahmen. Clients muessen immer maskieren."""
    mask = mask if mask is not None else os.urandom(4)
    length = len(payload)

    header = bytearray()
    header.append(0x80 | opcode)  # FIN gesetzt, keine Fragmentierung
    if length < 126:
        header.append(0x80 | length)
    elif length < 65536:
        header.append(0x80 | 126)
        header.extend(struct.pack("!H", length))
    else:
        header.append(0x80 | 127)
        header.extend(struct.pack("!Q", length))

    masked = bytes(byte ^ mask[i % 4] for i, byte in enumerate(payload))
    return bytes(header) + mask + masked


def accept_key(nonce: str) -> str:
    """Antwort, die der Server auf den Handshake schicken muss."""
    digest = hashlib.sha1((nonce + _ACCEPT_GUID).encode()).digest()
    return base64.b64encode(digest).decode()


class WebSocket:
    """Blockierender WebSocket-Client."""

    def __init__(self, url: str, timeout: float = 30.0) -> None:
        self.url = url
        self.timeout = timeout
        self.sock: ssl.SSLSocket | socket.socket | None = None
        self._buffer = bytearray()

    # -- Verbindung -------------------------------------------------------

    def connect(self) -> None:
        parsed = urlparse(self.url)
        secure = parsed.scheme in ("wss", "https")
        host = parsed.hostname or ""
        port = parsed.port or (443 if secure else 80)
        path = (parsed.path or "/") + (f"?{parsed.query}" if parsed.query else "")

        raw = socket.create_connection((host, port), timeout=self.timeout)
        self.sock = (
            ssl.create_default_context().wrap_socket(raw, server_hostname=host)
            if secure
            else raw
        )

        nonce = base64.b64encode(os.urandom(16)).decode()
        request = (
            f"GET {path} HTTP/1.1\r\n"
            f"Host: {host}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {nonce}\r\n"
            "Sec-WebSocket-Version: 13\r\n"
            "\r\n"
        )
        self.sock.sendall(request.encode())

        head = bytearray()
        while b"\r\n\r\n" not in head:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise WebSocketError("Verbindung waehrend des Handshakes geschlossen")
            head.extend(chunk)

        raw_head, _, rest = bytes(head).partition(b"\r\n\r\n")
        self._buffer = bytearray(rest)
        lines = raw_head.decode("latin-1").split("\r\n")

        if "101" not in lines[0]:
            raise WebSocketError(f"Server lehnt Upgrade ab: {lines[0]}")

        # Die Antwort pruefen, statt sie zu glauben - sonst wuerde ein
        # falsch konfigurierter Zwischenserver erst spaeter auffallen, dann
        # aber als unverstaendlicher Datenmuell.
        accepted = ""
        for line in lines[1:]:
            name, _, value = line.partition(":")
            if name.strip().lower() == "sec-websocket-accept":
                accepted = value.strip()
        if accepted != accept_key(nonce):
            raise WebSocketError("Server-Antwort auf den Handshake ist ungueltig")

    # -- Datenverkehr -----------------------------------------------------

    def _read(self, count: int) -> None:
        while len(self._buffer) < count:
            if self.sock is None:
                raise WebSocketError("nicht verbunden")
            chunk = self.sock.recv(65536)
            if not chunk:
                raise WebSocketError("Gegenstelle hat die Verbindung geschlossen")
            self._buffer.extend(chunk)

    def _read_frame(self) -> tuple[bool, int, bytes]:
        """Liest einen Rahmen. Rueckgabe: (letzter Teil?, Opcode, Daten)."""
        self._read(2)
        first, second = self._buffer[0], self._buffer[1]
        final = bool(first & 0x80)
        opcode = first & 0x0F
        length = second & 0x7F
        offset = 2

        if length == 126:
            self._read(4)
            length = struct.unpack("!H", bytes(self._buffer[2:4]))[0]
            offset = 4
        elif length == 127:
            self._read(10)
            length = struct.unpack("!Q", bytes(self._buffer[2:10]))[0]
            offset = 10

        # Server maskieren nicht, der Vollstaendigkeit halber trotzdem behandelt.
        masked = bool(second & 0x80)
        if masked:
            self._read(offset + 4)
            mask = bytes(self._buffer[offset : offset + 4])
            offset += 4
        else:
            mask = b""

        self._read(offset + length)
        payload = bytes(self._buffer[offset : offset + length])
        del self._buffer[: offset + length]

        if mask:
            payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        return final, opcode, payload

    def send(self, data: bytes, opcode: int = OP_TEXT) -> None:
        if self.sock is None:
            raise WebSocketError("nicht verbunden")
        self.sock.sendall(build_frame(data, opcode))

    def send_json(self, payload: Any) -> None:
        self.send(json.dumps(payload).encode("utf-8"))

    def recv(self) -> bytes | None:
        """Naechste vollstaendige Nachricht. None, wenn die Gegenstelle schliesst.

        Ping wird selbst beantwortet und Fragmente werden zusammengesetzt -
        beides ist Pflicht, sonst trennt der Server nach kurzer Zeit.
        """
        parts = bytearray()
        started = False

        while True:
            final, opcode, payload = self._read_frame()

            # Steuerrahmen duerfen zwischen den Teilen einer Nachricht
            # auftauchen und beenden sie nicht.
            if opcode == OP_PING:
                self.send(payload, OP_PONG)
                continue
            if opcode == OP_PONG:
                continue
            if opcode == OP_CLOSE:
                return None

            if opcode == OP_CONTINUATION:
                if not started:
                    raise WebSocketError("Fortsetzung ohne begonnene Nachricht")
                parts.extend(payload)
            elif opcode in (OP_TEXT, OP_BINARY):
                parts = bytearray(payload)
                started = True
            else:
                raise WebSocketError(f"Unbekannter Opcode {opcode}")

            if final:
                return bytes(parts)

    def recv_json(self) -> Any | None:
        data = self.recv()
        if data is None:
            return None
        try:
            return json.loads(data)
        except json.JSONDecodeError:
            return None

    def settimeout(self, seconds: float | None) -> None:
        if self.sock is not None:
            self.sock.settimeout(seconds)

    def close(self) -> None:
        if self.sock is None:
            return
        try:
            self.send(b"", OP_CLOSE)
        except (OSError, WebSocketError):
            pass
        try:
            self.sock.close()
        finally:
            self.sock = None

    def __enter__(self) -> "WebSocket":
        self.connect()
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()
