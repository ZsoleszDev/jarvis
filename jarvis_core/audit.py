"""Visszamenőleges ellenőrzés: a gépen lévő találatok, tippek és az árbázis a mostani szabályokkal újramérve.

Használat a projektmappában (a .venv Pythonjával):

    python -m jarvis_core.audit            # a beállított munka mappa ([work] dir; üresen %LOCALAPPDATA%\\Jarvis\\munka)
    python -m jarvis_core.audit MAPPA      # egy másik mappa (pl. egy másolat)
    python -m jarvis_core.audit --all      # a maradó találatokat is felsorolja
    python -m jarvis_core.audit --json     # gépi feldolgozáshoz (pl. hogy elküldd valakinek)

Csak olvas: a kereskedes.json, az arbazis.json és az allapotok.json egy ideiglenes mappába kerül, és ott dolgozik; a munka
mappában semmit nem ír át (az árbázist a Jarvis a következő indításkor magától rendezi át: pricebook.KEY_VERSION).

Mit néz meg:
- az árbázist: hány ár nem a termék ára a mostani szabályok szerint (alkatrész, kiegészítő, másolat), és hány
  laptop-memória (SO-DIMM) ára volt az asztali memóriák között (ezek most a saját kulcsukra kerülnek);
- minden találatot és tippet: a mostani árakkal és szabályokkal is jelölt lenne-e: kiesne (nem maga a termék), már nem
  érné meg (pl. mert laptop-memória, és a laptop-memóriákhoz mérve drága), csak bizonytalan lenne (Érdemes megnézni),
  vagy marad. A felmért keresésednél (a szokásos árát te hagytad jóvá) csak azt nézi, illik-e még a kereséshez.
A mostani árbázissal számol (az árak azóta változhattak): ez nem azt mondja meg, mi volt akkor a jó döntés, hanem hogy ma
mit mondana róla; a leírását csak annál a találatnál ismeri, amelyiket a modell még nem nézett meg (a többinél a cím
alapján)."""
from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
import time
from collections import Counter
from pathlib import Path
from typing import Any

from .deals import WATCH_PROFIT, DealHunter, matches_query
from .pricebook import product_key, unusable
from .selling import PLATFORMS, format_price

FILES = ("kereskedes.json", "arbazis.json", "allapotok.json")
EXAMPLES = 5               # ennyi példát ír ki egy-egy okhoz
VERDICTS = (("kiesne", "Kiesne: nem maga a termék"), ("nem érné meg", "Már nem érné meg"),
            ("bizonytalan", "Csak bizonytalan lenne (Érdemes megnézni)"), ("marad", "Marad"))


def work_dir() -> Path:
    """A beállított munka mappa (config.toml [work] dir; üresen az adatmappán belül: %LOCALAPPDATA%\\Jarvis\\munka)."""
    from .config import data_path, load_config
    return data_path(load_config().work.dir, "munka")


def _book_report(raw: dict[str, Any]) -> dict[str, Any]:
    """A mentett árbázis (a régi kulcsokkal): hány ára nem a termék ára a mostani szabályok szerint (az okkal), és hány
    laptop-memória ára volt egy kivitel nélküli (asztali) memória kulcsán."""
    keys = raw.get("keys") if isinstance(raw.get("keys"), dict) else {}
    out: dict[str, Any] = {"version": raw.get("kv"), "products": len(keys), "prices": 0, "not_product": Counter(),
                           "examples": {}, "laptop_moved": 0, "laptop_examples": []}
    for old_key, entry in keys.items():
        for sample in (entry or {}).get("samples") or []:
            out["prices"] += 1
            title = str(sample.get("t") or "")
            if not title:
                continue
            why = unusable(title)
            if why:
                reason = why.split(" (")[0]
                out["not_product"][reason] += 1
                examples = out["examples"].setdefault(reason, [])
                if len(examples) < EXAMPLES:
                    examples.append({"t": title, "p": int(sample.get("p") or 0), "key": old_key, "why": why})
                continue
            new = product_key(title)
            if new and "sodimm" in new[0].split() and "sodimm" not in str(old_key).split():
                out["laptop_moved"] += 1
                if len(out["laptop_examples"]) < EXAMPLES:
                    out["laptop_examples"].append({"t": title, "p": int(sample.get("p") or 0), "key": old_key})
    out["not_product"] = dict(out["not_product"].most_common())
    return out


def _check(hunter: DealHunter, item: dict[str, Any], kind: str) -> dict[str, Any]:
    """Egy találat (kind: „találat”) vagy tipp („tipp”) a mostani szabályokkal: {verdict, why, old, new, …}."""
    title = str(item.get("title") or item.get("text") or "")
    out = {"kind": kind, "title": title, "price": int(item.get("price") or 0), "url": str(item.get("url") or ""),
           "site": str(item.get("site") or item.get("platform") or ""), "old": int(item.get("median") or item.get("usual")
                                                                                   or 0),
           "bought": bool(item.get("bought")), "new": None}
    why = unusable(title)
    if why:
        return {**out, "verdict": "kiesne", "why": why}
    if (item.get("basis") or {}).get("given"):
        query = str(item.get("query") or "")
        if query and not matches_query(query, title):
            return {**out, "verdict": "kiesne", "why": f"a keresésedhez („{query}”) már nem illik"}
        return {**out, "verdict": "marad", "why": "a szokásos árát te hagytad jóvá (a keresésed)"}
    if item.get("auction") or item.get("bundle") or item.get("haggle"):
        return {**out, "verdict": "marad", "why": "licit, csomag vagy alku: ezt nem méri újra"}
    if not out["url"] or not out["price"]:
        return {**out, "verdict": "marad", "why": "hiányos adat"}
    row = {"title": title, "text": str(item.get("text") or title), "price": out["price"], "url": out["url"],
           "platform": str(item.get("platform") or ""), "site": str(item.get("site") or ""),
           **({"seller": str(item["seller"])} if item.get("seller") else {})}
    floor = int(item.get("floor") or item.get("min_profit") or WATCH_PROFIT)
    value = hunter.quick_value(row, floor)
    if value is None:
        return {**out, "verdict": "bizonytalan", "why": "a mostani árbázisban nincs hozzá elég hasonló ár"}
    out["new"] = int(value["reference"])
    details = str((item.get("check") or {}).get("details") or item.get("details") or "")
    deal = {**item, **row, "median": out["new"], "low": value.get("low"), "basis": value.get("basis"), "floor": floor,
            "page_checked": True, "details": details}
    respec = hunter._respec(deal)
    if respec is not None:
        state, why, _hold = respec
        if deal.get("median"):
            out["new"] = int(deal["median"])
        verdict = {"hibás": "kiesne", "nem éri meg": "nem érné meg"}.get(state, "bizonytalan")
        return {**out, "verdict": verdict, "why": why}
    if deal.get("respec_key"):
        out["new"] = int(deal["median"])
    if not value["cheap"]:
        return {**out, "verdict": "nem érné meg",
                "why": f"a hasonlók most kb. {format_price(out['new'])}-ért mennek (akkor: "
                       f"{format_price(out['old'])})" if out["old"] else
                       f"a hasonlók most kb. {format_price(out['new'])}-ért mennek"}
    if not value["sure"]:
        found = value.get("found") or {}
        return {**out, "verdict": "bizonytalan",
                "why": str(found.get("unsure_why") or found.get("stale") or "kevés a biztos összehasonlító ár")}
    return {**out, "verdict": "marad", "why": f"most is kb. {format_price(out['new'])} a szokásos ára"}


def audit(root: Path, clock: Any = time.time) -> dict[str, Any]:
    """Az ellenőrzés (lásd fent) egy munka mappára: {root, book, items}. A fájlokat egy ideiglenes másolaton méri."""
    root = Path(root)
    raw: dict[str, Any] = {}
    try:
        raw = json.loads((root / "arbazis.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raw = {}
    with tempfile.TemporaryDirectory() as tmp:
        for name in FILES:
            if (root / name).is_file():
                shutil.copy2(root / name, Path(tmp) / name)
        hunter = DealHunter(Path(tmp), PLATFORMS, None, None, clock=clock)
        with hunter._lock:
            deals = [dict(d) for d in hunter._data["deals"]]
            tips = [dict(t) for t in hunter._data["tips"]]
        items = [_check(hunter, deal, "találat") for deal in deals] + [_check(hunter, tip, "tipp") for tip in tips]
    return {"root": str(root), "at": clock(), "book": _book_report(raw if isinstance(raw, dict) else {}),
            "items": items}


def report(result: dict[str, Any], everything: bool = False) -> str:
    """Az ellenőrzés eredménye olvasható szövegként."""
    book, items = result["book"], result["items"]
    when = time.strftime("%Y-%m-%d %H:%M", time.localtime(result["at"]))
    lines = [f"Visszamenőleges ellenőrzés – {result['root']} ({when})",
             "A mostani árakkal és szabályokkal számoltam; a fájljaidhoz nem nyúltam.", ""]
    lines.append(f"Árbázis: {book['products']} termék, {book['prices']} ár.")
    total = sum(book["not_product"].values())
    if total:
        lines.append(f"  {total} ár nem a termék ára (a következő indítástól kimarad):")
        for reason, count in book["not_product"].items():
            lines.append(f"    {reason}: {count}")
            for example in book["examples"].get(reason, [])[:3]:
                lines.append(f"      „{example['t']}” {format_price(example['p'])} – a(z) „{example['key']}” termék "
                             f"árai között ({example['why']})")
    if book["laptop_moved"]:
        lines.append(f"  {book['laptop_moved']} laptop-memória (SO-DIMM) ára volt az asztali memóriák között (a "
                     "következő indítástól külön számít), pl.:")
        for example in book["laptop_examples"][:3]:
            lines.append(f"      „{example['t']}” {format_price(example['p'])} – a(z) „{example['key']}” árai között")
    if not total and not book["laptop_moved"]:
        lines.append("  Nem találtam benne idegen árat.")
    lines += ["", f"Találatok és tippek: {len(items)}"]
    for verdict, label in VERDICTS:
        group = [item for item in items if item["verdict"] == verdict]
        if not group:
            continue
        lines.append(f"  {label}: {len(group)}")
        if verdict == "marad" and not everything:
            continue
        for item in group:
            bought = " [MEGVETTED]" if item["bought"] else ""
            prices = ""
            if item.get("new") is not None and item["old"] and item["new"] != item["old"]:
                prices = f" (szokásos ár akkor {format_price(item['old'])}, most {format_price(item['new'])})"
            lines.append(f"    - {item['kind']}: „{item['title'][:70]}” {format_price(item['price'])} ({item['site']})"
                         f"{bought}: {item['why']}{prices}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m jarvis_core.audit",
                                     description="A találatok, tippek és az árbázis a mostani szabályokkal újramérve "
                                                 "(csak olvas).")
    parser.add_argument("root", nargs="?", help="a munka mappa (alapból a beállított)")
    parser.add_argument("--all", action="store_true", help="a maradó találatokat is felsorolja")
    parser.add_argument("--json", action="store_true", help="JSON-ban írja ki")
    args = parser.parse_args(argv)
    root = Path(args.root) if args.root else work_dir()
    if not (root / "kereskedes.json").is_file() and not (root / "arbazis.json").is_file():
        print(f"Nem találok kereskedési adatot itt: {root}", file=sys.stderr)
        return 1
    result = audit(root)
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=1))
    else:
        print(report(result, args.all))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
