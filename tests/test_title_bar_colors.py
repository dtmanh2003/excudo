"""The Windows title bar continues the app's top bar: same colours as ui/style.css."""
import re

from excudo.app import studio_api


def test_colorref_puts_red_in_the_lowest_byte():
    assert studio_api._colorref("#112233") == 0x332211


def test_title_bar_uses_the_top_bar_colours_of_the_stylesheet():
    css = (studio_api.Path(studio_api.__file__).parent / "ui" / "style.css").read_text(encoding="utf-8")
    variables = dict(re.findall(r"--(bg|text)\s*:\s*(#[0-9a-fA-F]{6})\s*;", css))
    assert studio_api.TITLE_BAR_COLORS == {"caption": studio_api._colorref(variables["bg"]),
                                           "text": studio_api._colorref(variables["text"])}
