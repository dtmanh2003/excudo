"""Tests for UI language settings and strings."""

import json
import string

import pytest

from excudo import i18n


@pytest.fixture
def extra_locale(monkeypatch):
    # A language with a partial locale file, as a newly added translation would be.
    locales = {**i18n._LOCALES, "zh": {"app_send": "发送"}}
    monkeypatch.setattr(i18n, "_LOCALES", locales)
    monkeypatch.setattr(i18n, "MESSAGES", {
        key: {lang: texts[key] for lang, texts in locales.items() if key in texts}
        for key in locales["en"]
    })
    yield
    i18n.set_language("en")


@pytest.mark.parametrize(
    "code, expected",
    [
        ("ja", "ja"), ("ja_JP", "ja"), ("ja-JP", "ja"), ("vi_VN.UTF-8", "vi"), ("EN-us", "en"),
        ("zh_TW", None), ("ko", None), ("", None), (None, None),
    ],
)
def test_resolve_language_uses_region_then_base_language(code, expected):
    assert i18n.resolve_language(code) == expected


def test_resolve_language_prefers_the_exact_region(monkeypatch):
    monkeypatch.setattr(i18n, "_LOCALES", {**i18n._LOCALES, "zh": {}, "zh-tw": {}})
    assert i18n.resolve_language("zh_TW") == "zh-tw"
    assert i18n.resolve_language("zh_CN") == "zh"


@pytest.mark.parametrize(("os_code", "expected"), [("ja_JP", "ja"), ("vi_VN", "vi"), ("fr_FR", "en"), (None, "en")])
def test_system_language_follows_os_with_english_fallback(monkeypatch, os_code, expected):
    monkeypatch.delenv("EXCUDO_LANG", raising=False)
    monkeypatch.setattr(i18n, "_os_language", lambda: os_code)
    assert i18n.system_language() == expected


def test_new_locale_file_is_used_and_missing_strings_fall_back_to_english(monkeypatch, extra_locale):
    monkeypatch.setenv("EXCUDO_LANG", "zh_CN")
    i18n.set_language(i18n.system_language())
    assert i18n.get_language() == "zh"
    assert i18n.t("app_send") == "发送"
    assert i18n.t("app_stop") == "Stop"
    assert i18n.strings()["app_send"] == "发送"


def test_english_has_every_key_and_others_only_known_keys():
    keys = set(i18n._LOCALES["en"])
    assert {"en", "ja", "vi"} <= set(i18n._LOCALES)
    for lang, texts in i18n._LOCALES.items():
        assert set(texts) <= keys, lang


def test_translations_keep_the_same_placeholders():
    # A missing or renamed {placeholder} would break t(..., **kwargs) at run time.
    def fields(text):
        return {name for _, name, _, _ in string.Formatter().parse(text) if name}

    for key, english in i18n._LOCALES["en"].items():
        for lang, texts in i18n._LOCALES.items():
            if key in texts:
                assert fields(texts[key]) == fields(english), (lang, key)


def test_locale_files_are_valid_utf8_json():
    for path in i18n._LOCALES_DIR.glob("*.json"):
        data = json.loads(path.read_text(encoding="utf-8"))
        assert all(isinstance(value, str) and value for value in data.values()), path.name


def test_t_formats_and_switches():
    i18n.set_language("vi")
    assert i18n.t("app_done", secs=1, tools=2) == "1s · 2 lần dùng tool"
    i18n.set_language("en")
    assert i18n.t("app_done", secs=1, tools=2) == "1s · 2 tool(s)"
