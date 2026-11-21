"""Check the data and the files GitHub Pages will serve before making a release."""

from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlsplit

from chain_history import validate as validate_chain_history
from execution_study import validate as validate_execution_study
from market_classifications import validate as validate_market_classifications
from markets import load_partition, read_existing
from supplement import CATALOG, WALLETS, USDC, public_research, read_json


ROOT = Path(__file__).resolve().parents[1]
PUBLIC = ROOT / "public"
DATA = PUBLIC / "data"


class Assets(HTMLParser):
    def __init__(self):
        super().__init__()
        self.urls = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag in {"script", "img", "link"}:
            self.urls.append(attrs.get("src") or attrs.get("href", ""))


def check():
    index = PUBLIC / "index.html"
    parser = Assets()
    parser.feed(index.read_text())
    for asset in parser.urls:
        if not asset or urlsplit(asset).scheme:
            continue
        target = (PUBLIC / asset).resolve()
        if not target.is_relative_to(PUBLIC) or not target.is_file():
            raise ValueError("Missing public asset: " + asset)
    entries = read_existing(DATA)
    if not entries:
        raise ValueError("No validated Omni observations")
    for item in entries:
        if item.get("partition_format") in {"full", "compact"}:
            snap = load_partition(DATA, item)
            if snap["totals"] != item["totals"]:
                raise ValueError("Daily totals differ between detail and history index")
    validate_execution_study(read_json(DATA / "execution-study.json"))
    validate_market_classifications(read_json(DATA / "market-classifications.json"))
    supplement = read_json(DATA / "supplement.json")
    validate_chain_history(read_json(DATA / "chain-history.json"))
    if supplement["research"] != public_research(read_json(CATALOG)):
        raise ValueError("Public research differs from validated catalogue")
    chain = supplement["chain"]
    if chain["chain_id"] != 42161 or chain["usdc_contract"].lower() != USDC:
        raise ValueError("Unexpected asset or chain")
    accounts = {a["id"]: a["address"].lower() for a in chain["accounts"]}
    if accounts != {key: pair[1] for key, pair in WALLETS.items()}:
        raise ValueError("Unexpected wallet registry")
    token = supplement["token"]
    if token["status"] != "verified" and any(token.get(k) is not None for k in
            ("price_usd", "market_cap_usd", "circulating_supply", "fdv_usd")):
        raise ValueError("Token market data requires verified identity")
    forbidden = (b"/Users/", b"app.notion.com/", b"notion.so/", b"-----BEGIN PRIVATE KEY",
                 b"github_pat_", b"ghp_", b"x-access-token:", b"api_key=", b"SLACK_BOT_TOKEN")
    files = [p for p in PUBLIC.rglob("*") if p.is_file()]
    for path in files:
        if path.is_symlink() or path.name.startswith(".") and path.name != ".nojekyll":
            raise ValueError(f"Unexpected file in the public site: {path.name}")
        if path.suffix in {".html", ".json", ".css", ".js", ".md"}:
            if any(marker in path.read_bytes() for marker in forbidden):
                raise ValueError(f"Private reference in {path.name}")
    byte_count = sum(p.stat().st_size for p in files)
    if byte_count > 350_000_000:
        raise ValueError("Site exceeded its free hosting budget")
    print(f"Validated {len(entries)} market dates, {len(files)} public files, {byte_count:,} bytes")


if __name__ == "__main__":
    check()
