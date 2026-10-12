#!/usr/bin/env python3
"""Revita -> supplied Temu template. Offline tests never access the website."""
from __future__ import annotations

import argparse
import base64
import copy
import csv
import hashlib
import http.cookiejar
import itertools
import json
import logging
import re
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from lxml import etree, html

LOG = logging.getLogger("revita")
BASE = "https://revita.bg"
CATEGORIES = ["alive", "natures-way", "double-wood", "barbel-drexel",
              "now-foods", "natural-factors", "webber-naturals"]
NS = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
M = "{" + NS["m"] + "}"
CLASS = lambda name: 'contains(concat(" ",normalize-space(@class)," ")," ' + name + ' ")'


def norm(value):
    value = unicodedata.normalize("NFKD", str(value or "")).casefold()
    return re.sub(r"[^\w]+", " ", "".join(c for c in value if not unicodedata.combining(c))).strip()


def clean(value):
    return re.sub(r"\s+", " ", str(value or "")).strip()


def first(root, query, default=""):
    found = root.xpath(query)
    if not found:
        return default
    return clean(found[0].text_content() if hasattr(found[0], "text_content") else found[0])


def money(value):
    value = re.sub(r"[^\d,.-]", "", str(value or ""))
    if not value:
        return None
    if "," in value and "." in value:
        value = value.replace(",", "") if value.rfind(".") > value.rfind(",") else value.replace(".", "").replace(",", ".")
    else:
        value = value.replace(",", ".")
    try:
        return float(Decimal(value).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))
    except Exception:
        return None


def canonical(url):
    u = urllib.parse.urlsplit(urllib.parse.urljoin(BASE, url))
    if u.hostname not in ("revita.bg", "www.revita.bg"):
        raise ValueError("URL outside revita.bg: " + url)
    return BASE + u.path.rstrip("/")


def product_url(url):
    return urllib.parse.urlsplit(url).path.startswith("/shop/")


def parse_doc(source):
    # Decode explicitly: the supplied HTML is UTF-8, not Latin-1.
    return html.fromstring(source.decode("utf-8-sig", errors="replace") if isinstance(source, bytes) else source)


def walk_json(value):
    if isinstance(value, dict):
        yield value
        for v in value.values():
            yield from walk_json(v)
    elif isinstance(value, list):
        for v in value:
            yield from walk_json(v)


def json_nodes(doc):
    for s in doc.xpath('//script[@type="application/ld+json"]'):
        try:
            yield from walk_json(json.loads(s.text or ""))
        except (ValueError, TypeError):
            continue


def decoded_expiry(root):
    for value in root.xpath('.//*[@data-best_before_text]/@data-best_before_text'):
        try:
            t = base64.b64decode(value).decode("utf-8")
            m = re.search(r"(\d{2})\.(\d{2})\.(\d{4})", t)
            if m:
                return datetime.strptime(m.group(0), "%d.%m.%Y").date().isoformat()
        except (ValueError, UnicodeError):
            pass
    return ""


def parse_listing(source, category_url):
    doc = parse_doc(source)
    containers = doc.xpath('//*[@id="js-product-list"]')
    if not containers:
        # Limited fallback; never harvest recommendation carousels.
        containers = doc.xpath('//*[@id="products"]')
    if not containers:
        raise ValueError("Listing container absent; cannot confirm category completeness")
    cards = containers[0].xpath('.//article[' + CLASS("product-miniature") + ']')
    hints = {}
    for card in cards:
        link = first(card, './/*[' + CLASS("product-title") + ']//a/@href') or first(card, './/a[' + CLASS("product-thumbnail") + ']/@href')
        if not link or not product_url(link):
            continue
        url = canonical(link)
        hints[url] = {"id": card.get("data-id-product", ""),
                      "reference": first(card, './/*[' + CLASS("product-reference") + ']//span'),
                      "brand": first(card, './/*[' + CLASS("product-brand") + ']'),
                      "best_before": decoded_expiry(card)}
    pages = set()
    base_path = urllib.parse.urlsplit(category_url).path.rstrip("/")
    for a in doc.xpath('//*[' + CLASS("pagination") + ']//*[@href or @data-href]'):
        value = a.get("href") or a.get("data-href")
        u = urllib.parse.urlsplit(urllib.parse.urljoin(category_url, value))
        query = urllib.parse.parse_qs(u.query)
        if u.hostname in ("revita.bg", "www.revita.bg") and u.path.rstrip("/") == base_path and query.get("page", [""])[0].isdigit():
            number=query['page'][0]
            # Page 1 is the unparameterized start page, not a new crawl target.
            pages.add(BASE + u.path.rstrip('/') + ("?page="+number if int(number)>1 else ''))
    total = None
    text = " ".join(doc.xpath('//*[@id="js-product-list-top" or ' + CLASS("pagination") + ']//text()'))
    match = re.search(r"(?:общо\s*|от\s+)(\d+)\s*(?:продукт|product)", text, re.I)
    if match:
        total = int(match.group(1))
    if not hints:
        raise ValueError("No product cards in category; empty or blocked page")
    return hints, sorted(pages), total


def dosage_form(title, source):
    rules = [("Gummies", r"желиран|gumm|дъвчащ"), ("Capsules", r"капсул|capsul|softgel"),
             ("Tablets", r"таблет|каплет|tablet|caplet"), ("Powder", r"прах|powder"),
             ("Liquid", r"течн|капки|liquid|drops|\b\d+\s*(?:ml|мл)\b"),
             ("Tea Bag", r"чай|tea bag"), ("Ointment", r"мехлем|ointment"),
             ("Granules", r"гранули|granul"), ("Pellets", r"пелети|pellet")]
    for text in (title, source):
        for value, pattern in rules:
            if re.search(pattern, text, re.I):
                return value
    return "Others"


def parse_product(source, url, hint=None, brand_hint=""):
    hint = hint or {}
    doc = parse_doc(source)
    canonical_url = first(doc, '//link[@rel="canonical"]/@href') or url
    if not product_url(canonical_url) or canonical(canonical_url) != canonical(url):
        raise ValueError("Product redirected to a different page")
    wrappers = doc.xpath('//*[@id="main-product-wrapper"]')
    root = wrappers[0] if wrappers else doc
    title = first(root, './/h1') or first(doc, '//h1')
    forms = root.xpath('.//form[@id="add-to-cart-or-refresh"]')
    if not title or not forms:
        raise ValueError("Primary title/cart form absent: blocked page or changed layout")
    form = forms[0]
    prices = form.xpath('.//*[' + CLASS("product-prices") + ']')
    price_root = prices[0] if prices else form
    current = first(price_root, './/*[' + CLASS("product-price") + ']/@content') or first(price_root, './/*[' + CLASS("current-price") + ']')
    regular = first(price_root, './/*[' + CLASS("regular-price") + ']/@data-product_regular_price') or first(price_root, './/*[' + CLASS("regular-price") + ']')
    currency = first(price_root, './/*[' + CLASS("product-price") + ']/@data-currency_sign')
    if not currency:
        currency = "EUR" if price_root.xpath('.//meta[@content="EUR"]') or "€" in price_root.text_content() else "BGN" if "лв" in price_root.text_content() else ""
    price, list_price = money(current), money(regular)
    if currency in ("лв.", "лв", "BGN"):
        # Only used if the response explicitly identifies BGN.
        price = round(price / 1.95583, 2) if price is not None else None
        list_price = round(list_price / 1.95583, 2) if list_price is not None else None
    elif currency not in ("€", "EUR"):
        raise ValueError("Unidentified price currency; refusing an assumed conversion")
    if price is None or price <= 0:
        raise ValueError("Missing/invalid primary product price")
    nodes = list(json_nodes(doc))
    substance = next((n for n in nodes if n.get("@type") == "Substance" and (not n.get("url") or canonical(n["url"]) == canonical(url))), {})
    ingredients = [clean(x) for x in substance.get("activeIngredient", []) if isinstance(x, str)]
    intake = [clean(x) for x in substance.get("maximumIntake", []) if isinstance(x, str)]
    # Elementor editors contain description, composition, directions and manufacturer.
    # Restrict to section#main; omit questions, articles and recommended products.
    editors = doc.xpath('//*[@id="main"]//div[' + CLASS("elementor-text-editor") + ']')
    if not editors:
        editors = root.xpath('.//*[@id="description"] | .//*[' + CLASS("product-description") + ']')
    blocks = []
    tables = []
    for editor in editors:
        if editor.xpath('ancestor::article | ancestor::*[@id="questions-list"]'):
            continue
        tables.extend(editor.xpath('.//table'))
        for el in editor.xpath('.//p | .//h2 | .//h3 | .//li'):
            if not el.xpath('ancestor::table'):
                text = clean(el.text_content())
                if text and text not in blocks:
                    blocks.append(text)
    if not ingredients:
        for table in tables:
            if "състав" not in norm(table.text_content()):
                continue
            for row in table.xpath('.//tr')[1:]:
                cells = [clean(e.text_content()) for e in row.xpath('./td | ./th')]
                if cells:
                    ingredients.append(" ".join(cells[:2]))
    factual = "\n".join(blocks)
    brand = first(root, './/*[' + CLASS("product-manufacturer") + ']//img/@alt') or hint.get("brand") or brand_hint
    manufacturer = next((b.split(":", 1)[1].strip() for b in blocks if re.match(r"^Производител\s*:", b, re.I)), "")
    importer = next((b for b in blocks if re.match(r"^Вносител\s*:", b, re.I)), "")
    directions = list(dict.fromkeys(intake + [b for b in blocks if re.match(r"^(Забележка|Съхранение|Годност|Препоръчителна дневна|Начин на|Дозировка|Предупреждение)\s*:", b, re.I)]))
    # Do not copy therapeutic marketing paragraphs into a marketplace description.
    directions = [b for b in directions if not b.lower().startswith("приложение:")]
    description = [title, "Марка: " + brand, "Хранителна добавка."]
    if ingredients:
        description.append("Състав (според етикета/страницата):\n" + "\n".join(ingredients))
    description.extend(directions)
    if manufacturer:
        description.append("Производител: " + manufacturer)
    if importer:
        description.append(importer)
    images = []
    for image in root.xpath('.//*[@id="product-images-large"]//img'):
        src = image.get("data-image-large-src") or image.get("data-full-size-image-url") or image.get("data-lazy") or image.get("src")
        if src:
            src = urllib.parse.urljoin(BASE, src)
            if src not in images and src.startswith("https://"):
                images.append(src)
    if not images:
        for n in nodes:
            if n.get("@type") == "Product" and canonical(n.get("url", url)) == canonical(url):
                imgs = n.get("image", [])
                for img in imgs if isinstance(imgs, list) else [imgs]:
                    img = img.get("url", "") if isinstance(img, dict) else img
                    if isinstance(img, str) and img and img not in images:
                        images.append(urllib.parse.urljoin(BASE, img))
    if not images:
        raise ValueError("No primary product images found")
    reference = first(price_root, './/*[' + CLASS("product-reference") + ']//span') or hint.get("reference", "")
    pid = first(form, './/input[@name="id_product"]/@value') or hint.get("id")
    links = price_root.xpath('.//link/@href')
    stock = "out" if any("OutOfStock" in x or "Discontinued" in x for x in links) else "in" if any("InStock" in x or "LimitedAvailability" in x for x in links) else "unknown"
    buttons = form.xpath('.//button[@data-button-action="add-to-cart"]')
    if buttons:
        stock = "out" if buttons[0].get("disabled") is not None else "in" if stock == "unknown" else stock
    ean = first(root, './/*[@itemprop="gtin13" or @itemprop="gtin12" or @itemprop="gtin14"]/@content')
    if not ean:
        for n in nodes:
            if n.get("@type") == "Product" and canonical(n.get("url", url)) == canonical(url):
                ean = str(n.get("gtin13") or n.get("gtin12") or n.get("gtin14") or "")
    specs = {}
    for select in form.xpath('.//*[' + CLASS("product-variants") + ']//select'):
        options = [(e.get("value"), clean(e.text_content())) for e in select.xpath('./option') if e.get("value")]
        if options:
            specs[select.get("name")] = options
    for ul in form.xpath('.//*[' + CLASS("product-variants") + ']//ul'):
        inputs = ul.xpath('.//input[@name and @value]')
        if inputs:
            specs[inputs[0].get("name")] = [(x.get("value"), x.get("title") or x.get("value")) for x in inputs]
    country = ""
    # Manufacturer address is not proof of the product's country of origin.
    origin_text = next((b for b in blocks if re.match(r'^(Страна|Държава) на произход\s*:|^Country of origin\s*:',b,re.I)), '')
    for pat, val in [(r"\bUSA\b|\bUnited States\b|САЩ", "United States"),
                     (r"Canada|Канада", "Canada"), (r"Germany|Германия", "Germany"),
                     (r"Bulgaria|България", "Bulgaria")]:
        if re.search(pat, origin_text, re.I):
            country = val
            break
    form_name = dosage_form(title, "\n".join(ingredients))
    count = re.search(r"(\d+)\s*(?:желирани\s+)?(?:капсули|таблетки|каплети|gummies|capsules|tablets|softgels|mini\s*caplets)", title, re.I)
    content = re.search(r"(\d+(?:[.,]\d+)?)\s*(ml|мл|kg|кг|g|гр|грама)\b", title, re.I)
    net, unit = (float(content.group(1).replace(",", ".")), {"мл":"ml", "кг":"kg", "гр":"g", "грама":"g"}.get(content.group(2).lower(), content.group(2).lower())) if content else (None, "")
    return {"url": canonical(url), "id": str(pid or ""), "reference": reference, "title": title,
            "brand": brand, "manufacturer_source": manufacturer, "country_source": country,
            "price": price, "list_price": list_price if list_price is not None and list_price >= price else price,
            "stock": stock, "images": images, "description": "\n\n".join(description)[:30000],
            "ingredients": ingredients, "directions": directions, "source_text": factual,
            "form": form_name, "count": int(count.group(1)) if count else None,
            "net": net, "net_unit": unit, "ean": ean, "best_before": hint.get("best_before", ""),
            "variant_options": specs, "variant_id": "0", "variant_label": ""}


# Bulgarian aliases plus English category terms, matched first against title.
ALIASES = {
    "Alfalfa": "люцерна", "Aloe Vera": "алое", "Artichoke": "артишок", "Astragalus": "астрагал",
    "Black Cohosh": "черен кохош|цимицифуга", "Cayenne": "кайен", "Chamomile": "лайка",
    "Cinnamon": "канела", "Dong Quai": "донг куай", "Echinacea": "ехинацея", "Evening Primrose": "вечерна иглика",
    "Flaxseed": "ленено семе", "Garlic": "чесън", "Ginger": "джинджифил", "Ginkgo": "гинко", "Ginseng": "женшен",
    "Goldenseal": "хидрастис", "Green Tea": "зелен чай", "Hawthorn": "глог", "Horny Goat Weed": "епимедиум",
    "Kava": "кава", "Licorice": "женско биле|сладник", "Milk Thistle": "бял трън", "Mushrooms": "гъби|рейши|кордицепс|лъвска грива|hericium",
    "Neem": "нийм", "Nettle": "коприва", "Olive Leaf": "маслинови листа", "Oregano": "риган",
    "Red Yeast Rice": "червен ферментирал ориз", "Saw Palmetto": "сао палмето|соу палмето",
    "Stevia": "стевия", "St. John's Wort": "жълт кантарион", "Tribulus": "трибулус|бабини зъби",
    "Turmeric": "куркума", "Valerian": "валериан", "Psyllium": "псилиум|индийски живовляк",
    "Bacopa": "бакопа", "Chlorophyll": "хлорофил", "Shilajit": "шилажит|мумийо", "Curcumin": "куркумин",
    "Chlorella": "хлорела", "Bilberry": "черна боровинка", "Cranberry": "червена боровинка",
    "Elderberry": "черен бъз", "Grape Seed": "гроздови семена", "Ashwagandha": "ашваганда",
    "Gotu Kola": "готу кола|центела", "Spirulina": "спирулина", "Boswellia": "босвелия",
    "Chondroitin": "хондроитин", "Glucosamine": "глюкозамин", "Bromelain": "бромелаин", "Lactase": "лактаза",
    "Multi-Enzyme": "ензимен комплекс|храносмилателни ензими", "Msm": "метилсулфонилметан|msm",
    "Royal Jelly": "пчелно млечице", "Calcium": "калций", "Chromium": "хром", "Copper": "мед меден",
    "Iodine": "йод", "Iron": "желязо", "Magnesium": "магнезий", "Manganese": "манган",
    "Potassium": "калий", "Selenium": "селен", "Silica": "силиций", "Sodium": "натрий", "Zinc": "цинк",
    "Collagen": "колаген", "Dmae": "дмае", "Resveratrol": "ресвератрол", "Alpha Lipoic": "алфа липоева",
    "Beta Carotene": "бета каротин", "Coq10": "коензим q10|коензим q 10|убихинол|coenzyme q10",
    "Lutein": "лутеин", "Lycopene": "ликопен", "Quercetin": "кверцетин", "Rutin": "рутин",
    "Astaxanthin": "астаксантин", "Creatine": "креатин", "5-Htp": "5 хтп|5 хидрокситриптофан",
    "L-Glutamine": "глутамин", "L-Leucine": "левцин", "L-Citrulline": "цитрулин", "L-Arginine": "аргинин",
    "L-Lysine": "лизин", "Beta-Alanine": "бета аланин", "Taurine": "таурин", "Carnitine": "карнитин",
    "Mct": "мст", "Fish Oil": "рибено масло|омега 3|omega 3", "Krill Oil": "крил",
    "Black Seed Oil": "черен кимион", "Dietary Fiber": "фибри", "Charcoal": "активен въглен",
    "Acidophilus": "ацидофилус|пробиотик|probiotic", "Prebiotic": "пребиотик", "Hyaluronic Acid": "хиалуронова",
    "Multivitamins": "мултивитамин|multi vitamin|multivitamin", "Prenatal": "пренатал|за бременни",
    "Multiminerals": "мултиминерал|минерален комплекс", "Blended Vitamin & Mineral": "витамини и минерали|витаминно минерален",
    "Lecithin": "лецитин", "Choline": "холин", "Sleep": "мелатонин|melatonin|за сън",
}
VITAMINS = {"17694":"a", "17707":"c", "17708":"d", "17709":"e", "17710":"k1",
            "17697":"b1", "17698":"b2", "17699":"b3", "17700":"b5", "17701":"b6",
            "17702":"b9", "17703":"b12", "17704":"b7", "17705":"b complex", "17706":"b8"}


def terms_for(leaf):
    core = re.split(r"\s+(?:Herbal|Nutritional|Mineral|Vitamin|Antioxidant|Weight Loss|Supplements)", leaf, maxsplit=1)[0]
    result = [norm(core)]
    for key, vals in ALIASES.items():
        if norm(key) in norm(leaf):
            result += [norm(t) for t in vals.split("|")]
    return [t for t in result if len(t) > 2]


def category_for(product, categories, override=None):
    if override:
        key = str(override)
        if key not in categories:
            raise ValueError("Category override absent from this template: " + key)
        return key, "override"
    title = norm(product["title"])
    ingredients = norm(" ".join(product.get("ingredients", [])))
    scored = []
    for cid, full in categories.items():
        leaf = full.split(" / ")[-1]
        terms = terms_for(leaf)
        if cid in VITAMINS:
            v = VITAMINS[cid]
            letters = {"a":"[aа]", "b":"[bвб]", "c":"[cс]", "d":"[dд]", "e":"[eе]", "k":"[kк]"}
            vregex = letters.get(v[0], v[0]) + re.escape(v[1:])
            terms = []
            pattern = r"(?:vitamin|витамин)\s*" + vregex + (r"[23]?" if cid=='17708' else '') + r"(?!\w|\d)"
            score = 110 if re.search(pattern, title) else 15 if re.search(pattern, ingredients) else 0
            if cid == "17704" and ("биотин" in title or "biotin" in title): score = 115
            if cid == "17702" and any(x in title for x in ["фолиева", "фолат", "folic"]): score = 115
            if cid == "17705" and re.search(r"[bвб]\s*(?:комплекс|complex)", title): score = 115
        else:
            score = max([100 + min(len(t), 25) for t in terms if re.search(r"(?<!\w)" + re.escape(t) + r"\w*", title)] or [0])
            if not score:
                score = max([10 + min(len(t), 20) for t in terms if re.search(r"(?<!\w)" + re.escape(t) + r"\w*", ingredients)] or [0])
        if cid == "17711" and re.search(r"мултивитамин|multi\s*vitamin", title): score = 160
        if cid == "17712" and re.search(r"пренатал|prenatal|бременни", title): score = 170
        if cid == "17604" and re.search(r"витамини и минерали|vitamins and minerals", title): score = 155
        if score:
            scored.append((score, cid))
    scored.sort(key=lambda x: (-x[0], x[1]))
    if scored:
        score, cid = scored[0]
        reason = "title" if score >= 100 else "composition: review"
        if len(scored) > 1 and scored[1][0] >= 100 and score - scored[1][0] <= 8:
            reason += "; multiple matching ingredients: review"
        return cid, reason
    fallback = "17604" if "17604" in categories else next(iter(categories))
    return fallback, "closest available fallback: review"


class Template:
    """Patch only Template sheet XML, retaining all other ZIP members byte-for-byte."""
    def __init__(self, path):
        self.path = Path(path)
        with zipfile.ZipFile(path) as z:
            self.parts = {n: z.read(n) for n in z.namelist()}
        self.strings = ["".join(e.itertext()) for e in etree.fromstring(self.parts['xl/sharedStrings.xml']).findall(M+'si')]
        rels = etree.fromstring(self.parts['xl/_rels/workbook.xml.rels'])
        targets = {r.get('Id'): 'xl/' + r.get('Target').lstrip('/') for r in rels}
        self.sheets = {}
        for s in etree.fromstring(self.parts['xl/workbook.xml']).findall(M+'sheets/'+M+'sheet'):
            self.sheets[s.get('name')] = targets[s.get('{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id')]
        self.sheet_path = self.sheets['Template']
        self.root = etree.fromstring(self.parts[self.sheet_path])
        self.headers = {re.sub(r'\d', '', c.get('r')): self.value(c) for c in self.root.findall(M+'sheetData/'+M+'row') if c.get('r')=='2' for c in c}
        self.codes = {re.sub(r'\d', '', c.get('r')): self.value(c) for c in self.root.findall(M+'sheetData/'+M+'row') if c.get('r')=='4' for c in c}
        self.categories = {}
        for r in self.rows('Category Name'):
            if len(r) >= 2: self.categories[str(r[0])] = r[1]
        self.dropdowns = {r[0]: r[2:] for r in self.rows('Dropdown Lists') if len(r)>=2}
        self.requirements = {}
        for row in self.rows_dict('GoodsLevelMode'):
            key = row.get('A', '')
            if key.endswith('_require'):
                self.requirements[key.split('_')[0]] = [col for col, value in row.items() if col!='A' and value=='require']
        self.styles = {re.sub(r'\d', '', c.get('r')): c.get('s') for r in self.root.findall(M+'sheetData/'+M+'row') if r.get('r')=='5' for c in r}

    def value(self, c):
        if c.get('t') == 'inlineStr':
            return ''.join(c.find(M+'is').itertext())
        v = c.find(M+'v')
        if v is None: return ''
        return self.strings[int(v.text)] if c.get('t')=='s' else v.text or ''

    def rows_dict(self, name):
        root = etree.fromstring(self.parts[self.sheets[name]])
        return [{re.sub(r'\d','',c.get('r')):self.value(c) for c in row} for row in root.findall(M+'sheetData/'+M+'row')]

    def rows(self, name):
        return [list(row.values()) for row in self.rows_dict(name)]

    def options(self, col, row):
        code, header = self.codes[col], self.headers[col]
        cat = str(row.get('E',''))
        if code.startswith('t_3_'):
            n=column_number(col)
            p = next((v for a,b,v in [('HX','IG','HW'),('II','IR','IH'),('IT','JC','IS'),('JE','JN','JD'),('JP','JY','JO'),('KA','KA','JZ')] if column_number(a)<=n<=column_number(b)),None)
            keys = [f't_3_{cat}_{row.get(p, "")}_{header}'] if p else []
            keys += [f't_3_{cat}_{header}']
        elif code.startswith(('t_4_', 't_8_')):
            keys = [code[:4]+cat+'_'+header]
        elif col=='S':
            keys = [f't_1_{row.get("R", "")}_Trademark']
        else:
            keys = [code]
        for key in keys:
            if key in self.dropdowns: return self.dropdowns[key]
        return []

    def choose(self, col, value, row):
        if value in ('', None): return ''
        opts = self.options(col, row)
        if not opts: return value
        return next((x for x in opts if norm(x)==norm(value)), '')

    def write(self, data, destination):
        root = copy.deepcopy(self.root)
        sheet_data = root.find(M+'sheetData')
        for row in list(sheet_data):
            if int(row.get('r')) >= 5: sheet_data.remove(row)
        for number, values in enumerate(data, 5):
            row = etree.SubElement(sheet_data, M+'row', r=str(number))
            for col in sorted(values, key=column_number):
                value = values[col]
                if value in ('', None): continue
                attrs = {'r':col+str(number)}
                if self.styles.get(col): attrs['s'] = self.styles[col]
                c = etree.SubElement(row, M+'c', **attrs)
                if isinstance(value, (int, float)) and not isinstance(value, bool):
                    etree.SubElement(c, M+'v').text = str(value)
                else:
                    c.set('t','inlineStr')
                    t = etree.SubElement(etree.SubElement(c,M+'is'),M+'t')
                    t.set('{http://www.w3.org/XML/1998/namespace}space','preserve')
                    t.text = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f]', '', str(value))[:32767]
        root.find(M+'dimension').set('ref',f'A1:OA{max(5,len(data)+4)}')
        with zipfile.ZipFile(destination, 'w', zipfile.ZIP_DEFLATED) as out:
            for name, value in self.parts.items():
                out.writestr(name, etree.tostring(root,xml_declaration=True,encoding='UTF-8') if name==self.sheet_path else value)


def column_number(col):
    n=0
    for c in col: n=n*26+ord(c)-64
    return n


def ingredient_group(text):
    text = norm(text)
    for pattern, group, sub in [
        (r'витамин [bвб]\s*12|cobalamin', 'Vitamin B','Vitamin B12 (Cobalamin)'),
        (r'витамин [bвб]\s*1\b|тиамин|thiamin', 'Vitamin B','Vitamin B1 (Thiamine)'),
        (r'витамин [bвб]\s*2\b|рибофлавин', 'Vitamin B','Vitamin B2 (Riboflavin)'),
        (r'витамин [bвб]\s*6\b|пиридоксин', 'Vitamin B','Vitamin B6 (pyridoxine)'),
        (r'биотин|biotin', 'Vitamin B','Vitamin B7 (Biotin)'),
        (r'фолат|фолиева|folic', 'Vitamin B','Vitamin B9 (Folic Acid)'),
        (r'ниацин|niacin', 'Vitamin B','Vitamin B3 (Niacin)'),
        (r'пантотен', 'Vitamin B','Vitamin B5 (pantothenic Acid)'),
        (r'витамин [cс]\b|аскорбин|vitamin c', 'Vitamin C',''),
        (r'витамин [dд]\s*3?\b|vitamin d', 'Vitamin D',''),
        (r'витамин [aа]\b|ретинил|vitamin a', 'Vitamin A',''),
        (r'витамин [eе]\b|токоферол|vitamin e', 'Vitamin E',''),
        (r'витамин [kк]\b|vitamin k', 'Vitamin K',''),
        (r'колаген|collagen', 'Collagen',''),
        (r'хиалурон|hyaluronic', 'glucosamine','Hyaluronic Acid'),
        (r'магнезий|magnesium', 'Minerals','Magnesium'),
        (r'цинк|zinc', 'Minerals','Zinc'), (r'калций|calcium', 'Minerals','Calcium'),
        (r'желязо|iron', 'Minerals','Iron'), (r'селен|selenium', 'Minerals','Selenium'),
        (r'йод|iodine', 'Minerals','Iodine'), (r'бор\b|boron', 'Minerals','Boron'),
        (r'рибено масло|fish oil', 'Animal-derived Ingredients','Fish Oil'),
        (r'коензим|coenzyme|coq10', 'Coenzyme','Coenzyme Q10'),
        (r'пробиот|lactobacillus|bifidobacterium', 'Probiotics',''),
        (r'холин|choline', 'Choline',''), (r'мелатонин|melatonin', 'melatonin','')]:
        if re.search(pattern,text): return group, sub
    for key, aliases in ALIASES.items():
        if any(t and t in text for t in [norm(key)]+[norm(a) for a in aliases.split('|')]):
            return 'Plant-based Ingredients', key
    return '', ''


def build_row(p, template, config):
    brand_key=norm(p['brand'])
    intentionally_blank=any(brand_key==norm(b) or brand_key.startswith(norm(b)+' ') for b in config.get('blank_brand_and_manufacturer',[]))
    override = config.get('product_overrides', {}).get(p['reference'], {}) or config.get('product_overrides', {}).get(p['url'], {})
    cat, category_reason = category_for(p, template.categories, override.get('category'))
    row, issues = {'E':cat, 'F':template.categories[cat]}, []
    slug = p['id'] or hashlib.sha256(p['url'].encode()).hexdigest()[:12]
    sku = 'REV-'+slug+('-'+p['variant_id'] if p['variant_id']!='0' else '')
    row.update({'G':'Normal product','L':(p['title']+(' – '+p['variant_label'] if p['variant_label'] else ''))[:250],
                'M':'REV-'+slug,'N':sku,'O':'Add','T':p['description'], 'KC':'Size','KE':p['variant_label'] or 'One size',
                'LA':config['stock_if_available'] if p['stock']=='in' else 0,
                'LB':p['price'], 'LC':p['url'], 'LD':p['list_price'],
                'LF':config['default_weight_g'], 'LG':config['default_dimensions_cm'][0],
                'LH':config['default_dimensions_cm'][1], 'LI':config['default_dimensions_cm'][2],
                'LJ':'Single set','LK':'Yes','LL':1,'LM':'pack', 'LU':config.get('shipping_template',''),
                'LV':config.get('handling_time',''), 'LW':'I will ship this item myself',
                'HT':p['form'],'JZ':'Liquid' if p['form']=='Liquid' else 'Solid' if p['form'] in ('Gummies','Tablets','Capsules','Powder','Granules','Pellets','Tea Bag') else 'Not Applicable'})
    for col, url in zip([c for c,h in template.headers.items() if h=='Detail Images URL'],p['images']): row[col]=url
    for col, url in zip([c for c,h in template.headers.items() if h=='SKU Images URL'],p['images']): row[col]=url
    bullets = ['Марка: '+p['brand'],'Форма: '+p['form']]
    if p['count']:
        row.update({'LN':p['count'],'LO':'piece', 'LP':p['count'],'LQ':p['count'],'LR':'count'})
        bullets.append('Брой в опаковка: '+str(p['count']))
    elif p['net']:
        row.update({'LP':p['net'],'LQ':p['net'],'LR':p['net_unit']})
        bullets.append(f'Нетно съдържание: {p["net"]} {p["net_unit"]}')
    for c, b in zip(['U','V','W','X','Y','Z'], bullets): row[c]=b
    if p['best_before']: row['HP']=p['best_before']
    if p['ean'] and re.fullmatch(r'\d{12,14}',p['ean']):
        row['LS']='UPC' if len(p['ean'])==12 else 'EAN' if len(p['ean'])==13 else 'GTIN-14'
        row['LT']=p['ean']
    # Only assign the real matching manufacturer; a lone dropdown is not evidence.
    mapped = config.get('brand_manufacturers', {}).get(norm(p['brand']), '')
    if not mapped:
        opts = template.options('NY',row)
        for opt in opts:
            if norm(opt) and norm(opt) in norm(p['manufacturer_source']): mapped=opt;break
    row['NY']=mapped
    row['R']=template.choose('R',p['brand'],row)
    if row['R']:
        options = template.options('S',row)
        row['S']=options[0] if len(options)==1 else ''
    row['LY']=p['country_source']
    row['NZ']=config.get('eu_responsible_by_brand',{}).get(norm(p['brand']), '')
    used = set()
    for group_col, sub_col in [('HW','HX'),('IH','II'),('IS','IT'),('JD','JE'),('JO','JP')]:
        for text in p['ingredients']:
            group, sub = ingredient_group(text)
            if group and (group,sub) not in used:
                candidate=template.choose(group_col,group,row)
                if not candidate: continue
                row[group_col]=candidate
                if sub: row[sub_col]=template.choose(sub_col,sub,row)
                used.add((group,sub));break
    if p['form']=='Liquid':
        sugar=norm(' '.join(p['ingredients']))
        if any(x in sugar for x in ['захар','фруктоз','сукралоз','еритрит','стевия','sugar','sweetener']): row['KA']='Yes'
    # Product overrides use column names, never alter or expand account dropdowns.
    for c,v in override.get('columns',{}).items():
        if c not in template.headers: raise ValueError('Unknown override column: '+c)
        if c in ('E','F'): raise ValueError('Use category override, not column '+c)
        row[c]=v
    if intentionally_blank:
        # These products remain in the same export; only account fields are blank.
        row['R']=row['S']=row['NY']=''
    for c,v in list(row.items()):
        chosen=template.choose(c,v,row)
        if chosen=='' and v not in ('',None):
            issues.append('Dropdown mismatch '+c+' '+template.headers[c]+': '+str(v))
        row[c]=chosen
    missing=[]
    # Repeated attribute columns are alternate slots, not separate mandatory facts.
    seen_headers=set()
    for c in template.requirements.get(cat,[]):
        h=template.headers[c]
        if h in seen_headers:continue
        seen_headers.add(h)
        if not any(row.get(k) not in ('',None) for k,v in template.headers.items() if v==h): missing.append(c+' '+h)
    if missing: issues.append('Missing mandatory: '+'; '.join(missing))
    if intentionally_blank:
        issues.append('Brand and Manufacturer intentionally blank; included in common export')
    else:
        if not row.get('NY'): issues.append('Manufacturer not available/matched in account dropdown')
        if not row.get('R'): issues.append('Brand not present in account dropdown; source brand retained in title/description')
    if not row.get('NZ') and row.get('LY') not in ('Germany','Bulgaria'):
        issues.append('EU Responsible person not confirmed')
    if category_reason!='title' and category_reason!='override': issues.append('Category '+category_reason)
    if p['stock']=='unknown': issues.append('Availability unknown; exported stock 0')
    if p.get('variant_problem'): issues.append(p['variant_problem'])
    if not p['ingredients']: issues.append('Composition not found')
    if not p['directions']: issues.append('Directions not found')
    for c in ('LF','LG','LH','LI'):
        if c not in override.get('columns',{}): issues.append('Configured default '+c+'='+str(row[c]))
    return row, {'sku':sku,'reference':p['reference'],'url':p['url'],'brand':p['brand'],
                 'category':cat,'category_reason':category_reason,'stock':p['stock'],
                 'price_eur':row['LB'],'list_price_eur':row['LD'],'missing_required':'; '.join(missing),
                 'issues':' | '.join(issues),'requires_review':bool(issues)}


class Client:
    def __init__(self, delay=1.2, cache='cache', timeout=45, refresh=False):
        self.delay,self.timeout,self.refresh=delay,timeout,refresh
        self.cache=Path(cache);self.cache.mkdir(parents=True,exist_ok=True)
        self.opener=urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
        self.last=0

    def get(self, url, fresh=False):
        u=urllib.parse.urlsplit(url)
        if u.scheme!='https' or u.hostname not in ('revita.bg','www.revita.bg'):
            raise ValueError('Request outside allowed site')
        cache=self.cache/(hashlib.sha256(url.encode()).hexdigest()+'.html')
        if cache.exists() and not self.refresh and not fresh:return cache.read_bytes()
        error=None
        for attempt in range(4):
            time.sleep(max(0,self.delay-(time.monotonic()-self.last)))
            try:
                req=urllib.request.Request(url,headers={'User-Agent':'RevitaCatalogExporter/1.0 (product catalog export)', 'Accept':'text/html,application/json', 'Accept-Language':'bg-BG,bg;q=0.9', 'Accept-Encoding':'identity'})
                self.last=time.monotonic()
                with self.opener.open(req,timeout=self.timeout) as response:
                    if urllib.parse.urlsplit(response.url).hostname not in ('revita.bg','www.revita.bg'):
                        raise ValueError('Redirect outside allowed host')
                    body=response.read()
                lower=body[:60000].lower()
                if any(x in lower for x in [b'<title>just a moment',b'cf-chl-',b'challenge-platform',b'<title>access denied']):
                    raise ValueError('Verification/access-denied response: '+url)
                cache.write_bytes(body)
                return body
            except urllib.error.HTTPError as exc:
                error=exc
                if exc.code not in (429,500,502,503,504):break
                wait=exc.headers.get('Retry-After','')
                time.sleep(min(45,int(wait)) if wait.isdigit() else min(30,2**attempt*3))
            except (urllib.error.URLError,TimeoutError,OSError) as exc:
                error=exc;time.sleep(min(20,2**attempt*2))
        raise RuntimeError('Failed request '+url+': '+str(error))


def expand_variants(product, source, client):
    opts=product['variant_options']
    if not opts:return [product]
    count=1
    for values in opts.values():count*=len(values)
    if count>100:
        raise ValueError('Variant matrix exceeds 100; no silent truncation')
    doc=parse_doc(source)
    form=doc.xpath('//form[@id="add-to-cart-or-refresh"]')[0]
    params={e.get('name'):e.get('value','') for e in form.xpath('.//input[@type="hidden"]') if e.get('name')}
    result,seen=[],set()
    for combination in itertools.product(*opts.values()):
        selected=dict(zip(opts,combination))
        query={**params,**{k:v[0] for k,v in selected.items()},'ajax':'1','action':'refresh','qty':'1'}
        payload=json.loads(client.get(product['url']+'?'+urllib.parse.urlencode(query)))
        vid=str(payload.get('id_product_attribute') or '')
        if not vid:raise ValueError('Variant refresh returned no combination ID')
        if vid in seen:continue
        seen.add(vid)
        p=copy.deepcopy(product);p['variant_id']=vid;p['variant_label']=' / '.join(v[1] for v in selected.values())
        p['ean']=''  # A base barcode must never be assigned to all combinations.
        price_html=payload.get('product_prices','')
        if not price_html:raise ValueError('Variant refresh returned no price HTML')
        prices=parse_doc(price_html)
        price=money(first(prices,'.//*['+CLASS('product-price')+']/@content') or first(prices,'.//*['+CLASS('current-price')+']'))
        sign=first(prices,'.//*['+CLASS('product-price')+']/@data-currency_sign')
        if sign not in ('€','EUR') and '€' not in prices.text_content():raise ValueError('Variant EUR price not confirmed')
        if price is None:raise ValueError('Variant price missing')
        p['price']=price;p['list_price']=money(first(prices,'.//*['+CLASS('regular-price')+']/@data-product_regular_price') or first(prices,'.//*['+CLASS('regular-price')+']')) or price
        add=payload.get('product_add_to_cart','')
        if add:
            buttons=parse_doc(add).xpath('.//button[@data-button-action="add-to-cart"]')
            p['stock']='in' if buttons and buttons[0].get('disabled') is None else 'out'
        else:p['stock']='unknown'
        reference=payload.get('product_details','')
        if reference:
            detail=parse_doc(reference)
            p['reference']=first(detail,'.//*['+CLASS('product-reference')+']//span') or p['reference']
            p['ean']=first(detail,'.//*[@itemprop="gtin13" or @itemprop="gtin12" or @itemprop="gtin14"]/@content')
        p['variant_problem']='Variant label, composition and package quantity need review against the label'
        if payload.get('product_cover_thumbnails'):
            cover=parse_doc(payload['product_cover_thumbnails'])
            urls=cover.xpath('.//img/@data-image-large-src')
            if urls:p['images']=list(dict.fromkeys(urllib.parse.urljoin(BASE,u) for u in urls))
        result.append(p)
    return result


def write_csv(path, rows, fields):
    with Path(path).open('w',encoding='utf-8-sig',newline='') as f:
        writer=csv.DictWriter(f,fieldnames=fields,extrasaction='ignore');writer.writeheader();writer.writerows(rows)


def main(argv=None):
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--config',default='config.json');ap.add_argument('--template')
    ap.add_argument('--output',default='output');ap.add_argument('--limit',type=int,default=0)
    ap.add_argument('--delay',type=float,default=1.2);ap.add_argument('--refresh',action='store_true')
    ap.add_argument('--offline',action='store_true',help='Use bundled fixtures, with ZERO network requests')
    args=ap.parse_args(argv)
    if args.limit<0:ap.error('--limit must be 0 or a positive number')
    logging.basicConfig(level=logging.INFO,format='%(asctime)s %(levelname)s %(message)s')
    config=json.loads(Path(args.config).read_text(encoding='utf-8'))
    template=Template(args.template or config['template'])
    output=Path(args.output);output.mkdir(parents=True,exist_ok=True)
    for old in output.glob('TEMU_REVITA_*.xlsx'):old.unlink()
    errors,products,links,coverage=[],[],{},[]
    if args.offline:
        hints,pages,_=parse_listing(Path('tests/fixtures/category.html').read_bytes(),BASE+'/alive')
        fixture=Path('tests/fixtures/product.html').read_bytes()
        url=first(parse_doc(fixture),'//link[@rel="canonical"]/@href')
        products=[parse_product(fixture,url,hints.get(url),'Alive')]
        coverage=[{'category':'alive','pages':1,'products_found':len(hints),'status':'OFFLINE: one supplied product only'}]
    else:
        client=Client(args.delay,refresh=args.refresh)
        for category in CATEGORIES:
            start=BASE+'/'+category;queue=[start];visited=set();signatures=set();category_urls=set();reported=None
            while queue:
                url=queue.pop(0)
                if url in visited:continue
                if len(visited)>=500:
                    errors.append({'stage':'category','url':start,'error':'Pagination exceeded 500 pages'});break
                visited.add(url)
                try:
                    hints,pages,total=parse_listing(client.get(url),start)
                    signature=tuple(sorted(hints))
                    if signature in signatures:raise ValueError('Repeated listing page; pagination may be blocked/ignored')
                    signatures.add(signature)
                    if total:reported=total
                    for link,hint in hints.items():
                        category_urls.add(link)
                        if link not in links:links[link]={'hint':hint,'category':category}
                        elif hint.get('best_before') and not links[link]['hint'].get('best_before'):links[link]['hint']['best_before']=hint['best_before']
                    queue += [p for p in pages if p not in visited and p not in queue]
                    LOG.info('%s page %s: %s products',category,len(visited),len(hints))
                except Exception as exc:
                    errors.append({'stage':'category','url':url,'error':str(exc)});LOG.error('%s',exc)
            if reported and len(category_urls)<reported:
                errors.append({'stage':'category','url':start,'error':f'Found {len(category_urls)}, reported {reported}'})
            coverage.append({'category':category,'pages':len(visited),'products_found':len(category_urls),'reported_total':reported,'status':'ERROR' if any(e['stage']=='category' and canonical(e['url'])==start for e in errors) else 'OK'})
        targets=list(links.items())[:args.limit or None]
        for index,(url,info) in enumerate(targets,1):
            try:
                source=client.get(url)
                product=parse_product(source,url,info['hint'],info['category'])
                products.extend(expand_variants(product,source,client))
                LOG.info('Product %s/%s: %s',index,len(targets),product['title'])
            except Exception as exc:
                errors.append({'stage':'product','url':url,'error':str(exc)});LOG.error('%s',exc)
    rows,reports=[],[]
    seen=set()
    for p in products:
        key=(p['id'] or p['url'],p['variant_id'])
        if key in seen:continue
        seen.add(key)
        try:
            row,report=build_row(p,template,config);rows.append(row);reports.append(report)
        except Exception as exc:
            errors.append({'stage':'mapping','url':p['url'],'error':str(exc)})
    # Every run publishes diagnostic artifacts, including failed/partial runs.
    for index in range(0,len(rows),4996):
        template.write(rows[index:index+4996],output/f'TEMU_REVITA_{index//4996+1:03d}.xlsx')
    (output/'catalog.json').write_text(json.dumps(products,ensure_ascii=False,indent=2),encoding='utf-8')
    write_csv(output/'review.csv',reports,['sku','reference','url','brand','category','category_reason','stock','price_eur','list_price_eur','missing_required','issues','requires_review'])
    write_csv(output/'errors.csv',errors,['stage','url','error'])
    (output/'coverage.json').write_text(json.dumps(coverage,ensure_ascii=False,indent=2),encoding='utf-8')
    (output/'discovered_urls.json').write_text(json.dumps(links,ensure_ascii=False,indent=2),encoding='utf-8')
    summary={'mode':'offline fixture' if args.offline else 'live','timestamp':datetime.now(timezone.utc).isoformat(),
             'discovered_unique_products':len(links),'exported_rows':len(rows),'errors':len(errors),
             'rows_requiring_review':sum(r['requires_review'] for r in reports),'limited_run':bool(args.limit),
             'complete_crawl':not args.offline and not args.limit and not errors and bool(rows),
             'upload_ready':bool(rows) and not errors and not any(r['requires_review'] for r in reports)}
    (output/'summary.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2),encoding='utf-8')
    LOG.info('Exported %s rows; %s errors; %s rows require review',len(rows),len(errors),summary['rows_requiring_review'])
    return 1 if errors or not rows else 0


if __name__=='__main__':
    raise SystemExit(main())
