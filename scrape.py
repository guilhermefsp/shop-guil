"""
Scrapes the Amazon affiliate wishlist and generates items.json + data.js.

Usage:
    uv run python raw/projects/amazon-affiliate/scrape.py

Output:
    raw/projects/amazon-affiliate/items.json  — raw data
    raw/projects/amazon-affiliate/data.js     — embeddable JS for index.html
"""

import asyncio
import json
import platform
import re
import sys
from datetime import date
from pathlib import Path

import httpx
from playwright.async_api import async_playwright

WISHLIST_URL  = "https://www.amazon.com.br/hz/wishlist/ls/2HZT8IDK09OSC"
ASSOCIATE_TAG = "guilhermefsp-20"
FIREBASE_URL  = "https://guil-default-rtdb-default-rtdb.firebaseio.com"
HERE = Path(__file__).parent

# (category_name, keywords_to_match_in_lowercase_title)
# Order matters: first match wins. Outros is the fallback (no keywords needed).
CATEGORY_RULES: list[tuple[str, list[str]]] = [
    ("Jogos e Acessórios", [
        # Games
        "tabuleiro", "dado", "dados", "dominó", "xadrez", "dama",
        "jogo de cartas", "baralho", "rpg", "board game", "card game",
        "dice", "miniature", "dungeon", "eurogame", "wargame",
        "quebra-cabeça", "meeple", "estratégia", "war game",
        # Accessories
        "sleeve", "sleeves", "card sleeve", "playmat", "token",
        "insert", "organizer", "organizador", "expansion", "expansão",
        "promo", "kickstarter", "compendium",
    ]),
    ("Tecnologia", [
        "teclado", "mouse", "monitor", "tablet", "fone de ouvido",
        "headphone", "carregador", "cabo usb", "adaptador", "webcam",
        "keyboard", "headset", "hub", "kindle", "e-reader", "speaker",
        "microfone", "microphone", "hd", "ssd", "memória ram",
        "notebook", "laptop", "impressora", "roteador", "router",
        "lâmpada inteligente", "smart plug", "alexa", "google home",
    ]),
    ("Casa", [
        "cozinha", "panela", "frigideira", "decoração", "luminária",
        "vela", "tapete", "almofada", "utensílio", "caneca", "copo",
        "móvel", "cadeira", "mesa", "prateleira", "shelf",
        "limpeza", "detergente", "sabão", "aspirador",
        "alimento", "café", "chá", "tempero",
    ]),
    ("Olívia", [
        "bebê", "baby", "infantil", "brinquedo", "pelúcia", "boneca",
        "educativo", "didático", "criança", "toy", "kids",
    ]),
]


def parse_price_float(price_str: str) -> float | None:
    if not price_str:
        return None
    cleaned = re.sub(r"[^\d,]", "", price_str).replace(",", ".")
    try:
        return float(cleaned)
    except ValueError:
        return None


def assign_category(title: str) -> str:
    text = title.lower()
    for name, keywords in CATEGORY_RULES:
        if any(kw in text for kw in keywords):
            return name
    return "Outros"


async def scrape_wishlist() -> list[dict]:
    items = []

    async with async_playwright() as p:
        if platform.system() == "Windows":
            browser = await p.chromium.launch(channel="msedge", headless=True)
        else:
            browser = await p.chromium.launch(headless=True)

        context = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1280, "height": 900},
            locale="pt-BR",
            extra_http_headers={"Accept-Language": "pt-BR,pt;q=0.9,en;q=0.8"},
        )
        await context.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
        )
        page = await context.new_page()

        print(f"Loading: {WISHLIST_URL}")
        await page.goto(WISHLIST_URL, wait_until="domcontentloaded", timeout=30000)
        await page.wait_for_timeout(2000)

        page_num = 1
        seen_asins: set[str] = set()

        while True:
            print(f"  Page {page_num} — ", end="", flush=True)

            # Scroll to trigger lazy loading
            stale = 0
            prev_height = -1
            while stale < 3:
                await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                await page.wait_for_timeout(1500)
                height = await page.evaluate("document.body.scrollHeight")
                if height == prev_height:
                    stale += 1
                else:
                    stale = 0
                prev_height = height

            for load_sel in [
                "button:has-text('Mostrar mais')",
                "a:has-text('Mostrar mais')",
                "[data-action='load-more-items'] button",
            ]:
                try:
                    btn = await page.query_selector(load_sel)
                    if btn and await btn.is_visible():
                        await btn.click()
                        await page.wait_for_timeout(2000)
                except Exception:
                    pass

            await page.evaluate("window.scrollTo(0, 0)")

            try:
                await page.wait_for_selector(
                    "a.a-link-normal[href*='/dp/']", timeout=10000
                )
            except Exception:
                print("no items found, stopping.")
                break

            links = await page.query_selector_all("a.a-link-normal[href*='/dp/']")
            page_count = 0

            for link in links:
                href = await link.get_attribute("href") or ""
                m = re.search(r"/dp/([A-Z0-9]{10})", href)
                if not m:
                    continue
                asin = m.group(1)
                if asin in seen_asins:
                    continue
                seen_asins.add(asin)

                title = (await link.get_attribute("title") or "").strip()
                image = ""
                price = ""

                try:
                    li = await link.evaluate_handle(
                        "el => el.closest('li') || el.closest('[data-id]')"
                    )
                    if li:
                        img = await li.query_selector("img.wl-img-size-adjust, img[alt]")
                        if img:
                            image = await img.get_attribute("src") or ""
                            if not title:
                                title = (await img.get_attribute("alt") or "").strip()

                        for sel in [
                            ".a-price .a-offscreen",
                            ".a-color-price",
                            ".itemUsedAndNewPrice",
                        ]:
                            price_el = await li.query_selector(sel)
                            if price_el:
                                price = (await price_el.inner_text()).strip()
                                if price:
                                    break
                except Exception:
                    pass

                items.append({
                    "title": title,
                    "asin": asin,
                    "image": image,
                    "price": price,
                    "category": assign_category(title),
                    "affiliate_url": f"https://www.amazon.com.br/dp/{asin}/?tag={ASSOCIATE_TAG}",
                })
                page_count += 1

            print(f"{page_count} items (total: {len(items)})")

            next_btn = None
            for sel in [
                "li.a-last:not(.a-disabled) a",
                "ul.a-pagination li.a-last:not(.a-disabled) a",
                "a[aria-label*='próxima' i]",
                "a[aria-label*='next' i]",
                ".a-pagination .a-last:not(.a-disabled) a",
            ]:
                next_btn = await page.query_selector(sel)
                if next_btn:
                    break

            if not next_btn:
                for a in await page.query_selector_all("a"):
                    try:
                        text = (await a.inner_text()).strip().lower()
                        if text in ("próxima", "next", "próxima página", ">"):
                            next_btn = a
                            break
                    except Exception:
                        continue

            if not next_btn:
                pag = await page.query_selector(".a-pagination")
                if pag:
                    print(f"  Pagination HTML: {await pag.inner_html()}")
                else:
                    print("  No pagination — done.")
                break

            await next_btn.scroll_into_view_if_needed()
            await next_btn.click()
            await page.wait_for_load_state("domcontentloaded")
            await page.wait_for_timeout(2000)
            page_num += 1

        await browser.close()

    return items


def write_outputs(items: list[dict]) -> None:
    today = date.today().isoformat()

    # Load or initialize local history
    history_path = HERE / "history.json"
    history: dict = {}
    if history_path.exists():
        try:
            history = json.loads(history_path.read_text(encoding="utf-8"))
        except Exception:
            history = {}

    # Update history and embed date_added in each item
    for item in items:
        asin = item["asin"]
        current_price = parse_price_float(item.get("price", ""))

        if asin not in history:
            history[asin] = {"first_seen": today, "prices": []}

        item["date_added"] = history[asin]["first_seen"]

        prices = history[asin].setdefault("prices", [])
        last_price = prices[-1]["p"] if prices else None
        if current_price is not None and current_price != last_price:
            prices.append({"p": current_price, "d": today})

    # Save history.json
    history_path.write_text(
        json.dumps(history, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"Saved history  → {history_path}")

    # Push history to Firebase
    if FIREBASE_URL:
        try:
            r = httpx.put(
                FIREBASE_URL.rstrip("/") + "/items.json",
                content=json.dumps(history, ensure_ascii=False).encode(),
                headers={"Content-Type": "application/json"},
                timeout=15,
            )
            print(f"Firebase sync  → HTTP {r.status_code}")
        except Exception as e:
            print(f"Firebase sync failed: {e}", file=sys.stderr)

    # Write items.json
    json_path = HERE / "items.json"
    json_path.write_text(
        json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"Saved {len(items)} items → {json_path}")

    # Write data.js
    js_path = HERE / "data.js"
    js_path.write_text(
        f"// Auto-generated by scrape.py — do not edit manually\n"
        f"const ITEMS = {json.dumps(items, ensure_ascii=False, indent=2)};\n",
        encoding="utf-8",
    )
    print(f"Saved JS data  → {js_path}")


async def main() -> None:
    items = await scrape_wishlist()
    if not items:
        print("No items scraped — check the wishlist URL.", file=sys.stderr)
        sys.exit(1)
    write_outputs(items)


if __name__ == "__main__":
    asyncio.run(main())
