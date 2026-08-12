"""Tests fuer WebSocket-Client und Live-Strom.

Kein Netzwerk: geprueft werden die Rahmen-Kodierung nach RFC 6455 und das
Auslesen der Erstellungs-Ereignisse aus den Programm-Logs. Genau dort steckt
die Arbeit - alles andere ist Verbindungsverwaltung.
"""

from __future__ import annotations

import base64
import struct
import time

import pytest

from xeno.live import (
    CREATE_DISCRIMINATOR,
    LiveFeed,
    NewToken,
    b58encode,
    extract_new_tokens,
    parse_create_event,
)
from xeno.ws import OP_TEXT, WebSocket, WebSocketError, accept_key, build_frame


class TestFrames:
    def test_small_payload(self):
        frame = build_frame(b"hallo", mask=b"\x00\x00\x00\x00")
        assert frame[0] == 0x81           # FIN + Text
        assert frame[1] == 0x80 | 5       # maskiert, Laenge 5
        assert frame[6:] == b"hallo"

    def test_client_must_mask(self):
        """Der Standard verlangt Maskierung fuer Clients - ohne trennt der
        Server die Verbindung."""
        frame = build_frame(b"x")
        assert frame[1] & 0x80

    def test_masking_is_reversible(self):
        mask = b"\x01\x02\x03\x04"
        frame = build_frame(b"abcdefgh", mask=mask)
        payload = frame[6:]
        assert bytes(b ^ mask[i % 4] for i, b in enumerate(payload)) == b"abcdefgh"

    def test_medium_payload_uses_two_byte_length(self):
        frame = build_frame(b"x" * 200, mask=b"\x00" * 4)
        assert frame[1] & 0x7F == 126
        assert struct.unpack("!H", frame[2:4])[0] == 200

    def test_large_payload_uses_eight_byte_length(self):
        frame = build_frame(b"x" * 70_000, mask=b"\x00" * 4)
        assert frame[1] & 0x7F == 127
        assert struct.unpack("!Q", frame[2:10])[0] == 70_000

    def test_empty_payload(self):
        frame = build_frame(b"", mask=b"\x00" * 4)
        assert frame[1] & 0x7F == 0


class TestHandshake:
    def test_accept_key_matches_the_standard(self):
        """Beispiel aus RFC 6455, Abschnitt 1.3."""
        assert accept_key("dGhlIHNhbXBsZSBub25jZQ==") == "s3pPLMBiTxaQ9kYGzzhZRbK+xOo="

    def test_wrong_answer_is_rejected(self):
        assert accept_key("abc") != accept_key("abd")


class TestBase58:
    def test_known_value(self):
        assert b58encode(b"\x00" * 32) == "1" * 32

    def test_leading_zeros_become_ones(self):
        assert b58encode(b"\x00\x01").startswith("1")

    def test_length_of_a_pubkey(self):
        """32 Byte ergeben eine Adresse von 32 bis 44 Zeichen."""
        encoded = b58encode(bytes(range(32)))
        assert 32 <= len(encoded) <= 44

    def test_alphabet_excludes_confusable_characters(self):
        encoded = b58encode(bytes(range(1, 33)))
        assert not set(encoded) & set("0OIl")


def make_create_blob(
    name: str = "Testcoin",
    symbol: str = "TEST",
    uri: str = "https://example.test/x",
    mint: bytes = bytes(range(32)),
    creator: bytes = bytes(range(32, 64)),
) -> bytes:
    """Baut ein Ereignis im Format, das pump.fun tatsaechlich schreibt."""
    blob = bytearray(CREATE_DISCRIMINATOR)
    for text in (name, symbol, uri):
        raw = text.encode()
        blob.extend(struct.pack("<I", len(raw)))
        blob.extend(raw)
    blob.extend(mint)
    blob.extend(b"\x00" * 64)  # bonding curve und deren Token-Konto
    blob.extend(creator)
    return bytes(blob)


class TestCreateEvent:
    def test_reads_all_fields(self):
        token = parse_create_event(make_create_blob())
        assert token is not None
        assert token.name == "Testcoin"
        assert token.symbol == "TEST"
        assert len(token.mint) >= 32

    def test_other_events_are_ignored(self):
        """Kaufe und Verkaeufe tragen andere Kennungen - sie duerfen nicht
        als Erstellung durchgehen."""
        blob = bytes.fromhex("bdd b7f d3b4 e661 ee".replace(" ", "")) + b"\x00" * 64
        assert parse_create_event(blob) is None

    def test_truncated_data_is_survived(self):
        """Ein geaendertes Format soll den Strom nicht abreissen lassen."""
        assert parse_create_event(make_create_blob()[:20]) is None

    def test_absurd_length_is_rejected(self):
        blob = bytearray(CREATE_DISCRIMINATOR)
        blob.extend(struct.pack("<I", 999_999))
        assert parse_create_event(bytes(blob)) is None

    def test_empty_input(self):
        assert parse_create_event(b"") is None

    def test_unicode_names_do_not_crash(self):
        token = parse_create_event(make_create_blob(name="Töken 🚀"))
        assert token is not None


class TestLogExtraction:
    def _log_line(self, blob: bytes) -> str:
        return "Program data: " + base64.b64encode(blob).decode()

    def test_finds_the_creation_in_the_logs(self):
        logs = [
            "Program log: Instruction: CreateV2",
            self._log_line(make_create_blob(symbol="ABC")),
            "Program log: success",
        ]
        found = extract_new_tokens(logs, signature="sig123")
        assert len(found) == 1
        assert found[0].symbol == "ABC"
        assert found[0].signature == "sig123"

    def test_logs_without_creation_yield_nothing(self):
        assert extract_new_tokens(["Program log: Instruction: BuyV2"]) == []

    def test_broken_base64_is_skipped(self):
        assert extract_new_tokens(["Program data: !!!kein-base64!!!"]) == []

    def test_several_creations_in_one_transaction(self):
        logs = [
            self._log_line(make_create_blob(symbol="EINS")),
            self._log_line(make_create_blob(symbol="ZWEI")),
        ]
        assert [t.symbol for t in extract_new_tokens(logs)] == ["EINS", "ZWEI"]


class TestMaturation:
    """Ein Token in Sekunde eins ist nicht bewertbar - es hat noch niemand
    gehandelt. Der Strom sammelt deshalb und gibt erst heraus, was alt genug
    fuer eine Aussage ist."""

    def _feed(self) -> LiveFeed:
        return LiveFeed("wss://example.test")

    def _add(self, feed: LiveFeed, mint: str, age_seconds: float) -> None:
        feed._pending[mint] = NewToken(mint=mint, seen_at=time.time() - age_seconds)

    def test_young_tokens_are_held_back(self):
        feed = self._feed()
        self._add(feed, "frisch", 10)
        assert feed.take_matured(min_age_seconds=120) == []
        assert feed.pending_count == 1

    def test_matured_tokens_are_released(self):
        feed = self._feed()
        self._add(feed, "reif", 200)
        assert [t.mint for t in feed.take_matured(min_age_seconds=120)] == ["reif"]

    def test_released_tokens_are_not_returned_twice(self):
        feed = self._feed()
        self._add(feed, "reif", 200)
        feed.take_matured(min_age_seconds=120)
        assert feed.take_matured(min_age_seconds=120) == []
        assert feed.pending_count == 0

    def test_oldest_are_released_first(self):
        feed = self._feed()
        self._add(feed, "aelter", 400)
        self._add(feed, "juenger", 200)
        assert [t.mint for t in feed.take_matured(120)] == ["aelter", "juenger"]

    def test_limit_is_respected(self):
        feed = self._feed()
        for i in range(10):
            self._add(feed, f"m{i}", 200 + i)
        assert len(feed.take_matured(120, limit=3)) == 3
        assert feed.pending_count == 7

    def test_stale_entries_are_dropped(self):
        """Wer nach Stunden noch wartet, ist nicht mehr 'frisch'."""
        feed = self._feed()
        self._add(feed, "vergessen", 40_000)
        self._add(feed, "aktuell", 100)
        assert feed.drop_stale(max_age_seconds=6 * 3600) == 1
        assert feed.pending_count == 1

    def test_pending_pool_stays_bounded(self):
        """Bei rund 40 Neuzugaengen pro Minute waeren es sonst nach einem Tag
        ueber 50.000 Eintraege."""
        feed = LiveFeed("wss://example.test", max_pending=5)
        assert feed.max_pending == 5


class TestConnectionErrors:
    def test_sending_without_a_connection_fails_clearly(self):
        with pytest.raises(WebSocketError):
            WebSocket("wss://example.test").send(b"x", OP_TEXT)

    def test_feed_is_not_running_before_start(self):
        assert LiveFeed("wss://example.test").running is False
