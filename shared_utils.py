"""Pure-stdlib helpers shared by main.py and content_sources.py.

Kept in their own module (rather than living in main.py) so content_sources.py
can import them without creating a circular import — main.py imports content
fetchers from content_sources.py, and content_sources.py needs these in turn.
"""
import json
import re
import ssl
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request

# Older python-for-android builds often can't locate the system's CA
# certificate bundle, which makes every HTTPS request fail even on a
# perfectly good connection — the failure looks identical to "no internet"
# from urllib's point of view. certifi ships its own CA bundle as plain
# data, so this sidesteps the missing-system-certs problem entirely.
try:
    import certifi
    SSL_CONTEXT = ssl.create_default_context(cafile=certifi.where())
except ImportError:
    SSL_CONTEXT = None


def urlopen(req, timeout=15):
    return urllib.request.urlopen(req, timeout=timeout, context=SSL_CONTEXT)


def slugify(name):
    slug = re.sub(r"[^a-z0-9]+", "_", name.strip().lower()).strip("_")
    return slug or "subject"


def sanitize_imported_text(text):
    """Strip invisible control/format characters that render as tofu boxes,
    and tidy up whitespace — ported as-is from the desktop app."""
    text = unicodedata.normalize("NFKC", text)
    kept = []
    for ch in text:
        if ch in ("\n", "\t", " "):
            kept.append(ch)
            continue
        if ch == "\ufffd":
            continue
        category = unicodedata.category(ch)
        if category in ("Cc", "Cf", "Co", "Cs"):
            continue
        if category in ("Zl", "Zp"):
            kept.append("\n")
            continue
        kept.append(ch)
    text = "".join(kept)
    text = text.replace("\u00a0", " ")
    text = re.sub(r"[ \t]{2,}", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


# ---------------------------------------------------------------------------
# Translation (MyMemory's free API, stdlib urllib only — no extra deps)
# ---------------------------------------------------------------------------

def fetch_mymemory_translation(text, source_lang, target_lang="en"):
    langpair = f"{source_lang}|{target_lang}"
    url = "https://api.mymemory.translated.net/get?" + urllib.parse.urlencode(
        {"q": text, "langpair": langpair}
    )
    req = urllib.request.Request(
        url, headers={"User-Agent": "Sendero-Learning-App/1.0 (personal mobile learning app)"},
    )
    try:
        with urlopen(req, timeout=20) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raise ValueError(f"Translation request failed (HTTP {e.code}).")
    except urllib.error.URLError as e:
        raise ValueError(f"Couldn't reach the translation service: {e.reason}")
    except TimeoutError:
        raise ValueError("Translation request timed out — check your connection and try again.")

    status = data.get("responseStatus")
    if str(status) != "200":
        detail = data.get("responseDetails", "unknown error")
        raise ValueError(f"Translation service error: {detail}")
    translated = data.get("responseData", {}).get("translatedText", "").strip()
    if not translated:
        raise ValueError("Translation returned no text.")
    return translated


def _split_into_translation_chunks(text, max_chars=450):
    sentences = re.split(r"(?<=[.!?])\s+", text.strip())
    chunks = []
    current = ""
    for sentence in sentences:
        if len(sentence) > max_chars:
            if current:
                chunks.append(current)
                current = ""
            for i in range(0, len(sentence), max_chars):
                chunks.append(sentence[i:i + max_chars])
            continue
        if current and len(current) + len(sentence) + 1 > max_chars:
            chunks.append(current)
            current = sentence
        else:
            current = f"{current} {sentence}".strip()
    if current:
        chunks.append(current)
    return [c for c in chunks if c.strip()]


def translate_text_to_english(text, source_lang, max_chunks=40):
    if not text.strip():
        return text
    chunks = _split_into_translation_chunks(text)
    truncated = len(chunks) > max_chunks
    chunks = chunks[:max_chunks]
    translated_parts = []
    for i, chunk in enumerate(chunks):
        translated_parts.append(fetch_mymemory_translation(chunk, source_lang))
        if i < len(chunks) - 1:
            time.sleep(0.4)
    result = " ".join(translated_parts)
    if truncated:
        result += " [translation stops here — the rest is untranslated in the original language]"
    return result
