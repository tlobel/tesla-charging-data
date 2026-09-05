#!/usr/bin/env python3
"""Fail-closed price updater for the Tesla Charging family app."""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import re
import sys
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import date, datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Iterable
from zoneinfo import ZoneInfo

import pdfplumber


EON_INDEX_URL = "https://www.eon-drive.cz/pro-ridice/"
PRE_CURRENT_PRICE_URL = (
    "https://www.pre.cz/Files/emobilita/cenik/"
    "cenik-dobijeni-v-siti-pre-point-aktualne-platny/"
)
CEZ_INDEX_URL = "https://futurego.cz/cs/verejne-dobijeni/cenik"
USER_AGENT = "TeslaChargingPriceRobot/1.0 (+family price verification)"
PRAGUE = ZoneInfo("Europe/Prague")


class PriceRobotError(RuntimeError):
    pass


class LinkParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.links: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() != "a":
            return
        href = dict(attrs).get("href")
        if href:
            self.links.append(href)


@dataclass(frozen=True)
class ParsedProvider:
    provider_id: str
    document_url: str
    effective_from: date
    document_sha256: str
    rules: list[dict[str, object]]


def fetch(url: str, timeout: int = 30) -> tuple[bytes, str]:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        data = response.read(8_000_001)
        if len(data) > 8_000_000:
            raise PriceRobotError(f"Dokument je neočekávaně velký: {url}")
        return data, response.geturl()


def links_from_page(url: str) -> list[str]:
    data, final_url = fetch(url)
    parser = LinkParser()
    parser.feed(data.decode("utf-8", errors="replace"))
    return list(dict.fromkeys(urllib.parse.urljoin(final_url, link) for link in parser.links))


def pdf_content(data: bytes) -> tuple[str, list[list[list[str | None]]]]:
    if not data.startswith(b"%PDF"):
        raise PriceRobotError("Stažený ceník není PDF.")
    try:
        with pdfplumber.open(io.BytesIO(data)) as pdf:
            texts = [
                page.extract_text(x_tolerance=2, y_tolerance=3) or ""
                for page in pdf.pages
            ]
            tables: list[list[list[str | None]]] = []
            for page in pdf.pages:
                tables.extend(page.extract_tables() or [])
    except Exception as error:  # pdfplumber exposes several backend exception types
        raise PriceRobotError(f"PDF se nepodařilo bezpečně přečíst: {error}") from error
    text = "\n".join(texts)
    if len(text.strip()) < 100:
        raise PriceRobotError("PDF neobsahuje dostatek čitelného textu.")
    return text, tables


def decimal_czk(value: str) -> float:
    match = re.search(r"(\d{1,3}(?:[ .]\d{3})*[,.]\d{1,2}|\d{1,3})", value)
    if not match:
        raise PriceRobotError(f"Částku nelze přečíst: {value!r}")
    normalized = match.group(1).replace(" ", "").replace(".", "").replace(",", ".")
    number = float(normalized)
    if not (0 <= number <= 100):
        raise PriceRobotError(f"Částka je mimo bezpečný rozsah: {number}")
    return number


def parse_czech_date(day: str, month: str, year: str) -> date:
    try:
        return date(int(year), int(month), int(day))
    except ValueError as error:
        raise PriceRobotError("Ceník obsahuje neplatné datum účinnosti.") from error


def effective_date(text: str, patterns: Iterable[str]) -> date:
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match:
            return parse_czech_date(*match.groups())
    raise PriceRobotError("V ceníku chybí datum účinnosti.")


def rule(
    *,
    rule_id: str,
    provider_id: str,
    tariff_name: str,
    connector_kind: str,
    maximum_power_kw: float,
    day_price: float,
    night_price: float | None,
    free_minutes: int,
    fee_per_minute: float,
    night_fee_waived: bool,
) -> dict[str, object]:
    return {
        "id": rule_id,
        "providerID": provider_id,
        "tariffName": tariff_name,
        "connectorKind": connector_kind,
        "maximumPowerKW": maximum_power_kw,
        "dayPriceCZKPerKWh": day_price,
        "nightPriceCZKPerKWh": night_price,
        "occupancyFreeMinutes": free_minutes,
        "occupancyFeeCZKPerMinute": fee_per_minute,
        "nightOccupancyFeeWaived": night_fee_waived,
    }


def eon_group_price(segment: str, group: int) -> float:
    labels = {
        1: r"Skupina\s+1\s+\(až\s+100\s*kW\)",
        2: r"Skupina\s+2\s+\(101\s*-\s*200\s*kW\)",
        3: r"Skupina\s+3\s+\(201\s*-?\s*400\s*kW\)",
    }
    match = re.search(
        labels[group] + r"\s+\d+[,.]\d+\s+(\d+[,.]\d+)",
        segment,
        flags=re.IGNORECASE,
    )
    if not match:
        raise PriceRobotError(f"E.ON: chybí cena s DPH pro skupinu {group}.")
    return decimal_czk(match.group(1))


def parse_eon(data: bytes, document_url: str) -> ParsedProvider:
    text, _ = pdf_content(data)
    valid_from = effective_date(
        text,
        [r"Ceník\s+platný\s+od\s+(\d{1,2})\.(\d{1,2})\.(\d{4})"],
    )
    if "Registrovaný zákazník služby E.ON Drive CZ" not in text:
        raise PriceRobotError("E.ON: nejde o ceník registrovaného zákazníka.")
    try:
        day_segment, remainder = text.split("Noční tarif", 1)
        night_segment = remainder.split("Objem volných minut", 1)[0]
    except ValueError as error:
        raise PriceRobotError("E.ON: nelze oddělit denní a noční tarif.") from error

    day_prices = {group: eon_group_price(day_segment, group) for group in (1, 2, 3)}
    night_prices = {group: eon_group_price(night_segment, group) for group in (1, 2, 3)}

    free_match = re.search(
        r"480\s*\(AC\s+konektory\)\s*/\s*120\s*\(DC\s+konektory\)", text
    )
    group_two_free = re.search(r"Skupina\s+2\s+\(101\s*-\s*200\s*kW\)\s+60", text)
    group_three_free = re.search(r"Skupina\s+3\s+\(201\s*-?\s*400\s*kW\)\s+30", text)
    fee_section = text.split("Poplatek za minuty po", 1)[-1]
    required_fee_fragments = (
        "Denní tarif",
        "Noční tarif",
        "1,65 2,00",
        "0,00 0,00",
    )
    if not free_match or not group_two_free or not group_three_free:
        raise PriceRobotError("E.ON: nelze bezpečně ověřit volné minuty.")
    if not all(fragment in fee_section for fragment in required_fee_fragments):
        raise PriceRobotError("E.ON: nelze bezpečně ověřit poplatek za obsazení.")

    rules = [
        rule(
            rule_id="eon-registered-ac-group-1",
            provider_id="eon",
            tariff_name="E.ON Drive · Stálý plat",
            connector_kind="ac",
            maximum_power_kw=100,
            day_price=day_prices[1],
            night_price=night_prices[1],
            free_minutes=480,
            fee_per_minute=2,
            night_fee_waived=True,
        ),
        rule(
            rule_id="eon-registered-ccs-group-1",
            provider_id="eon",
            tariff_name="E.ON Drive · Stálý plat",
            connector_kind="ccs",
            maximum_power_kw=100,
            day_price=day_prices[1],
            night_price=night_prices[1],
            free_minutes=120,
            fee_per_minute=2,
            night_fee_waived=True,
        ),
        rule(
            rule_id="eon-registered-ccs-group-2",
            provider_id="eon",
            tariff_name="E.ON Drive · Stálý plat",
            connector_kind="ccs",
            maximum_power_kw=200,
            day_price=day_prices[2],
            night_price=night_prices[2],
            free_minutes=60,
            fee_per_minute=2,
            night_fee_waived=False,
        ),
        rule(
            rule_id="eon-registered-ccs-group-3",
            provider_id="eon",
            tariff_name="E.ON Drive · Stálý plat",
            connector_kind="ccs",
            maximum_power_kw=400,
            day_price=day_prices[3],
            night_price=night_prices[3],
            free_minutes=30,
            fee_per_minute=2,
            night_fee_waived=False,
        ),
    ]
    return ParsedProvider(
        provider_id="eon",
        document_url=document_url,
        effective_from=valid_from,
        document_sha256=hashlib.sha256(data).hexdigest(),
        rules=rules,
    )


def table_header_key(table: list[list[str | None]]) -> str:
    if not table or not table[0]:
        return ""
    return re.sub(r"\s+", "", table[0][0] or "").upper()


def parse_pre(data: bytes, document_url: str) -> ParsedProvider:
    text, tables = pdf_content(data)
    valid_from = effective_date(
        text,
        [r"platný\s+od\s+(\d{1,2})\.\s*(\d{1,2})\.\s*(\d{4})"],
    )
    start_tables = [table for table in tables if table_header_key(table) == "TRATSEGRAHCERP"]
    if len(start_tables) != 1:
        raise PriceRobotError("PRE: tarif PRE Charge START nebyl nalezen právě jednou.")

    table = start_tables[0]
    if len(table) != 4:
        raise PriceRobotError("PRE: tabulka START nemá očekávané tři typy konektoru.")
    parsed_rows: dict[str, tuple[float, float, int]] = {}
    for row in table[1:]:
        if len(row) < 6 or not row[1] or not row[3] or not row[4] or not row[5]:
            raise PriceRobotError("PRE: neúplný řádek v tabulce START.")
        connector = row[1].strip().upper()
        parsed_rows[connector] = (
            decimal_czk(row[3]),
            decimal_czk(row[4]),
            int(row[5].strip()),
        )
    if set(parsed_rows) != {"AC", "DC", "UFC"}:
        raise PriceRobotError("PRE: tabulka START nemá AC, DC a UFC.")

    limits = {"AC": 49, "DC": 149, "UFC": 1_000}
    kinds = {"AC": "ac", "DC": "ccs", "UFC": "ccs"}
    rules = []
    for connector in ("AC", "DC", "UFC"):
        price, fee, free_minutes = parsed_rows[connector]
        rules.append(
            rule(
                rule_id=f"pre-start-{connector.lower()}",
                provider_id="pre",
                tariff_name="PRE Charge START",
                connector_kind=kinds[connector],
                maximum_power_kw=limits[connector],
                day_price=price,
                night_price=None,
                free_minutes=free_minutes,
                fee_per_minute=fee,
                night_fee_waived=False,
            )
        )
    return ParsedProvider(
        provider_id="pre",
        document_url=document_url,
        effective_from=valid_from,
        document_sha256=hashlib.sha256(data).hexdigest(),
        rules=rules,
    )


def parse_cez_band(section: str, pattern: str, has_monthly_fee: bool = False) -> tuple[float, int, float]:
    monthly = r"\s+\d+[,.]\d+" if has_monthly_fee else ""
    match = re.search(
        pattern
        + monthly
        + r"\s+(\d+[,.]\d+)\s+od\s+(\d+)\.\s*min\.\*?\s+(\d+[,.]\d+)",
        section,
        flags=re.IGNORECASE,
    )
    if not match:
        raise PriceRobotError("ČEZ: nelze přečíst cenové pásmo tarifu Basic.")
    price = decimal_czk(match.group(1))
    first_paid_minute = int(match.group(2))
    fee = decimal_czk(match.group(3))
    if first_paid_minute <= 1:
        raise PriceRobotError("ČEZ: neplatný limit obsazení.")
    return price, first_paid_minute - 1, fee


def parse_cez(data: bytes, document_url: str) -> ParsedProvider:
    text, _ = pdf_content(data)
    valid_from = effective_date(
        text,
        [r"Účinnost:\s*od\s+(\d{1,2})\.\s*(\d{1,2})\.\s*(\d{4})"],
    )
    try:
        own_network = text.split("I. Ceník Vlastních a Partnerských DS", 1)[1]
        basic_section = own_network.split("Standard", 1)[0]
    except (IndexError, ValueError) as error:
        raise PriceRobotError("ČEZ: nelze oddělit tarif Basic.") from error

    low = parse_cez_band(basic_section, r"≤\s*49\s*kW")
    medium = parse_cez_band(basic_section, r"Basic\s+≤\s*149\s*kW", has_monthly_fee=True)
    high = parse_cez_band(basic_section, r"≥\s*150\s*kW")
    rules = [
        rule(
            rule_id="cez-basic-ac-49",
            provider_id="cez",
            tariff_name="futurego BASIC",
            connector_kind="ac",
            maximum_power_kw=49,
            day_price=low[0],
            night_price=None,
            free_minutes=low[1],
            fee_per_minute=low[2],
            night_fee_waived=False,
        ),
        rule(
            rule_id="cez-basic-ccs-49",
            provider_id="cez",
            tariff_name="futurego BASIC",
            connector_kind="ccs",
            maximum_power_kw=49,
            day_price=low[0],
            night_price=None,
            free_minutes=low[1],
            fee_per_minute=low[2],
            night_fee_waived=False,
        ),
        rule(
            rule_id="cez-basic-ccs-149",
            provider_id="cez",
            tariff_name="futurego BASIC",
            connector_kind="ccs",
            maximum_power_kw=149,
            day_price=medium[0],
            night_price=None,
            free_minutes=medium[1],
            fee_per_minute=medium[2],
            night_fee_waived=False,
        ),
        rule(
            rule_id="cez-basic-ccs-1000",
            provider_id="cez",
            tariff_name="futurego BASIC",
            connector_kind="ccs",
            maximum_power_kw=1_000,
            day_price=high[0],
            night_price=None,
            free_minutes=high[1],
            fee_per_minute=high[2],
            night_fee_waived=False,
        ),
    ]
    return ParsedProvider(
        provider_id="cez",
        document_url=document_url,
        effective_from=valid_from,
        document_sha256=hashlib.sha256(data).hexdigest(),
        rules=rules,
    )


def current_document(
    links: Iterable[str],
    parser,
    today: date,
    provider: str,
) -> ParsedProvider:
    parsed: list[ParsedProvider] = []
    errors: list[str] = []
    for url in list(dict.fromkeys(links))[:12]:
        try:
            data, final_url = fetch(url)
            candidate = parser(data, final_url)
            if candidate.effective_from <= today:
                parsed.append(candidate)
        except Exception as error:
            errors.append(f"{url}: {error}")
    if not parsed:
        detail = " | ".join(errors[-3:])
        raise PriceRobotError(f"{provider}: nebyl nalezen platný ceník. {detail}")
    return max(parsed, key=lambda candidate: candidate.effective_from)


def collect_prices(today: date | None = None) -> dict[str, object]:
    today = today or datetime.now(PRAGUE).date()

    eon_links = [
        link
        for link in links_from_page(EON_INDEX_URL)
        if re.search(r"cenik-dobijeni[_\-.].*e[.]?on-drive[.]pdf$", link, re.IGNORECASE)
    ]
    eon = current_document(eon_links, parse_eon, today, "E.ON")

    pre_data, pre_url = fetch(PRE_CURRENT_PRICE_URL)
    pre = parse_pre(pre_data, pre_url)
    if pre.effective_from > today:
        raise PriceRobotError("PRE: aktuální odkaz zatím míří na budoucí ceník.")

    cez_links = [
        link
        for link in links_from_page(CEZ_INDEX_URL)
        if "cenik_elektromobilita" in link.lower()
        and "jednorazove" not in link.lower()
        and link.lower().endswith(".pdf")
    ]
    cez = current_document(cez_links, parse_cez, today, "ČEZ")

    providers = [eon, pre, cez]
    if {provider.provider_id for provider in providers} != {"eon", "pre", "cez"}:
        raise PriceRobotError("Výstup neobsahuje právě E.ON, PRE a ČEZ.")

    now = datetime.now(timezone.utc).replace(microsecond=0)
    return {
        "schemaVersion": 2,
        "updatedAt": now.isoformat().replace("+00:00", "Z"),
        "verifiedAt": today.isoformat(),
        "sources": [
            {
                "providerID": provider.provider_id,
                "documentURL": provider.document_url,
                "effectiveFrom": provider.effective_from.isoformat(),
                "documentSHA256": provider.document_sha256,
            }
            for provider in providers
        ],
        "tariffs": [rule for provider in providers for rule in provider.rules],
    }


def validate_feed(feed: dict[str, object]) -> None:
    if feed.get("schemaVersion") != 2:
        raise PriceRobotError("Výstup má neplatnou verzi schématu.")
    sources = feed.get("sources")
    tariffs = feed.get("tariffs")
    if not isinstance(sources, list) or not isinstance(tariffs, list):
        raise PriceRobotError("Výstup nemá zdroje nebo tarifní pravidla.")
    if {source.get("providerID") for source in sources if isinstance(source, dict)} != {
        "eon",
        "pre",
        "cez",
    }:
        raise PriceRobotError("Výstup nemá všechny tři zdroje.")
    rule_ids = [item.get("id") for item in tariffs if isinstance(item, dict)]
    if len(rule_ids) != len(tariffs) or len(set(rule_ids)) != len(rule_ids):
        raise PriceRobotError("Tarifní pravidla mají duplicitní nebo chybějící ID.")
    if len(tariffs) != 11:
        raise PriceRobotError("Výstup nemá očekávaných 11 tarifních pravidel.")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=Path("prices.json"))
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args(argv)

    try:
        feed = collect_prices()
        validate_feed(feed)
        if args.check:
            print(
                f"Ověřeno {len(feed['tariffs'])} tarifních pravidel ze tří zdrojů; "
                f"platnost {feed['verifiedAt']}."
            )
        else:
            if args.output.exists():
                try:
                    previous = json.loads(args.output.read_text(encoding="utf-8"))
                    comparable_previous = dict(previous)
                    comparable_feed = dict(feed)
                    comparable_previous.pop("updatedAt", None)
                    comparable_feed.pop("updatedAt", None)
                    if comparable_previous == comparable_feed and isinstance(
                        previous.get("updatedAt"), str
                    ):
                        feed["updatedAt"] = previous["updatedAt"]
                except (OSError, json.JSONDecodeError):
                    pass
            args.output.write_text(
                json.dumps(feed, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            print(f"Ceník bezpečně uložen do {args.output}.")
        return 0
    except Exception as error:
        print(f"Cenový robot zastavil aktualizaci: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
