"""Fail-closed live entry universe.

Mudrex's futures response does not currently document a dependable asset-class field. Explicit crypto metadata
is accepted; explicit non-crypto metadata is rejected. Rows without metadata are accepted only when the symbol
is in the small reviewed crypto allowlist. Unknown symbols never become live entries from ticker heuristics.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
import math

MIN_USD_VOLUME_24H = 12_000_000.0
REVIEWED_CRYPTO = frozenset({"BTC", "ETH", "SOL", "XRP", "DOGE", "BNB", "ADA", "AVAX", "LINK", "LTC", "SUI", "TRX"})
CRYPTO_CLASSES = {"crypto", "cryptocurrency", "digital_asset", "digital asset"}
NON_CRYPTO_CLASSES = {"stock", "equity", "commodity", "forex", "fx", "index"}


@dataclass(frozen=True)
class Selection:
    rows: tuple[dict, ...]
    rejected: dict[str, str]

    @property
    def coins(self):
        return tuple(r["symbol"].removesuffix("USDT") for r in self.rows)


def _class(row):
    for key in ("asset_class", "asset_type", "category", "underlying_type"):
        if row.get(key) not in (None, ""):
            return str(row[key]).strip().lower()
    return None


def select_rows(rows, min_usd_volume_24h=MIN_USD_VOLUME_24H):
    accepted, rejected = [], {}
    for row in rows or ():
        symbol = str(row.get("symbol") or "")
        coin = symbol.removesuffix("USDT") if symbol.endswith("USDT") else symbol or "<missing>"
        cls = _class(row)
        if not symbol.endswith("USDT") or not coin:
            rejected[coin] = "not a USDT futures symbol"
            continue
        if cls in NON_CRYPTO_CLASSES or (cls is not None and cls not in CRYPTO_CLASSES):
            rejected[coin] = f"asset class {cls!r} is not verified crypto"
            continue
        if cls is None and coin not in REVIEWED_CRYPTO:
            rejected[coin] = "asset class missing and symbol is not in reviewed crypto allowlist"
            continue
        try:
            nums = [float(row[k]) for k in ("price", "quantity_step", "min_contract", "min_notional_value",
                                             "price_step", "max_leverage")]
            volume = float(row["volume"]) * nums[0]
        except (KeyError, TypeError, ValueError):
            rejected[coin] = "missing/non-numeric contract specification"
            continue
        if not all(math.isfinite(x) and x > 0 for x in nums) or not math.isfinite(volume):
            rejected[coin] = "non-finite/non-positive contract specification"
            continue
        if nums[-1] < 1:
            rejected[coin] = "maximum leverage below 1x"
            continue
        if volume < min_usd_volume_24h:
            rejected[coin] = f"24h notional volume below {min_usd_volume_24h:g} USDT"
            continue
        accepted.append(row)
    return Selection(tuple(accepted), rejected)


def archive(con, cycle, selection, created_at):
    """Persist point-in-time membership; reruns of a cycle retain the first immutable snapshot."""
    payload = json.dumps({"coins": selection.coins, "rejected": selection.rejected}, sort_keys=True)
    con.execute("INSERT OR IGNORE INTO universe_snapshots(cycle,created_at,payload) VALUES(?,?,?)",
                (cycle, int(created_at), payload))
    return json.loads(con.execute("SELECT payload FROM universe_snapshots WHERE cycle=?", (cycle,)).fetchone()[0])


def replay(selection, snapshot):
    """Apply the cycle's first membership to fresh specifications without admitting later arrivals."""
    by_coin = {r["symbol"].removesuffix("USDT"): r for r in selection.rows}
    frozen_rows = tuple(by_coin[c] for c in snapshot.get("coins", ()) if c in by_coin)
    rejected = dict(selection.rejected)
    for coin in snapshot.get("coins", ()):
        if coin not in by_coin:
            rejected[coin] = "archived cycle member is absent from the current verified listing"
    return Selection(frozen_rows, rejected)
