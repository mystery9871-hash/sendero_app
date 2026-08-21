"""
Multi-source content import — ported from the desktop app's "Import Content"
feature. Pulls from Wikipedia, Project Gutenberg, Open Library, Internet
Archive, Library of Congress newspapers, and Google News, all via plain
stdlib urllib — no extra pip dependencies, so nothing here can reintroduce
the build problems earlier dependencies caused.

Not ported from desktop: the Wikidata quiz-generation and Wikipedia
category-sweep modes. Both are advanced, less-used opt-ins on top of this
same machinery — worth adding later if wanted, but out of scope for the
first mobile pass.
"""
import json
import re
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

from shared_utils import (
    sanitize_imported_text, slugify, fetch_mymemory_translation, translate_text_to_english, urlopen,
)

USER_AGENT = "Sendero-Learning-App/1.0 (personal mobile learning app)"

SKIP_SECTIONS = {
    "see also", "references", "further reading", "external links", "notes",
    "bibliography", "sources", "citations", "gallery", "footnotes",
    "notes and references", "external links and references",
}
_INTRO = "__intro__"

GUTENBERG_START_RE = re.compile(
    r"\*\*\*\s*START OF (?:THE|THIS) PROJECT GUTENBERG EBOOK.*?\*\*\*",
    re.IGNORECASE | re.DOTALL,
)
GUTENBERG_END_RE = re.compile(
    r"\*\*\*\s*END OF (?:THE|THIS) PROJECT GUTENBERG EBOOK.*",
    re.IGNORECASE | re.DOTALL,
)
CHAPTER_HEADING_RE = re.compile(
    r"^[ \t]*(chapter|book|part|letter|canto|section)\b.*$",
    re.IGNORECASE | re.MULTILINE,
)


def _get_json(url, timeout=15, error_label="request"):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raise ValueError(f"{error_label} failed (HTTP {e.code}).")
    except urllib.error.URLError as e:
        raise ValueError(f"Couldn't reach the internet: {e.reason}")
    except TimeoutError:
        raise ValueError("Request timed out — check your connection and try again.")


# --------------------------------------------------------------- Wikipedia --
def fetch_wikipedia_article(topic):
    params = {"action": "query", "format": "json", "prop": "extracts",
              "explaintext": 1, "redirects": 1, "titles": topic}
    url = "https://en.wikipedia.org/w/api.php?" + urllib.parse.urlencode(params)
    data = _get_json(url, error_label=f"Wikipedia request for '{topic}'")

    pages = data.get("query", {}).get("pages", {})
    page = next(iter(pages.values()), None)
    if page is None or "missing" in page:
        raise ValueError(f"No Wikipedia page found for '{topic}'.")

    title = page.get("title", topic)
    full_text = (page.get("extract") or "").strip()
    if not full_text:
        raise ValueError(f"No article text found for '{topic}'.")
    full_text = sanitize_imported_text(full_text)

    safe_title = urllib.parse.quote(title.replace(" ", "_"))
    page_url = f"https://en.wikipedia.org/wiki/{safe_title}"
    return title, full_text, page_url


def split_into_sections(full_text):
    pattern = re.compile(r"^(={2,6})\s*(.+?)\s*\1\s*$", re.MULTILINE)
    matches = list(pattern.finditer(full_text))
    if not matches:
        return [(_INTRO, full_text.strip())]

    sections = []
    intro = full_text[: matches[0].start()].strip()
    if intro:
        sections.append((_INTRO, intro))
    for i, m in enumerate(matches):
        heading = m.group(2).strip()
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(full_text)
        body = full_text[start:end].strip()
        sections.append((heading, body))
    return sections


def build_lessons_from_wikipedia(topic, start_id, max_lessons_per_topic=6):
    title, full_text, page_url = fetch_wikipedia_article(topic)
    sections = split_into_sections(full_text)
    attribution = f"Source: Wikipedia — {page_url} (text under CC BY-SA 4.0)."

    lessons = []
    next_id = start_id
    for heading, body in sections:
        if heading.strip().lower() in SKIP_SECTIONS:
            continue
        body = body.strip()
        if len(body.split()) < 30:
            continue
        if len(body) > 3500:
            cutoff = body.rfind(". ", 0, 3500)
            body = body[: cutoff + 1] if cutoff != -1 else body[:3500]
            body += " [continues in the full article]"

        lesson_title = title if heading == _INTRO else f"{title}: {heading}"
        first_sentence = body.split(". ")[0].strip()
        if not first_sentence.endswith("."):
            first_sentence += "."
        lessons.append({
            "id": next_id, "title": lesson_title, "summary": first_sentence,
            "body": f"{body}\n\n{attribution}",
        })
        next_id += 1
        if len(lessons) >= max_lessons_per_topic:
            break

    if not lessons:
        lessons.append({
            "id": next_id, "title": title,
            "summary": full_text.split(". ")[0].strip() + ".",
            "body": f"{full_text.strip()}\n\n{attribution}",
        })
    return lessons


# --------------------------------------------------------------- Gutenberg --
def fetch_gutenberg_metadata(query):
    url = "https://gutendex.com/books/?search=" + urllib.parse.quote(query.strip())
    data = _get_json(url, error_label=f"Gutenberg lookup for '{query}'")

    results = data.get("results", [])
    if not results:
        raise ValueError(f"No Project Gutenberg book found for '{query}'.")

    book = results[0]
    title = book.get("title", query).strip()
    authors = book.get("authors", [])
    author = authors[0].get("name", "") if authors else ""
    languages = book.get("languages") or ["en"]
    language = languages[0] if languages else "en"

    formats = book.get("formats", {})
    text_url = None
    for mime, link in formats.items():
        if mime.startswith("text/plain") and not link.endswith(".zip"):
            text_url = link
            break
    if not text_url:
        raise ValueError(f"'{title}' has no plain-text version available on Gutenberg.")
    return title, author, text_url, book.get("id"), language


def fetch_gutenberg_text(text_url):
    req = urllib.request.Request(text_url, headers={"User-Agent": USER_AGENT})
    try:
        with urlopen(req, timeout=25) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as e:
        raise ValueError(f"Couldn't download the book text (HTTP {e.code}).")
    except urllib.error.URLError as e:
        raise ValueError(f"Couldn't reach the internet: {e.reason}")
    except TimeoutError:
        raise ValueError("Request timed out — check your connection and try again.")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        text = raw.decode("latin-1")
    return sanitize_imported_text(text)


def strip_gutenberg_boilerplate(text):
    m = GUTENBERG_START_RE.search(text)
    if m:
        text = text[m.end():]
    m = GUTENBERG_END_RE.search(text)
    if m:
        text = text[: m.start()]
    return text.strip()


def split_into_chapters(text, max_chapters=20):
    candidates = []
    for m in CHAPTER_HEADING_RE.finditer(text):
        line_start = text.rfind("\n", 0, m.start()) + 1
        line_end = text.find("\n", m.end())
        if line_end == -1:
            line_end = len(text)
        line = text[line_start:line_end].strip()
        if len(line) <= 60 and len(line.split()) <= 6:
            candidates.append((line_start, line))

    if len(candidates) < 2:
        words = text.split()
        if not words:
            return []
        chunk_size = max(600, (len(words) // max_chapters) + 1)
        chunks = [" ".join(words[i:i + chunk_size]) for i in range(0, len(words), chunk_size)]
        return [(f"Part {i + 1}", chunk) for i, chunk in enumerate(chunks[:max_chapters])]

    sections = []
    for i, (line_start, heading) in enumerate(candidates):
        content_start = text.find("\n", line_start) + 1
        content_end = candidates[i + 1][0] if i + 1 < len(candidates) else len(text)
        body = text[content_start:content_end].strip()
        if body:
            sections.append((heading, body))
    return sections[:max_chapters]


def build_lessons_from_gutenberg(query, start_id, max_lessons=20, auto_translate=False):
    title, author, text_url, gid, language = fetch_gutenberg_metadata(query)
    raw_text = fetch_gutenberg_text(text_url)
    clean_text = strip_gutenberg_boilerplate(raw_text)
    chapters = split_into_chapters(clean_text, max_chapters=max_lessons)

    translate_from = language if (auto_translate and language and language != "en") else None

    display_title = title
    if translate_from:
        try:
            display_title = fetch_mymemory_translation(title, translate_from)
        except ValueError:
            pass

    book_url = f"https://www.gutenberg.org/ebooks/{gid}" if gid else "https://www.gutenberg.org"
    by_line = f" by {author}" if author else ""
    attribution = f"Source: Project Gutenberg — {title}{by_line} ({book_url}), public domain in the US."
    if translate_from:
        attribution += (
            f" Machine-translated from {translate_from} to English via MyMemory (source language "
            f"detected automatically from Gutenberg's catalog) — automated translation, so expect "
            f"some rough or imprecise wording."
        )

    lessons = []
    next_id = start_id
    for heading, body in chapters:
        body = body.strip()
        if len(body.split()) < 50:
            continue
        if len(body) > 4000:
            cutoff = body.rfind(". ", 0, 4000)
            body = body[: cutoff + 1] if cutoff != -1 else body[:4000]
            body += " [continues in the full text]"

        if translate_from:
            try:
                body = translate_text_to_english(body, translate_from)
            except ValueError as e:
                body += f"\n\n[Translation from {translate_from} failed for this section ({e}) — showing original text.]"

        lesson_title = f"{display_title}: {heading}" if heading else display_title
        first_sentence = body.split(". ")[0].strip()
        if not first_sentence.endswith("."):
            first_sentence += "."
        lessons.append({
            "id": next_id, "title": lesson_title, "summary": first_sentence,
            "body": f"{body}\n\n{attribution}",
        })
        next_id += 1
        if len(lessons) >= max_lessons:
            break

    if not lessons:
        fallback_body = clean_text[:4000].strip()
        if translate_from:
            try:
                fallback_body = translate_text_to_english(fallback_body, translate_from)
            except ValueError as e:
                fallback_body += f"\n\n[Translation from {translate_from} failed for this section ({e}) — showing original text.]"
        lessons.append({
            "id": next_id, "title": display_title,
            "summary": fallback_body.split(". ")[0].strip() + ".",
            "body": f"{fallback_body}\n\n{attribution}",
        })
    return lessons


# --------------------------------------------------------------- Open Library --
def fetch_openlibrary_subject_titles(subject_query, limit=12):
    slug = slugify(subject_query)
    url = f"https://openlibrary.org/subjects/{slug}.json?limit={limit}"
    try:
        data = _get_json(url, error_label=f"Open Library lookup for '{subject_query}'")
    except ValueError:
        raise ValueError(f"No Open Library subject found for '{subject_query}'.")

    works = data.get("works", [])
    titles, seen = [], set()
    for work in works:
        title = (work.get("title") or "").strip()
        key = title.lower()
        if title and key not in seen:
            titles.append(title)
            seen.add(key)
    if not titles:
        raise ValueError(f"Open Library has no books listed for '{subject_query}'.")
    return titles


def build_lessons_from_openlibrary_subject(
    subject_query, start_id, max_books=3, max_lessons_per_book=6, auto_translate=False
):
    candidate_titles = fetch_openlibrary_subject_titles(subject_query, limit=max_books * 5)

    lessons = []
    books_found = 0
    next_id = start_id
    for title in candidate_titles:
        if books_found >= max_books:
            break
        try:
            book_lessons = build_lessons_from_gutenberg(
                title, start_id=next_id, max_lessons=max_lessons_per_book, auto_translate=auto_translate
            )
        except ValueError:
            continue
        lessons.extend(book_lessons)
        next_id += len(book_lessons)
        books_found += 1

    if not lessons:
        raise ValueError(
            f"None of Open Library's top matches for '{subject_query}' were available "
            f"as public-domain text on Project Gutenberg."
        )
    return lessons


# --------------------------------------------------------------------- News --
def fetch_news_headlines(subject_query, limit=6):
    query = urllib.parse.quote(subject_query.strip())
    url = f"https://news.google.com/rss/search?q={query}&hl=en-US&gl=US&ceid=US:en"
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urlopen(req, timeout=15) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as e:
        raise ValueError(f"News lookup failed for '{subject_query}' (HTTP {e.code}).")
    except urllib.error.URLError as e:
        raise ValueError(f"Couldn't reach the internet: {e.reason}")
    except TimeoutError:
        raise ValueError("Request timed out — check your connection and try again.")

    try:
        root = ET.fromstring(raw)
    except ET.ParseError:
        raise ValueError(f"Couldn't read news results for '{subject_query}'.")

    items = []
    for item in root.findall(".//item")[:limit]:
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        pub_date = (item.findtext("pubDate") or "").strip()
        source_el = item.find("source")
        source_name = source_el.text.strip() if source_el is not None and source_el.text else ""
        if title and link:
            items.append({"title": title, "link": link, "pub_date": pub_date, "source": source_name})

    if not items:
        raise ValueError(f"No recent news found for '{subject_query}'.")
    return items


def build_news_lesson(subject_query, lesson_id, limit=6):
    items = fetch_news_headlines(subject_query, limit=limit)
    lines = []
    for it in items:
        date_part = f" ({it['pub_date']})" if it["pub_date"] else ""
        source_part = f" — {it['source']}" if it["source"] else ""
        lines.append(f"• {sanitize_imported_text(it['title'])}{source_part}{date_part}\n  {it['link']}")

    body = (
        f'Recent headlines mentioning "{subject_query}", via Google News. Only titles, sources, '
        f"and links are kept here — follow a link to read the full article on the original site.\n\n"
        + "\n\n".join(lines)
    )
    return {
        "id": lesson_id, "title": f"{subject_query}: Current News",
        "summary": f'{len(items)} recent headline(s) related to "{subject_query}".',
        "body": body,
    }


# ---------------------------------------------------------- Internet Archive --
def fetch_archive_org_items(subject_query, limit=5):
    query = f"({subject_query}) AND mediatype:(texts) AND year:[1000 TO 1928]"
    params = [
        ("q", query), ("fl[]", "identifier"), ("fl[]", "title"), ("fl[]", "creator"),
        ("fl[]", "year"), ("rows", str(limit)), ("page", "1"), ("output", "json"),
        ("sort[]", "downloads desc"),
    ]
    url = "https://archive.org/advancedsearch.php?" + urllib.parse.urlencode(params)
    data = _get_json(url, timeout=20, error_label=f"Internet Archive search for '{subject_query}'")

    docs = data.get("response", {}).get("docs", [])
    if not docs:
        raise ValueError(f"No public-domain Internet Archive texts found for '{subject_query}'.")
    return docs


def fetch_archive_org_text(identifier):
    url = f"https://archive.org/download/{identifier}/{identifier}_djvu.txt"
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urlopen(req, timeout=30) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as e:
        raise ValueError(f"No plain-text version available for this item (HTTP {e.code}).")
    except urllib.error.URLError as e:
        raise ValueError(f"Couldn't reach the internet: {e.reason}")
    except TimeoutError:
        raise ValueError("Request timed out — check your connection and try again.")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        text = raw.decode("latin-1")
    return sanitize_imported_text(text)


def build_lessons_from_archive_org(subject_query, start_id, max_items=2, max_lessons_per_item=6):
    docs = fetch_archive_org_items(subject_query, limit=max_items * 4)

    lessons = []
    next_id = start_id
    items_used = 0
    for doc in docs:
        if items_used >= max_items:
            break
        identifier = doc.get("identifier")
        if not identifier:
            continue
        try:
            raw_text = fetch_archive_org_text(identifier)
        except ValueError:
            continue
        clean_text = strip_gutenberg_boilerplate(raw_text)
        sections = split_into_chapters(clean_text, max_chapters=max_lessons_per_item)
        if not sections:
            continue

        title = doc.get("title") or identifier
        creator = doc.get("creator", "")
        if isinstance(creator, list):
            creator = creator[0] if creator else ""
        year = doc.get("year", "")
        item_url = f"https://archive.org/details/{identifier}"
        by_line = f" by {creator}" if creator else ""
        year_part = f" ({year})" if year else ""
        attribution = (
            f"Source: Internet Archive — {title}{by_line}{year_part} ({item_url}), "
            f"public domain (published before 1929). Text is raw OCR from a scanned copy "
            f"and may contain scanning errors."
        )

        item_lessons = 0
        for heading, body in sections:
            body = body.strip()
            if len(body.split()) < 50:
                continue
            lessons.append({
                "id": next_id, "title": (f"[Archive] {title}: {heading}")[:120],
                "summary": f'From "{title}"{by_line}{year_part}, via the Internet Archive.',
                "body": f"{body}\n\n{attribution}",
            })
            next_id += 1
            item_lessons += 1
        if item_lessons:
            items_used += 1

    if not lessons:
        raise ValueError(
            f"Found Internet Archive matches for '{subject_query}', but none had usable plain-text versions."
        )
    return lessons


# ------------------------------------------------- Library of Congress --
def fetch_chronicling_america(subject_query, limit=5):
    params = {"andtext": subject_query.strip(), "format": "json", "rows": str(limit)}
    url = "https://chroniclingamerica.loc.gov/search/pages/results/?" + urllib.parse.urlencode(params)
    data = _get_json(url, timeout=20, error_label=f"Library of Congress search for '{subject_query}'")

    items = data.get("items", [])
    if not items:
        raise ValueError(f"No Chronicling America newspaper pages found for '{subject_query}'.")
    return items


def build_lessons_from_loc(subject_query, start_id, limit=5):
    items = fetch_chronicling_america(subject_query, limit=limit)
    lessons = []
    next_id = start_id
    for item in items:
        ocr_text = (item.get("ocr_eng") or "").strip()
        if not ocr_text or len(ocr_text.split()) < 50:
            continue
        ocr_text = sanitize_imported_text(ocr_text)

        title = item.get("title") or "Untitled newspaper"
        date = item.get("date") or ""
        date_fmt = f"{date[:4]}-{date[4:6]}-{date[6:8]}" if len(date) == 8 else date
        page_url = item.get("url") or item.get("id") or ""

        if len(ocr_text) > 4000:
            cutoff = ocr_text.rfind(". ", 0, 4000)
            ocr_text = ocr_text[: cutoff + 1] if cutoff != -1 else ocr_text[:4000]
            ocr_text += " [continues on the full scanned page]"

        lesson_title = f"{title} ({date_fmt})" if date_fmt else title
        first_sentence = ocr_text.split(". ")[0].strip()
        if not first_sentence.endswith("."):
            first_sentence += "."
        attribution = (
            f"Source: Library of Congress, Chronicling America — {page_url} "
            f"(public domain, U.S. historic newspaper). This is raw OCR text from a "
            f"scanned page and may contain scanning errors or garbled words."
        )
        lessons.append({
            "id": next_id, "title": (f"[Newspaper] {lesson_title}")[:120], "summary": first_sentence,
            "body": f"{ocr_text}\n\n{attribution}",
        })
        next_id += 1

    if not lessons:
        raise ValueError(f"Found Chronicling America pages for '{subject_query}', but none had usable OCR text.")
    return lessons


# --------------------------------------------------------- Combined search --
def build_lessons_from_all_sources(subject_query, start_id, max_books=2, max_archive_items=1, auto_translate=False):
    combined = []
    failures = []
    next_id = start_id

    try:
        wiki_lessons = build_lessons_from_wikipedia(subject_query, start_id=next_id)
        for l in wiki_lessons:
            l["title"] = f"[Wiki] {l['title']}"
        combined.extend(wiki_lessons)
        next_id += len(wiki_lessons)
    except ValueError as e:
        failures.append(f"Wikipedia ({e})")

    try:
        book_lessons = build_lessons_from_openlibrary_subject(
            subject_query, start_id=next_id, max_books=max_books, auto_translate=auto_translate
        )
        for l in book_lessons:
            l["title"] = f"[Book] {l['title']}"
        combined.extend(book_lessons)
        next_id += len(book_lessons)
    except ValueError as e:
        failures.append(f"Books ({e})")

    try:
        archive_lessons = build_lessons_from_archive_org(subject_query, start_id=next_id, max_items=max_archive_items)
        combined.extend(archive_lessons)
        next_id += len(archive_lessons)
    except ValueError as e:
        failures.append(f"Internet Archive ({e})")

    try:
        loc_lessons = build_lessons_from_loc(subject_query, start_id=next_id, limit=2)
        combined.extend(loc_lessons)
        next_id += len(loc_lessons)
    except ValueError as e:
        failures.append(f"Library of Congress ({e})")

    try:
        news_lesson = build_news_lesson(subject_query, lesson_id=next_id)
        news_lesson["title"] = f"[News] {news_lesson['title']}"
        combined.append(news_lesson)
        next_id += 1
    except ValueError as e:
        failures.append(f"News ({e})")

    if not combined:
        raise ValueError(f"'{subject_query}': " + "; ".join(failures))
    return combined, failures


def dedupe_new_lessons(existing_lessons, new_lessons):
    seen_titles = {(l.get("title") or "").strip().lower() for l in existing_lessons}
    kept = []
    skipped = 0
    for l in new_lessons:
        key = (l.get("title") or "").strip().lower()
        if key and key in seen_titles:
            skipped += 1
            continue
        if key:
            seen_titles.add(key)
        kept.append(l)
    return kept, skipped
