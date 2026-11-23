"""Listing classification: `group` and `cls` for every Omni listing.

`pipeline/config/classification.json` is the record. A listing that is not in the file is
classified by the rules below at build time and reported, so the file can be extended:

  1. a swap is `tradfi_swap`
  2. BTC, ETH, SOL and HYPE perpetuals are `crypto_majors`
  3. a perpetual whose annualised funding has ever printed the crypto floor 0.1095 is `other_crypto`
  4. one that has printed exactly 0 is `tradfi_perp` (TradFi perpetuals carry a 0% interest rate)
  5. one that has printed 0.05475 is a pre-IPO perpetual: `tradfi_perp`, class `pre_ipo`
  6. anything else, or a listing matching both 3 and 4, is `unclassified`

Tokenised gold (PAXG, XAUT) is crypto: it is a token and it funds at the crypto floor.
The class of a TradFi listing comes from its name (`name_class`): equity, etf, index, commodity or
fx, plus pre_ipo from rule 5. A name that does not resolve is reported and counted under the usual
class of its instrument until reviewed. In sums, `unclassified` counts inside `other_crypto` and
`crypto`.
"""
import os
import re

from .common import CONFIG, GROUPS, CLASSES, read_json

PATH = os.path.join(CONFIG, "classification.json")
MAJORS = {"BTC-PERP", "ETH-PERP", "SOL-PERP", "HYPE-PERP"}

_FUND = re.compile(r"\bETF\b|\bTrust\b|\bFund\b|ProShares|Direxion|iShares|SPDR|Invesco|Roundhill|"
                   r"KraneShares|Sprott|Grayscale|VanEck|Vanguard", re.I)
_CORP = re.compile(r"\b(Inc|Incorporated|Corp|Corporation|Company|Co|Ltd|Limited|plc|PBC|Oyj|"
                   r"Holdings?|Group)\b|N\.V\.|S\.p\.A\.|A/S|Common Stock|Depositary")
_COMMODITY = re.compile(r"\b(Gold|Silver|Copper|Platinum|Palladium|Oil|Crude|Gas|Gasoline|Cocoa|"
                        r"Coffee|Sugar|Wheat|Corn|Soybeans?|Cotton)\b", re.I)
_INDEX = re.compile(r"\bIndex\b|\bUS ?(\d+K?|MidCap|Non-Financial)\b", re.I)
_FX = re.compile(r"\b[A-Z]{3}/[A-Z]{3}\b")


def name_class(name):
    """TradFi asset class from a listing name, or None when the name does not resolve.

    A fund, trust or leveraged product is `etf`; a company share (common stock, ADR, preferred)
    is `equity`. Pre-IPO perpetuals are recognised by their fixed funding rate, not by name.
    """
    n = (name or "").replace("Swap on ", "")
    if _FUND.search(n):
        return "etf"
    if _CORP.search(n):
        return "equity"
    if _FX.search(n):
        return "fx"
    if _INDEX.search(n):
        return "index"
    if _COMMODITY.search(n):
        return "commodity"
    return None


# Where a TradFi listing is not in the file and its name does not resolve, it is counted under the
# most common class of its instrument and reported by the build until someone reviews it.
FALLBACK_CLASS = {"perp": "equity", "swap": "index"}


def by_rule(ticker, typ, name, marks):
    """(group, cls, basis) from the rules alone."""
    if typ == "swap":
        return "tradfi_swap", name_class(name) or FALLBACK_CLASS["swap"], "swap"
    if ticker in MAJORS:
        return "crypto_majors", "crypto", "major"
    floor, zero, preipo = ("floor" in marks), ("zero" in marks), ("preipo" in marks)
    if floor and not zero and not preipo:
        return "other_crypto", "crypto", "funding_floor"
    if zero and not floor and not preipo:
        return "tradfi_perp", name_class(name) or FALLBACK_CLASS["perp"], "funding_zero"
    if preipo and not floor and not zero:
        return "tradfi_perp", "pre_ipo", "pre_ipo_rate"
    return "unclassified", "crypto", "unresolved"


class Classifier:
    def __init__(self, seen):
        cfg = read_json(PATH) if os.path.exists(PATH) else {"listings": {}, "swap_financing": {}}
        self.listings = cfg.get("listings", {})
        self.fin_cfg = cfg.get("swap_financing", {})
        self.seen = seen
        self.not_in_file = {}
        self.unclassified = set()
        self.class_unresolved = set()

    def get(self, ticker, typ, name):
        e = self.listings.get(ticker)
        if e:
            group, cls, basis = e["group"], e["cls"], e.get("basis")
        else:
            marks = (self.seen.get(ticker) or {}).get("marks", [])
            group, cls, basis = by_rule(ticker, typ, name, marks)
            self.not_in_file[ticker] = (group, cls, basis)
            if group in ("tradfi_perp", "tradfi_swap") and basis != "pre_ipo_rate" and name_class(name) is None:
                self.class_unresolved.add(ticker)
        if group == "unclassified":
            self.unclassified.add(ticker)
        assert group in GROUPS or group == "unclassified", (ticker, group)
        assert cls in CLASSES, (ticker, cls)
        return group, cls, basis


def sum_group(group):
    """The key a listing's figures are summed under."""
    return "other_crypto" if group == "unclassified" else group
