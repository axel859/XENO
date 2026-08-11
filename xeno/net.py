"""HTTP- und Solana-RPC-Client.

Beides mit Rate-Limiting und Backoff. Das ist hier kein Luxus: der
oeffentliche Solana-RPC antwortet schon beim zweiten Call in Folge mit 429,
und auch die kostenlosen Daten-APIs drosseln zuegig. Ohne Drosselung liefert
der Scanner nur Fehler statt Ergebnisse.

Nur Standardbibliothek - keine externen Abhaengigkeiten.
"""

from __future__ import annotations

import json
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

USER_AGENT = "xeno/0.1 (+https://github.com/axel859/XENO)"

#: Statuscodes, bei denen ein erneuter Versuch sinnvoll ist.
RETRY_STATUS = frozenset({408, 425, 429, 500, 502, 503, 504, 520, 522, 524})

#: Query-Parameter, die ein Geheimnis enthalten koennen.
_SECRET_PARAMS = frozenset({"api-key", "apikey", "api_key", "key", "token", "access_token"})

#: Telegram legt den Bot-Token in den Pfad: /bot<id>:<secret>/methode
_TELEGRAM_TOKEN = re.compile(r"/bot\d+:[\w-]+")


def redact_url(url: str) -> str:
    """Entfernt Zugangsdaten aus einer URL, bevor sie in einer Meldung landet.

    Sowohl der Helius-RPC (``?api-key=...``) als auch die Telegram-API
    (``/bot123:ABC/...``) tragen ihr Geheimnis in der URL. Ohne diese
    Bereinigung stuende es in jeder Fehlermeldung, in jedem Log und in jedem
    Screenshot, den man zum Debuggen weitergibt.
    """
    url = _TELEGRAM_TOKEN.sub("/bot***", url)
    head, sep, query = url.partition("?")
    if not sep:
        return url
    parts = []
    for chunk in query.split("&"):
        name, delim, _ = chunk.partition("=")
        parts.append(f"{name}=***" if name.lower() in _SECRET_PARAMS and delim else chunk)
    return f"{head}?{'&'.join(parts)}"


class HttpError(RuntimeError):
    """Fehlgeschlagener HTTP-Request nach allen Wiederholungen."""

    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class RpcError(RuntimeError):
    """Der RPC-Knoten hat einen JSON-RPC-Fehler zurueckgegeben."""

    def __init__(self, message: str, code: int | None = None) -> None:
        super().__init__(message)
        self.code = code


class RateLimiter:
    """Erzwingt einen Mindestabstand zwischen Requests. Thread-safe."""

    def __init__(self, calls_per_second: float) -> None:
        self._min_interval = 1.0 / calls_per_second if calls_per_second > 0 else 0.0
        self._lock = threading.Lock()
        self._next_allowed = 0.0

    def acquire(self) -> None:
        if self._min_interval <= 0:
            return
        with self._lock:
            now = time.monotonic()
            wait = self._next_allowed - now
            if wait > 0:
                time.sleep(wait)
                now = time.monotonic()
            self._next_allowed = now + self._min_interval

    def penalize(self, seconds: float) -> None:
        """Schiebt das naechste erlaubte Zeitfenster nach hinten (nach einem 429)."""
        with self._lock:
            self._next_allowed = max(self._next_allowed, time.monotonic() + seconds)


class HttpClient:
    """Schlanker JSON-HTTP-Client mit Retry und Backoff."""

    def __init__(
        self,
        rate_limit: float = 4.0,
        timeout: float = 25.0,
        max_retries: int = 4,
        user_agent: str = USER_AGENT,
    ) -> None:
        self.limiter = RateLimiter(rate_limit)
        self.timeout = timeout
        self.max_retries = max_retries
        self.user_agent = user_agent

    def _sleep_for_retry(self, attempt: int, retry_after: str | None) -> None:
        """Wartet nach Retry-After, sonst exponentiell (1s, 2s, 4s, 8s ...)."""
        delay = 2.0**attempt
        if retry_after:
            try:
                delay = max(delay, min(float(retry_after), 30.0))
            except ValueError:
                pass
        self.limiter.penalize(delay)
        time.sleep(delay)

    def request(
        self,
        url: str,
        *,
        method: str = "GET",
        params: dict[str, Any] | None = None,
        body: Any = None,
        headers: dict[str, str] | None = None,
    ) -> Any:
        """Fuehrt den Request aus und gibt die geparste JSON-Antwort zurueck."""
        if params:
            clean = {k: v for k, v in params.items() if v is not None}
            if clean:
                url = f"{url}{'&' if '?' in url else '?'}{urllib.parse.urlencode(clean)}"

        data = json.dumps(body).encode("utf-8") if body is not None else None
        all_headers = {
            "User-Agent": self.user_agent,
            "Accept": "application/json",
        }
        if data is not None:
            all_headers["Content-Type"] = "application/json"
        if headers:
            all_headers.update(headers)

        last_error: Exception | None = None
        for attempt in range(self.max_retries):
            self.limiter.acquire()
            req = urllib.request.Request(url, data=data, headers=all_headers, method=method)
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as response:
                    raw = response.read()
                return json.loads(raw) if raw else None
            except urllib.error.HTTPError as exc:
                last_error = exc
                if exc.code in RETRY_STATUS and attempt < self.max_retries - 1:
                    self._sleep_for_retry(attempt, exc.headers.get("Retry-After"))
                    continue
                raise HttpError(
                    f"HTTP {exc.code} bei {redact_url(url)}", status=exc.code
                ) from exc
            except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
                last_error = exc
                if attempt < self.max_retries - 1:
                    self._sleep_for_retry(attempt, None)
                    continue

        raise HttpError(f"Request an {redact_url(url)} fehlgeschlagen: {last_error}")

    def get(self, url: str, **kwargs: Any) -> Any:
        return self.request(url, method="GET", **kwargs)

    def post(self, url: str, body: Any, **kwargs: Any) -> Any:
        return self.request(url, method="POST", body=body, **kwargs)


class SolanaRpc:
    """JSON-RPC-Client fuer Solana.

    Deckt genau die Methoden ab, die der Deep-Check braucht. Batch-Requests
    werden genutzt, wo es geht - das spart bei begrenztem Kontingent deutlich.
    """

    def __init__(self, url: str, http: HttpClient | None = None, rate_limit: float = 2.0) -> None:
        self.url = url
        self.http = http or HttpClient(rate_limit=rate_limit)
        self._request_id = 0

    def _next_id(self) -> int:
        self._request_id += 1
        return self._request_id

    def call(self, method: str, params: list[Any] | None = None) -> Any:
        payload = {
            "jsonrpc": "2.0",
            "id": self._next_id(),
            "method": method,
            "params": params or [],
        }
        response = self.http.post(self.url, payload)
        if isinstance(response, dict) and "error" in response:
            error = response["error"] or {}
            raise RpcError(error.get("message", str(error)), code=error.get("code"))
        return (response or {}).get("result")

    def batch(self, calls: list[tuple[str, list[Any]]]) -> list[Any]:
        """Mehrere Methoden in einem Request. Ergebnisse in Eingabereihenfolge.

        Ein Fehler in einem Teil-Call fuehrt zu ``None`` an dieser Stelle,
        statt den kompletten Batch scheitern zu lassen.
        """
        if not calls:
            return []
        payload = []
        ids = []
        for method, params in calls:
            request_id = self._next_id()
            ids.append(request_id)
            payload.append(
                {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}
            )

        response = self.http.post(self.url, payload)
        if isinstance(response, dict):  # Manche Knoten antworten auf Batches mit Fehlerobjekt
            raise RpcError(str(response.get("error", response)))

        by_id = {item.get("id"): item for item in (response or []) if isinstance(item, dict)}
        results = []
        for request_id in ids:
            item = by_id.get(request_id) or {}
            results.append(None if "error" in item else item.get("result"))
        return results

    # -- Konkrete Methoden ------------------------------------------------

    def get_account_info(self, address: str, encoding: str = "jsonParsed") -> dict[str, Any] | None:
        result = self.call("getAccountInfo", [address, {"encoding": encoding}])
        return (result or {}).get("value")

    def get_multiple_accounts(
        self, addresses: list[str], encoding: str = "jsonParsed"
    ) -> list[dict[str, Any] | None]:
        """Bis zu 100 Accounts pro Call - der RPC begrenzt hier."""
        values: list[dict[str, Any] | None] = []
        for start in range(0, len(addresses), 100):
            chunk = addresses[start : start + 100]
            result = self.call("getMultipleAccounts", [chunk, {"encoding": encoding}])
            values.extend((result or {}).get("value") or [None] * len(chunk))
        return values

    def get_token_largest_accounts(self, mint: str) -> list[dict[str, Any]]:
        """Die groessten 20 Token-Accounts. Mehr gibt der RPC nicht her."""
        result = self.call("getTokenLargestAccounts", [mint])
        return (result or {}).get("value") or []

    def get_token_supply(self, mint: str) -> dict[str, Any] | None:
        result = self.call("getTokenSupply", [mint])
        return (result or {}).get("value")

    def get_signatures_for_address(
        self, address: str, limit: int = 100, before: str | None = None
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"limit": min(limit, 1000)}
        if before:
            params["before"] = before
        return self.call("getSignaturesForAddress", [address, params]) or []

    def get_transaction(self, signature: str) -> dict[str, Any] | None:
        return self.call(
            "getTransaction",
            [
                signature,
                {
                    "encoding": "jsonParsed",
                    "maxSupportedTransactionVersion": 0,
                    "commitment": "confirmed",
                },
            ],
        )
