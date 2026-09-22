from __future__ import annotations

"""
Annual key financials for stock pages, from SEC EDGAR company facts.

Polygon's financials endpoint is a resale of the same 10-K XBRL, paced at the
free-tier 5 calls/min. EDGAR serves it directly at ~10 requests/sec with no API
key. One company-facts document carries the whole history; we keep the latest
annual figure for each line the stock page shows.

Tickers with no CIK (ETFs, many funds) are skipped. A filer whose facts do not
yield a headline number keeps whatever financials were already on the page.

Usage:
  python src/fetch_financials.py [--max N] [--ticker TICKER]
"""

import argparse
import sys
import time
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from utils import (DATA_DIR, EDGAR_CACHE, PolygonClient, Progress, TICKER_ALIASES,
                   fmt_duration, http_session, load_config, load_json, save_json,
                   setup_logging)

log = setup_logging("financials")

COMPANY_INFO_PATH = DATA_DIR / "company_info.json"
TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
FACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json"
ANNUAL_FORMS = {"10-K", "10-K/A", "20-F", "20-F/A", "40-F", "40-F/A"}
HEADLINE = ("revenues", "net_income", "assets")

# Earlier tags win when two concepts share an annual period. Banks often have no
# "Revenues" concept; interest income is the fallback for that line only.
TAGS = {
    "revenues": (
        "Revenues",
        "RevenueFromContractWithCustomerExcludingAssessedTax",
        "RevenueFromContractWithCustomerIncludingAssessedTax",
        "SalesRevenueNet",
        "RevenuesNetOfInterestExpense",
        "InterestAndDividendIncomeOperating",
    ),
    "net_income": ("NetIncomeLoss", "ProfitLoss"),
    "operating_income": ("OperatingIncomeLoss", "ProfitLossFromOperatingActivities"),
    "diluted_eps": ("EarningsPerShareDiluted", "DilutedEarningsLossPerShare"),
    "assets": ("Assets",),
    "liabilities": ("Liabilities",),
    "equity": (
        "StockholdersEquity",
        "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest",
        "Equity",
        "EquityAttributableToOwnersOfParent",
    ),
}
MONEY_UNITS = {"USD"}
EPS_UNITS = {"USD/shares"}
INSTANT_FIELDS = {"assets", "liabilities", "equity"}


def has_statement(summary: dict | None) -> bool:
    return bool(summary) and any(summary.get(k) is not None for k in HEADLINE)


def _period_days(row: dict) -> int | None:
    start, end = row.get("start"), row.get("end")
    if not start or not end:
        return None
    try:
        return (date.fromisoformat(end) - date.fromisoformat(start)).days
    except ValueError:
        return None


def _is_annual(row: dict, instant: bool) -> bool:
    form = (row.get("form") or "").upper()
    if form not in ANNUAL_FORMS or (row.get("fp") or "").upper() != "FY":
        return False
    if not row.get("end"):
        return False
    days = _period_days(row)
    if instant:
        return days is None
    return days is not None and 300 <= days <= 380


def _best_fact(facts: dict, tags: tuple[str, ...], units: set[str], instant: bool) -> dict | None:
    best_key = None
    best = None
    taxonomies = facts or {}
    for priority, tag in enumerate(tags):
        for taxonomy in ("us-gaap", "ifrs-full"):
            concept = (taxonomies.get(taxonomy) or {}).get(tag) or {}
            for unit, rows in (concept.get("units") or {}).items():
                if unit not in units:
                    continue
                for row in rows or []:
                    if not _is_annual(row, instant):
                        continue
                    # Latest period, then the preferred tag, then the latest amendment.
                    key = (row.get("end") or "", -priority, row.get("filed") or "")
                    if best_key is None or key > best_key:
                        best_key, best = key, row
    return best


def annual_summary(facts: dict) -> dict:
    """Latest annual 10-K/20-F lines from a company-facts payload. {} when none match."""
    chosen = {}
    for field, tags in TAGS.items():
        units = EPS_UNITS if field == "diluted_eps" else MONEY_UNITS
        chosen[field] = _best_fact(facts, tags, units, field in INSTANT_FIELDS)
    if not any(chosen.values()):
        return {}
    dated = [row for row in chosen.values() if row and row.get("end")]
    anchor = max(dated, key=lambda row: (row.get("end") or "", row.get("filed") or ""))
    summary = {
        "fiscal_period": anchor.get("fp") or "FY",
        "fiscal_year": anchor.get("fy") or int(anchor["end"][:4]),
        "source": "edgar",
    }
    for field, row in chosen.items():
        summary[field] = row.get("val") if row else None
    return summary


def _ticker_rows(payload) -> list[dict]:
    if isinstance(payload, dict):
        return [row for row in payload.values() if isinstance(row, dict) and row.get("ticker")]
    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, dict) and row.get("ticker")]
    return []


def cik_by_ticker(payload) -> dict[str, int]:
    """SEC ticker (BRK-B) -> CIK. Exact symbol only; callers try class-share aliases."""
    out = {}
    for row in _ticker_rows(payload):
        try:
            out[str(row["ticker"]).upper()] = int(row["cik_str"])
        except (TypeError, ValueError):
            continue
    return out


def resolve_cik(ticker: str, by_ticker: dict[str, int]) -> int | None:
    symbol = (ticker or "").upper()
    candidates = [symbol]
    dotted = TICKER_ALIASES.get(symbol)
    if dotted:
        candidates.append(dotted.upper())
        candidates.append(dotted.upper().replace(".", "-"))
    for candidate in candidates:
        cik = by_ticker.get(candidate)
        if cik is not None:
            return cik
    return None


def _cache_path(ticker: str) -> Path:
    return EDGAR_CACHE / "company_financials" / f"{ticker.upper()}.json"


def _fresh(path: Path, ttl_days: int) -> bool:
    return PolygonClient._cache_fresh(path, ttl_days)


class _Pace:
    def __init__(self, per_sec: float):
        self._interval = 1.0 / per_sec if per_sec else 0.0
        self._last = 0.0

    def wait(self) -> None:
        if self._last and self._interval:
            delay = self._interval - (time.monotonic() - self._last)
            if delay > 0:
                time.sleep(delay)
        self._last = time.monotonic()


def _get_json(session, url: str, pace: _Pace, retries: int = 4) -> dict | None:
    """GET JSON. None on 404. Raises after repeated transient failures."""
    import requests
    last = None
    for attempt in range(retries):
        pace.wait()
        try:
            resp = session.get(url, timeout=60)
        except requests.RequestException as e:
            last = e
            time.sleep(2 * (attempt + 1))
            continue
        if resp.status_code == 404:
            return None
        if resp.status_code in (429, 500, 502, 503, 504):
            last = f"HTTP {resp.status_code}"
            time.sleep(2 * (attempt + 1))
            continue
        resp.raise_for_status()
        return resp.json()
    raise RuntimeError(f"GET failed after {retries} attempts ({last}): {url}")


def refresh(tickers: list[str], company_info: dict, max_n: int | None = None) -> int:
    """Fill company_info financials from EDGAR. Returns how many rows were written.

    `tickers` is in priority order (largest disclosed dollars first). Only tickers
    that already have a company_info row are touched, so this pass does not invent
    profiles. Network fetches stop at max_n; fresh cache is applied either way.
    """
    cfg = load_config().get("edgar") or {}
    ttl = cfg.get("facts_ttl_days", 30)
    cap = max_n if max_n is not None else cfg.get("financials_refresh_max", 2000)
    per_sec = cfg.get("rate_limit_per_sec", 8)
    tickers_ttl = cfg.get("tickers_ttl_days", 7)

    wanted = [t for t in tickers if t in company_info]
    if not wanted:
        log.info("Financials: no profiled tickers to update")
        return 0

    by_ticker = cik_by_ticker(_load_ticker_map(tickers_ttl))
    pace = _Pace(per_sec)
    session = http_session()

    no_cik = 0
    fresh = 0
    applied = 0
    stale: list[tuple[str, int]] = []
    for ticker in wanted:
        cik = resolve_cik(ticker, by_ticker)
        if cik is None:
            no_cik += 1
            continue
        path = _cache_path(ticker)
        if _fresh(path, ttl):
            fresh += 1
            if _apply(company_info, ticker, load_json(path)):
                applied += 1
            continue
        stale.append((ticker, cik))

    planned = stale[:max(cap, 0)]
    deferred = len(stale) - len(planned)
    log.info(
        "Financials (EDGAR): %d profiled | %d no CIK | %d cached | fetching %d (~%s) | %d deferred",
        len(wanted), no_cik, fresh, len(planned),
        fmt_duration(len(planned) / per_sec if per_sec else 0), deferred,
    )
    prog = Progress(len(planned), "financials (EDGAR)", log, every=max(1, len(planned) // 20 or 1))
    fetched = 0
    for ticker, cik in planned:
        prog.step(ticker)
        try:
            payload = _get_json(session, FACTS_URL.format(cik=cik), pace)
        except Exception as e:
            log.warning("financials failed for %s: %s", ticker, e)
            continue
        summary = annual_summary((payload or {}).get("facts") or {})
        if has_statement(summary):
            summary["cik"] = cik
            record = summary
        else:
            record = {"ok": False, "cik": cik}
        save_json(_cache_path(ticker), record)
        if _apply(company_info, ticker, record):
            applied += 1
        fetched += 1
    prog_done = len(planned)
    log.info("Financials (EDGAR): applied %d, fetched %d of %d planned",
             applied, fetched, prog_done)
    return applied


def _apply(company_info: dict, ticker: str, record: dict) -> bool:
    if not has_statement(record):
        return False
    company_info[ticker]["financials"] = {
        "fiscal_period": record.get("fiscal_period"),
        "fiscal_year": record.get("fiscal_year"),
        "revenues": record.get("revenues"),
        "net_income": record.get("net_income"),
        "operating_income": record.get("operating_income"),
        "diluted_eps": record.get("diluted_eps"),
        "assets": record.get("assets"),
        "liabilities": record.get("liabilities"),
        "equity": record.get("equity"),
        "source": "edgar",
    }
    return True


def _load_ticker_map(ttl_days: int) -> dict:
    path = EDGAR_CACHE / "company_tickers.json"
    if _fresh(path, ttl_days):
        return load_json(path)
    log.info("Downloading SEC ticker → CIK map")
    resp = http_session().get(TICKERS_URL, timeout=60)
    resp.raise_for_status()
    payload = resp.json()
    save_json(path, payload)
    return payload


def run_standalone(max_n: int | None, tickers: set[str]) -> None:
    if not COMPANY_INFO_PATH.exists():
        log.error("No company_info at %s", COMPANY_INFO_PATH)
        return
    company_info = load_json(COMPANY_INFO_PATH)
    order = sorted(tickers) if tickers else sorted(company_info)
    if tickers:
        order = [t for t in order if t in company_info]
    refresh(order, company_info, max_n)
    save_json(COMPANY_INFO_PATH, company_info)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--max", type=int, default=None, help="max EDGAR fetches this run")
    ap.add_argument("--ticker", action="append", default=[], help="limit to this ticker; repeatable")
    args = ap.parse_args()
    run_standalone(args.max, {t.upper() for t in args.ticker})


if __name__ == "__main__":
    main()
