#!/usr/bin/env python3
"""Export RSNA 2026 South and North Hall exhibitor data to CSV.

The interactive floor plan uses a public JSON proxy. Two hall requests provide
the exhibitor/booth assignments and booth dimensions; one small request per
unique exhibitor provides the public company address, country, and website.

Successful responses are cached immediately, so an interrupted run can be
resumed without repeating completed requests.
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import re
import sys
import time
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlparse

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


BASE_URL = "https://rsna2026.mapyourshow.com"
PROXY_PATH = "/8_0/exhview/02/exh-remote-proxy.cfm"
HALLS = {"A": "South Hall", "B": "North Hall"}

CSV_COLUMNS = [
    "company_name",
    "company_address",
    "country",
    "hall",
    "booth_number",
    "booth_size",
    "booth_area_sq_ft",
    "website_url",
]

DIMENSIONS_RE = re.compile(
    r"^\s*(\d+(?:\.\d+)?)\s*[xX\u00d7]\s*(\d+(?:\.\d+)?)\s*(?:ft\.?)?\s*$",
    re.IGNORECASE,
)

LOG = logging.getLogger("rsna-scraper")


class ScrapeError(RuntimeError):
    """Raised when a response cannot safely be used."""


def clean_text(value: Any) -> str:
    """Return a single-line, trimmed string for a JSON value."""
    if value is None:
        return ""
    return re.sub(r"\s+", " ", str(value)).strip()


def decimal_text(value: Decimal) -> str:
    """Format a Decimal without exponent notation or trailing zeroes."""
    if value == value.to_integral_value():
        return str(value.quantize(Decimal("1")))
    return format(value.normalize(), "f")


def booth_measurements(raw_dimensions: Any, irregular: Any) -> tuple[str, str]:
    """Return (display dimensions, square feet) from a floor-plan booth row.

    Regular booths publish width x depth. Irregular booths publish only their
    area in the same source field, so their size is intentionally left blank.
    """
    raw = clean_text(raw_dimensions)
    match = DIMENSIONS_RE.match(raw)
    if match:
        width = Decimal(match.group(1))
        depth = Decimal(match.group(2))
        return f"{decimal_text(width)}x{decimal_text(depth)}", decimal_text(width * depth)

    is_irregular = str(irregular).strip().lower() in {"1", "true", "yes"}
    if is_irregular and raw:
        try:
            return "", decimal_text(Decimal(raw.replace(",", "")))
        except InvalidOperation:
            pass
    return raw.replace(" ", ""), ""


def company_address(detail: dict[str, Any]) -> str:
    """Build the address column, deliberately excluding country."""
    values = [
        detail.get("address1"),
        detail.get("address2"),
        detail.get("address3"),
        detail.get("city"),
        detail.get("state"),
        detail.get("zip"),
    ]
    parts: list[str] = []
    for value in values:
        part = clean_text(value)
        if part and part.casefold() not in {item.casefold() for item in parts}:
            parts.append(part)
    return ", ".join(parts)


def website_url(value: Any) -> str:
    """Normalize a public company website to an HTTP(S) URL."""
    url = clean_text(value)
    if not url:
        return ""
    supplied_scheme = re.match(r"^([A-Za-z][A-Za-z0-9+.-]*):", url)
    if supplied_scheme and supplied_scheme.group(1).lower() not in {"http", "https"}:
        return ""
    if url.startswith("//"):
        url = "https:" + url
    if "://" not in url:
        url = "https://" + url
    parsed = urlparse(url)
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
        return ""
    return url


def parse_hall_payload(payload: Any, hall_id: str) -> list[dict[str, Any]]:
    """Validate and select named exhibitor-booth assignments from one hall."""
    if not isinstance(payload, list):
        raise ScrapeError(f"hall {hall_id}: expected a JSON array")

    records: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for item in payload:
        if not isinstance(item, dict):
            continue
        company = clean_text(item.get("exhname"))
        exhibitor_id = clean_text(item.get("exhid"))
        booth = clean_text(item.get("boothdisplay") or item.get("booth"))
        item_hall = clean_text(item.get("hallid")) or hall_id

        # "Available" and "On Hold" spaces are included in the page's badge
        # count, but they are inventory, not exhibitors.
        if not company or not exhibitor_id or not booth:
            continue

        key = (item_hall, booth, exhibitor_id)
        if key in seen:
            continue
        seen.add(key)
        records.append(item)
    return records


def parse_exhibitor_payload(payload: Any, exhibitor_id: str) -> dict[str, Any]:
    """Validate one getExhibitorInfo response."""
    candidates = payload if isinstance(payload, list) else [payload]
    for item in candidates:
        if not isinstance(item, dict):
            continue
        response_id = clean_text(item.get("exhid"))
        if not response_id or response_id == exhibitor_id:
            return item
    raise ScrapeError(f"exhibitor {exhibitor_id}: response contained no matching record")


def build_session(retries: int, user_agent: str) -> requests.Session:
    """Create a session with bounded exponential backoff and Retry-After support."""
    retry = Retry(
        total=retries,
        connect=retries,
        read=retries,
        status=retries,
        allowed_methods=frozenset({"GET"}),
        status_forcelist=(429, 500, 502, 503, 504),
        backoff_factor=1.0,
        respect_retry_after_header=True,
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry, pool_connections=2, pool_maxsize=2)
    session = requests.Session()
    session.mount("https://", adapter)
    session.headers.update(
        {
            "User-Agent": user_agent,
            "Accept": "application/json",
            "Accept-Language": "en-US,en;q=0.9",
            "Referer": f"{BASE_URL}/8_0/exhview/index.cfm",
            "X-Requested-With": "XMLHttpRequest",
        }
    )
    return session


class CachedClient:
    """Timeout-aware JSON client that persists each successful response."""

    def __init__(
        self,
        cache_dir: Path,
        *,
        connect_timeout: float,
        read_timeout: float,
        retries: int,
        delay: float,
        refresh: bool,
        user_agent: str,
    ) -> None:
        self.cache_dir = cache_dir
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.timeout = (connect_timeout, read_timeout)
        self.delay = delay
        self.refresh = refresh
        self.session = build_session(retries, user_agent)
        self.last_request_finished = 0.0
        self.network_requests = 0
        self.cache_hits = 0

    def close(self) -> None:
        self.session.close()

    def _read_cache(self, path: Path) -> Any | None:
        if self.refresh or not path.exists():
            return None
        try:
            result = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            LOG.warning("Ignoring invalid cache file %s: %s", path, exc)
            return None
        self.cache_hits += 1
        return result

    def _write_cache(self, path: Path, payload: Any) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
            encoding="utf-8",
        )
        temporary.replace(path)

    def get_json(self, params: dict[str, Any], cache_path: Path) -> Any:
        cached = self._read_cache(cache_path)
        if cached is not None:
            return cached

        wait = self.delay - (time.monotonic() - self.last_request_finished)
        if wait > 0:
            time.sleep(wait)

        try:
            response = self.session.get(
                BASE_URL + PROXY_PATH,
                params=params,
                timeout=self.timeout,
            )
            self.network_requests += 1
            self.last_request_finished = time.monotonic()
            response.raise_for_status()
            payload = response.json()
        except requests.Timeout as exc:
            raise ScrapeError(f"request timed out after retries: {exc}") from exc
        except requests.RequestException as exc:
            raise ScrapeError(f"request failed after retries: {exc}") from exc
        except ValueError as exc:
            raise ScrapeError("server returned invalid JSON") from exc

        self._write_cache(cache_path, payload)
        return payload

    def get_hall(self, hall_id: str) -> Any:
        return self.get_json(
            {
                "action": "getExhViewBoothResults",
                "hallID": hall_id,
                "pavilion": "",
                "boothtypeid": "",
                "availableonly": "false",
                "boothsizefrom": "",
                "boothsizeto": "",
                "propertyid": "",
                "orsearchcount": "0",
            },
            self.cache_dir / "halls" / f"{hall_id}.json",
        )

    def get_exhibitor(self, exhibitor_id: str) -> Any:
        return self.get_json(
            {
                "action": "getExhibitorInfo",
                "exhID": exhibitor_id,
                "showCustID": "",
            },
            self.cache_dir / "exhibitors" / f"{exhibitor_id}.json",
        )


def make_csv_rows(
    assignments: Iterable[dict[str, Any]], details: dict[str, dict[str, Any]]
) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for assignment in assignments:
        exhibitor_id = clean_text(assignment.get("exhid"))
        detail = details.get(exhibitor_id, {})
        booth_size, area = booth_measurements(
            assignment.get("boothdims"), assignment.get("irregular")
        )
        hall_id = clean_text(assignment.get("hallid"))
        rows.append(
            {
                "company_name": clean_text(
                    detail.get("exhname") or assignment.get("exhname")
                ),
                "company_address": company_address(detail),
                "country": clean_text(detail.get("country")),
                "hall": HALLS.get(hall_id, hall_id),
                "booth_number": clean_text(
                    assignment.get("boothdisplay") or assignment.get("booth")
                ),
                "booth_size": booth_size,
                "booth_area_sq_ft": area,
                "website_url": website_url(detail.get("url")),
            }
        )
    return rows


def write_csv(path: Path, rows: Iterable[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    temporary.replace(path)


def write_summary(path: Path, summary: dict[str, Any]) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    temporary.replace(path)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("rsna_2026_exhibitors.csv"))
    parser.add_argument(
        "--cache-dir", type=Path, default=Path(".cache/rsna2026"),
        help="response cache used to resume interrupted runs",
    )
    parser.add_argument("--connect-timeout", type=float, default=10.0)
    parser.add_argument("--read-timeout", type=float, default=45.0)
    parser.add_argument("--retries", type=int, default=5)
    parser.add_argument(
        "--delay", type=float, default=0.15,
        help="minimum seconds between requests (default: 0.15)",
    )
    parser.add_argument(
        "--refresh", action="store_true",
        help="ignore cached responses and fetch current floor-plan data",
    )
    parser.add_argument(
        "--limit", type=int,
        help="process only N booth assignments (useful for a smoke test)",
    )
    parser.add_argument("--verbose", action="store_true")
    return parser.parse_args(argv)


def run(args: argparse.Namespace) -> int:
    if args.connect_timeout <= 0 or args.read_timeout <= 0:
        raise ScrapeError("timeouts must be greater than zero")
    if args.retries < 0 or args.delay < 0:
        raise ScrapeError("retries and delay cannot be negative")
    if args.limit is not None and args.limit <= 0:
        raise ScrapeError("--limit must be greater than zero")

    client = CachedClient(
        args.cache_dir,
        connect_timeout=args.connect_timeout,
        read_timeout=args.read_timeout,
        retries=args.retries,
        delay=args.delay,
        refresh=args.refresh,
        user_agent="rsna-conference-scraper/1.0 (public exhibitor data export)",
    )

    assignments: list[dict[str, Any]] = []
    source_counts: dict[str, int] = {}
    try:
        for hall_id, hall_name in HALLS.items():
            payload = client.get_hall(hall_id)
            source_counts[hall_name] = len(payload) if isinstance(payload, list) else 0
            selected = parse_hall_payload(payload, hall_id)
            LOG.info(
                "%s: %d floor-plan rows, %d named booth assignments",
                hall_name,
                source_counts[hall_name],
                len(selected),
            )
            assignments.extend(selected)

        assignments.sort(
            key=lambda item: (
                clean_text(item.get("hallid")),
                float(item.get("boothsort") or 0),
                clean_text(item.get("exhname")).casefold(),
            )
        )
        if args.limit is not None:
            assignments = assignments[: args.limit]

        unique_exhibitors: dict[str, str] = {}
        for assignment in assignments:
            unique_exhibitors.setdefault(
                clean_text(assignment.get("exhid")),
                clean_text(assignment.get("exhname")),
            )

        LOG.info(
            "Fetching company details for %d unique exhibitors (%d CSV rows)",
            len(unique_exhibitors),
            len(assignments),
        )
        details: dict[str, dict[str, Any]] = {}
        failures: list[dict[str, str]] = []
        total = len(unique_exhibitors)
        for index, (exhibitor_id, company) in enumerate(unique_exhibitors.items(), 1):
            try:
                payload = client.get_exhibitor(exhibitor_id)
                details[exhibitor_id] = parse_exhibitor_payload(payload, exhibitor_id)
            except ScrapeError as exc:
                LOG.error("%s (%s): %s", company, exhibitor_id, exc)
                failures.append(
                    {"exhibitor_id": exhibitor_id, "company_name": company, "error": str(exc)}
                )
            if index % 50 == 0 or index == total:
                LOG.info("Company details: %d/%d", index, total)

        rows = make_csv_rows(assignments, details)
        partial_output = args.output.with_suffix(".partial.csv")
        output_path = partial_output if failures else args.output
        write_csv(output_path, rows)

        summary = {
            "source_floor_plan_rows": source_counts,
            "source_floor_plan_rows_total": sum(source_counts.values()),
            "named_booth_assignments": len(assignments),
            "unique_exhibitors": len(unique_exhibitors),
            "csv_rows": len(rows),
            "network_requests": client.network_requests,
            "cache_hits": client.cache_hits,
            "failed_exhibitors": failures,
            "output": str(output_path),
        }
        summary_path = args.output.with_suffix(".summary.json")
        write_summary(summary_path, summary)

        LOG.info("Wrote %d rows to %s", len(rows), output_path)
        LOG.info("Wrote run summary to %s", summary_path)
        if failures:
            LOG.error(
                "%d exhibitor requests failed; rerun to resume. The incomplete export is %s",
                len(failures),
                partial_output,
            )
            return 2
        return 0
    finally:
        client.close()


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )
    try:
        return run(args)
    except KeyboardInterrupt:
        LOG.warning("Interrupted; cached responses were retained. Rerun to resume.")
        return 130
    except ScrapeError as exc:
        LOG.error("%s", exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
