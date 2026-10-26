"""Google Sheets client (service account), and an in-memory fake with the same interface.

Only the handful of calls the sync needs: list tabs, read whole tabs, write blocks
starting at a cell, clear ranges, and raw spreadsheets.batchUpdate requests
(add tabs, freeze headers, dropdowns). Auth is a service-account JWT signed with
authlib, so no Google SDK is needed.
"""

from __future__ import annotations

import json
import re
import threading
import time
from pathlib import Path

import httpx

SCOPE = "https://www.googleapis.com/auth/spreadsheets"
API = "https://sheets.googleapis.com/v4/spreadsheets"


class SheetsError(RuntimeError):
    pass


def quote(tab: str) -> str:
    return "'" + tab.replace("'", "''") + "'"


def col_letter(n: int) -> str:
    """0 -> A, 25 -> Z, 26 -> AA."""
    s = ""
    n += 1
    while n:
        n, r = divmod(n - 1, 26)
        s = chr(65 + r) + s
    return s


def a1(tab: str, row: int, col: int) -> str:
    """Zero-based row/col -> 'Tab'!B3."""
    return f"{quote(tab)}!{col_letter(col)}{row + 1}"


_A1 = re.compile(r"^'((?:[^']|'')*)'!([A-Z]+)(\d+)$")


def parse_a1(ref: str) -> tuple[str, int, int]:
    m = _A1.match(ref)
    if not m:
        raise ValueError(f"not a single-cell reference: {ref}")
    tab, letters, row = m.group(1).replace("''", "'"), m.group(2), int(m.group(3))
    col = 0
    for ch in letters:
        col = col * 26 + (ord(ch) - 64)
    return tab, row - 1, col - 1


def sheet_id_from(url_or_id: str) -> str:
    m = re.search(r"/spreadsheets/d/([A-Za-z0-9_-]+)", url_or_id)
    return m.group(1) if m else url_or_id.strip()


class Sheets:
    def __init__(self, key_file: str, spreadsheet_id: str, timeout: float = 15):
        self.key_file = key_file
        self._key_stamp = None
        self._load_key()
        self.id = sheet_id_from(spreadsheet_id)
        self.http = httpx.Client(timeout=timeout)
        self._token, self._exp = "", 0.0
        self._lock = threading.Lock()

    def _load_key(self) -> None:
        """(Re)read the service-account JSON. Called again whenever the file changes, so a
        rotated key is picked up without restarting."""
        p = Path(self.key_file)
        st = p.stat()
        info = json.loads(p.read_text())
        self.email = info["client_email"]
        self._key = info["private_key"]
        self._key_id = info.get("private_key_id", "")
        self._token_uri = info.get("token_uri", "https://oauth2.googleapis.com/token")
        self._key_stamp = (st.st_mtime_ns, st.st_size, st.st_ino)

    @property
    def url(self) -> str:
        return f"https://docs.google.com/spreadsheets/d/{self.id}/edit"

    def _auth(self) -> dict:
        with self._lock:
            try:
                st = Path(self.key_file).stat()
                if (st.st_mtime_ns, st.st_size, st.st_ino) != self._key_stamp:
                    self._load_key()
                    self._token, self._exp = "", 0.0  # new key: sign in again
            except (OSError, ValueError, KeyError) as e:
                if not self._key:
                    raise SheetsError(f"cannot read the Google key file {self.key_file}: {e}") from e
            if time.time() > self._exp - 120:
                from authlib.jose import jwt
                now = int(time.time())
                claims = {"iss": self.email, "scope": SCOPE, "aud": self._token_uri, "iat": now, "exp": now + 3600}
                header = {"alg": "RS256", **({"kid": self._key_id} if self._key_id else {})}
                assertion = jwt.encode(header, claims, self._key).decode()
                r = self.http.post(self._token_uri, data={
                    "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer", "assertion": assertion})
                if r.status_code != 200:
                    raise SheetsError(f"Google sign-in failed: HTTP {r.status_code} {r.text[:200]}")
                body = r.json()
                self._token, self._exp = body["access_token"], time.time() + float(body.get("expires_in", 3600))
            return {"Authorization": f"Bearer {self._token}"}

    def _call(self, method: str, url: str, **kw) -> dict:
        for attempt in range(3):
            r = self.http.request(method, url, headers=self._auth(), **kw)
            if r.status_code == 429 or r.status_code >= 500:
                time.sleep(1.5 * (attempt + 1))  # quota or Google hiccup: back off and retry
                continue
            if r.status_code >= 400:
                try:
                    msg = r.json().get("error", {}).get("message", r.text)
                except ValueError:
                    msg = r.text
                raise SheetsError(f"Google Sheets: HTTP {r.status_code} {msg[:300]}")
            return r.json() if r.content else {}
        raise SheetsError(f"Google Sheets: HTTP {r.status_code} after retries")

    # -- interface ----------------------------------------------------------------
    def tab_ids(self) -> dict[str, int]:
        body = self._call("GET", f"{API}/{self.id}", params={"fields": "sheets.properties(sheetId,title)"})
        return {s["properties"]["title"]: s["properties"]["sheetId"] for s in body.get("sheets", [])}

    def meta(self) -> dict:
        """Tabs with the formatting the styler replaces (conditional formats, bandings, protections)."""
        return self._call("GET", f"{API}/{self.id}", params={"fields": (
            "sheets(properties(sheetId,title,index,gridProperties(rowCount,columnCount)),conditionalFormats(ranges),bandedRanges(bandedRangeId),"
            "protectedRanges(protectedRangeId,description))")})

    def read(self, tabs: list[str]) -> dict[str, list[list]]:
        if not tabs:
            return {}
        body = self._call("GET", f"{API}/{self.id}/values:batchGet", params=[
            *(("ranges", quote(t)) for t in tabs),
            ("valueRenderOption", "UNFORMATTED_VALUE"), ("dateTimeRenderOption", "SERIAL_NUMBER"),
            ("majorDimension", "ROWS")])
        out = {}
        for tab, vr in zip(tabs, body.get("valueRanges", [])):
            out[tab] = vr.get("values", [])
        return out

    def write(self, blocks: dict[str, list[list]], raw: bool = True) -> None:
        """{"'Tab'!A1": rows} -> each block written from that cell. RAW keeps text as typed (no formulas);
        raw=False parses values as if typed in the Sheet (dates become dates)."""
        if blocks:
            self._call("POST", f"{API}/{self.id}/values:batchUpdate", json={
                "valueInputOption": "RAW" if raw else "USER_ENTERED",
                "data": [{"range": k, "values": v} for k, v in blocks.items()]})

    def clear(self, ranges: list[str]) -> None:
        if ranges:
            self._call("POST", f"{API}/{self.id}/values:batchClear", json={"ranges": ranges})

    def batch_update(self, requests: list[dict]) -> dict:
        if not requests:
            return {}
        return self._call("POST", f"{API}/{self.id}:batchUpdate", json={"requests": requests})


class FakeSheets:
    """In-memory spreadsheet for tests and the dev server."""

    url = "https://docs.google.com/spreadsheets/d/FAKE/edit"
    email = "labslots-sync@fake.iam.gserviceaccount.com"

    def __init__(self, tabs: dict[str, list[list]] | None = None):
        self.tabs: dict[str, list[list]] = {k: [list(r) for r in v] for k, v in (tabs or {}).items()}
        self.requests: list[dict] = []
        self.fail = False
        self.reads = self.writes = 0

    def _check(self):
        if self.fail:
            raise SheetsError("Google Sheets: HTTP 503 unavailable (fake)")

    def tab_ids(self) -> dict[str, int]:
        self._check()
        return {t: i for i, t in enumerate(self.tabs)}

    def meta(self) -> dict:
        self._check()
        return {"sheets": [{"properties": {"sheetId": i, "title": t, "gridProperties": {"rowCount": 1000, "columnCount": 26}},
                            "conditionalFormats": [], "bandedRanges": [],
                            "protectedRanges": []} for i, t in enumerate(self.tabs)]}

    def read(self, tabs: list[str]) -> dict[str, list[list]]:
        self._check()
        self.reads += 1
        out = {}
        for t in tabs:
            rows = [list(r) for r in self.tabs.get(t, [])]
            while rows and not any(str(c).strip() for c in rows[-1]):
                rows.pop()  # the API drops trailing empty rows
            out[t] = [_rstrip(r) for r in rows]
        return out

    def write(self, blocks: dict[str, list[list]], raw: bool = True) -> None:
        self._check()
        self.writes += 1
        for ref, rows in blocks.items():
            tab, r0, c0 = parse_a1(ref)
            grid = self.tabs.setdefault(tab, [])
            for i, row in enumerate(rows):
                while len(grid) <= r0 + i:
                    grid.append([])
                line = grid[r0 + i]
                for j, v in enumerate(row):
                    while len(line) <= c0 + j:
                        line.append("")
                    line[c0 + j] = v

    def clear(self, ranges: list[str]) -> None:
        self._check()
        for ref in ranges:
            tab = ref[1:ref.rindex("'")].replace("''", "'") if ref.startswith("'") else ref
            if tab in self.tabs:
                self.tabs[tab] = []

    def batch_update(self, requests: list[dict]) -> dict:
        self._check()
        replies = []
        for req in requests:
            self.requests.append(req)
            if "addSheet" in req:
                title = req["addSheet"]["properties"]["title"]
                self.tabs.setdefault(title, [])
                replies.append({"addSheet": {"properties": {"title": title, "sheetId": list(self.tabs).index(title)}}})
            elif "updateSheetProperties" in req and "title" in req["updateSheetProperties"]["fields"]:
                props = req["updateSheetProperties"]["properties"]
                old = list(self.tabs)[props["sheetId"]]
                self.tabs = {(props["title"] if t == old else t): rows for t, rows in self.tabs.items()}
                replies.append({})
            elif "deleteSheet" in req:
                sid = req["deleteSheet"]["sheetId"]
                title = list(self.tabs)[sid]
                del self.tabs[title]
                replies.append({})
            else:
                replies.append({})
        return {"replies": replies}

    # test helper
    def set(self, tab: str, row: int, col: int, value) -> None:
        self.write({a1(tab, row, col): [[value]]})


def _rstrip(row: list) -> list:
    row = list(row)
    while row and (row[-1] is None or str(row[-1]) == ""):
        row.pop()
    return row
