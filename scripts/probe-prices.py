"""Small Actions smoke check; prints only normalized public market facts."""
from __future__ import annotations

import concurrent.futures
import json

from providers import DataError, PublicProvider


def probe(item):
    market, code = item
    result = {"market": market, "stockCode": code, "start": "2026-04-01", "end": "2026-06-30"}
    try:
        series = PublicProvider().price_bars(code, market, result["start"], result["end"])
        rows = series["rows"]
        result.update({"status": "ok", "provider": series["provider"], "currency": series["currency"],
                       "priceBasis": series["priceBasis"], "count": len(rows),
                       "firstDate": rows[0]["date"], "lastDate": rows[-1]["date"],
                       "low": min(row["low"] for row in rows), "high": max(row["high"] for row in rows),
                       "providerErrors": series.get("providerErrors", [])})
    except DataError as error:
        result.update({"status": "failed", "errorCode": error.code,
                       "providerErrors": getattr(error, "provider_errors", [])})
    except Exception:
        result.update({"status": "failed", "errorCode": "PRICE_PROBE_FAILED", "providerErrors": []})
    return result


def main():
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        for result in pool.map(probe, [("CN", "600519"), ("HK", "00700")]):
            print(json.dumps(result, ensure_ascii=False, allow_nan=False))


if __name__ == "__main__":
    # This diagnostic is deliberately non-blocking for the collection job.
    try:
        main()
    except Exception:
        print(json.dumps({"status": "failed", "errorCode": "PRICE_PROBE_FAILED"}))
