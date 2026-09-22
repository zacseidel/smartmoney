from __future__ import annotations

"""
Enrich stock-page tickers with Polygon company context:
description, sector, market cap, recent news, key financials, and a price summary.

The price summary (current price, 52-week high/low) is derived for free from the
close bars fetch_prices already built from grouped-daily snapshots — no per-ticker
price call is ever made here. Polygon budget is spent only on new/stale company
profiles (description + news; details_ttl_days, ~6 months). Annual financials come
from EDGAR company facts (see fetch_financials.py), not from Polygon.

--focus outperformers restricts this API-spending work to the outperformer companies
in rankings.json (the standard, frequently-run pipeline); the default (all) is the
full refresh used by backfill.py. The bounded hedge-feature set is included in the
default so every rendered stock page can receive the same company context. Price
fields are refreshed for every page ticker either way, since that costs no API calls.

Usage:
  python src/enrich.py [--max N] [--financials-max N] [--focus all|outperformers] [--ticker TICKER]
"""

import argparse
import os
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import fetch_financials
from utils import (AGGS_CACHE, DATA_DIR, POLYGON_CACHE, Progress, PolygonClient,
                   fmt_duration, load_config, load_json, load_json_gz, save_json, setup_logging)
from stock_universe import hedge_featured_tickers, stock_page_tickers

log = setup_logging("enrich")

LEDGER_PATH = DATA_DIR / "transactions.json"
COMPANY_INFO_PATH = DATA_DIR / "company_info.json"
RANKINGS_PATH = DATA_DIR / "rankings.json"


def outperformer_tickers() -> set[str]:
    """Tickers bought by at least one out-performing member, from rankings.json.
    Empty set if rankings haven't been computed yet."""
    if not RANKINGS_PATH.exists():
        return set()
    stocks = (load_json(RANKINGS_PATH) or {}).get("stocks", {})
    return {t for t, s in stocks.items() if (s or {}).get("n_outperformer_buyers", 0) > 0}


def _has_profile(ticker: str, company_info: dict, details_ttl: int) -> bool:
    """A ticker is 'profiled' once its company_info record exists and its *details*
    cache is still fresh (details_ttl_days). Price and financials freshness are
    deliberately NOT part of this check — prices come from the close bars and
    annual figures from EDGAR — so neither triggers a full re-enrichment."""
    return (ticker in company_info
            and PolygonClient._cache_fresh(POLYGON_CACHE / f"{ticker}.json", details_ttl))


def _price_summary(ticker: str) -> dict:
    """Current price and 52-week (window) high/low from the cached close bars that
    fetch_prices built from grouped-daily snapshots. Close-based (no intraday extremes)
    and free — no API call."""
    path = AGGS_CACHE / f"{ticker}.json.gz"
    bars = load_json_gz(path) if path.exists() else []
    closes = [b["c"] for b in bars if b.get("c") is not None]  # bars are sorted ascending
    return {
        "current_price": closes[-1] if closes else None,
        "week_52_high": max(closes) if closes else None,
        "week_52_low": min(closes) if closes else None,
        "has_prices": bool(closes),
    }


def run(max_override: int | None = None, focus: str | None = None,
        ticker_overrides: set[str] | None = None,
        financials_max: int | None = None) -> None:
    cfg = load_config()
    pcfg = cfg["polygon"]
    max_new = max_override if max_override is not None else pcfg["max_enrichment_tickers"]
    details_ttl = pcfg.get("details_ttl_days", 180)
    fin_cap = financials_max
    if fin_cap is None:
        fin_cap = (cfg.get("edgar") or {}).get("financials_refresh_max", 2000)
    call_interval = 60.0 / pcfg["rate_limit_calls_per_min"]

    if not LEDGER_PATH.exists():
        log.error("No ledger at %s — run fetch_house/fetch_senate first", LEDGER_PATH)
        return
    ledger = load_json(LEDGER_PATH)
    rows = list(ledger.values())

    # Unique tickers, ranked by total disclosed dollar volume (enrich the big ones first).
    # Append the bounded hedge-feature set so hedge-only pages get profiles too.
    dollar = defaultdict(float)
    for r in rows:
        dollar[r["ticker"]] += r.get("amount_mid", 0) or 0
    page_tickers = stock_page_tickers(rows)
    hedge_only = hedge_featured_tickers() - set(dollar)
    tickers = sorted(page_tickers, key=lambda t: (dollar[t], t), reverse=True)
    if ticker_overrides:
        wanted = {ticker.upper() for ticker in ticker_overrides}
        tickers = [ticker for ticker in tickers if ticker in wanted]
    log.info("%d stock-page tickers in scope (%d hedge-only)",
             len(tickers), len(set(tickers) & hedge_only))

    api_key = os.environ.get("POLYGON_API_KEY", "")
    company_info = load_json(COMPANY_INFO_PATH) if COMPANY_INFO_PATH.exists() else {}

    # Price fields come (free) from the close bars fetch_prices built, so refresh them
    # for every already-profiled ticker each run — even ones whose profile is untouched.
    for ticker in tickers:
        if ticker in company_info:
            company_info[ticker].update(_price_summary(ticker))

    # Scope the API-spending work. The standard pipeline focuses on the out-performer
    # companies (deep dives that matter); the full refresh (default) covers everything.
    scope = tickers
    if focus == "outperformers":
        op = outperformer_tickers()
        scope = [t for t in tickers if t in op]
        log.info("Focus=outperformers — %d of %d tickers in scope", len(scope), len(tickers))
        if not scope:
            log.warning("No out-performer tickers (rankings.json missing/empty?) — nothing to enrich")
            save_json(COMPANY_INFO_PATH, company_info)
            return

    # 1) Build profiles for scoped tickers whose record is missing or whose description
    #    (details) cache has gone stale. 2 Polygon calls each (details + news).
    new_enriched = 0
    if not api_key:
        log.warning("POLYGON_API_KEY not set — keeping existing profiles (%d)", len(company_info))
    else:
        poly = PolygonClient(api_key, pcfg)
        needs_profile = [t for t in scope if not _has_profile(t, company_info, details_ttl)]
        planned = min(len(needs_profile), max_new)
        log.info("%d in scope | %d profiled & fresh | up to %d new/stale profiles via API (~%s)",
                 len(scope), len(scope) - len(needs_profile), planned,
                 fmt_duration(planned * 2 * call_interval))
        prog = Progress(planned, "profiles (API)", log)
        for ticker in needs_profile:
            if new_enriched >= max_new:
                break  # API budget spent this run; pick up the rest next run
            new_enriched += 1
            prog.step(ticker)

            details = poly.ticker_details(ticker) or {}
            news = poly.ticker_news(ticker)
            prior = company_info.get(ticker) or {}

            company_info[ticker] = {
                "ticker": ticker,
                "name": details.get("name"),
                "description": (details.get("description") or "")[:cfg["report"]["description_max_chars"]],
                "sic_code": details.get("sic_code"),
                "sic_description": details.get("sic_description"),
                "market_cap": details.get("market_cap"),
                "total_employees": details.get("total_employees"),
                "homepage_url": details.get("homepage_url"),
                "icon_url": (details.get("branding") or {}).get("icon_url"),
                **_price_summary(ticker),
                "recent_news": [
                    {"title": n.get("title"), "article_url": n.get("article_url"),
                     "publisher": (n.get("publisher") or {}).get("name"),
                     "published_utc": n.get("published_utc")}
                    for n in news
                ],
                "financials": prior.get("financials") or {},
            }

    # 2) Annual figures from EDGAR. Keeps an existing row when the filing has no
    #    headline number (ETFs, odd filers). Largest disclosed names first.
    fetch_financials.refresh(scope, company_info, fin_cap)

    save_json(COMPANY_INFO_PATH, company_info)
    log.info("Enriched %d new/stale profiles; company_info has %d tickers",
             new_enriched, len(company_info))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--max", type=int, default=None, help="override max new tickers this run")
    ap.add_argument("--financials-max", type=int, default=None,
                    help="override max EDGAR financials fetches this run "
                         "(default: edgar.financials_refresh_max)")
    ap.add_argument("--focus", choices=["all", "outperformers"], default="all",
                    help="'outperformers' restricts API work to out-performer companies (standard pipeline)")
    ap.add_argument("--ticker", action="append", default=[],
                    help="enrich only this stock-page ticker; may be repeated")
    args = ap.parse_args()
    run(args.max, focus=args.focus, ticker_overrides=set(args.ticker),
        financials_max=args.financials_max)


if __name__ == "__main__":
    main()
