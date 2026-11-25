"""UI language settings and UI strings.

The strings live in excudo/locales/<language>.json (en.json is complete; other files may
leave strings out, which are then shown in English). Adding a language means adding one
file; no code change is needed.

The UI starts in the language of the OS (or EXCUDO_LANG). Message content
does not change the display language. Unsupported OS languages fall back to English.
"""

import json
import locale
import os
from pathlib import Path
import sys

_LOCALES_DIR = Path(__file__).with_name("locales")


def _load_locales() -> dict[str, dict[str, str]]:
    """Every locale file as {language code: {key: text}}; codes are lower case (zh-tw)."""
    return {path.stem.lower(): json.loads(path.read_text(encoding="utf-8"))
            for path in sorted(_LOCALES_DIR.glob("*.json"))}


_LOCALES = _load_locales()
# {key: {language: text}}: every key of en.json, with the languages that translate it.
MESSAGES: dict[str, dict[str, str]] = {
    key: {lang: texts[key] for lang, texts in _LOCALES.items() if key in texts}
    for key in _LOCALES["en"]
}
_current = "en"

def resolve_language(code: str | None) -> str | None:
    """The locale for a language code such as "zh_TW.UTF-8", "zh-TW" or "ja": the exact
    region first, then the base language. None when no locale file matches."""
    if not code:
        return None
    normalized = code.split(".", 1)[0].replace("_", "-").lower()
    for candidate in (normalized, normalized.split("-", 1)[0]):
        if candidate in _LOCALES:
            return candidate
    return None


def _os_language() -> str | None:
    """The OS UI language as a code such as "ja-JP"; None when it cannot be read."""
    if sys.platform == "win32":
        try:
            import ctypes

            # The display language (not the date/number format), e.g. 0x0411 → ja_JP.
            return locale.windows_locale.get(ctypes.windll.kernel32.GetUserDefaultUILanguage())
        except (AttributeError, OSError):
            pass
    return locale.getlocale()[0]


def system_language() -> str:
    """EXCUDO_LANG if it names a locale, otherwise the OS UI language, otherwise English."""
    forced = resolve_language(os.getenv("EXCUDO_LANG", ""))
    if forced:
        return forced
    return resolve_language(_os_language()) or "en"


def get_language() -> str:
    return _current


def set_language(lang: str) -> None:
    """Switch to `lang` (or its base language). A language without a locale file is ignored."""
    global _current
    resolved = resolve_language(lang)
    if resolved:
        _current = resolved


def t(key: str, **kwargs) -> str:
    """The UI string `key` in the current language (falls back to English)."""
    entry = MESSAGES[key]
    text = entry.get(_current) or entry["en"]
    return text.format(**kwargs) if kwargs else text


def strings() -> dict[str, str]:
    """Every UI string in the current language, for the desktop page."""
    return {key: t(key) for key in MESSAGES}
