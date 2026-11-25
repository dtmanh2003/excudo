"""外部通信せず表記ゆれを吸収し、本文全体が挨拶だけか保守的に判定する。"""
from dataclasses import dataclass
import re
import unicodedata

from .prompts import build_greeting_prompt


_GREETING = re.compile(
    r"(?:こん(?:にち|ち|ばん)[はわ][ぁあ]*|(?:こん|(?:や)?ばん)ちゃ(?:っす)?|"
    r"おはよ[うぅおぉ]*(?:ございます)?|や[あぁ]+|やっ?ほ[おぉ]*|"
    r"おっ?す+|ちわ(?:っす)?|どうも|"
    r"he+l{2,}o+|h+i+|he+y+|good\s*(?:morning|afternoon|evening)|"
    r"ko+n+(?:i(?:chi|ti)(?:wa|ha)|cha))"
)
_SEPARATOR = re.compile(r"[\s!。.,、?…・]+")


def is_plain_greeting(message: str) -> bool:
    # 長さは難易度判定に使わず、長大な入力の正規化・探索を避ける上限だけ設ける。
    if len(message) > 256:
        return False
    text = unicodedata.normalize("NFKC", message).casefold()
    normalized = []
    for character in text:
        # カタカナ・半角や挨拶の伸ばし方を吸収する。AIへ送る原文は変更しない。
        if "ァ" <= character <= "ヶ":
            character = chr(ord(character) - 0x60)
        if character in "ー~〜":
            continue
        # 絵文字は区切り扱い。依頼文の文字や数字は消さず、必ず判定対象に残す。
        if unicodedata.category(character) in {"So", "Sk"} or character == "\ufe0f":
            character = " "
        normalized.append(character)
    text = "".join(normalized).strip()
    # 文全体を消費できる場合だけ成功。後ろに未知の語や依頼が残れば通常処理する。
    position = 0
    found = False
    while position < len(text):
        separator = _SEPARATOR.match(text, position)
        if separator:
            position = separator.end()
            continue
        greeting = _GREETING.match(text, position)
        if greeting is None:
            return False
        found = True
        position = greeting.end()
    return found


@dataclass(frozen=True)
class RequestOptions:
    """最初の応答に適用する設定。既定値は通常のモデル設定を維持する。"""

    thinking: bool | None = None
    offer_tools: bool = True
    prompt_suffix: str = ""


GREETING_SUFFIX = build_greeting_prompt("")


# 難しい依頼の目安：原因・設計・比較・不具合の調査、コードやエラーの貼り付け、長い依頼。
_DIFFICULT = re.compile(
    r"なぜ|どうして|原因|設計|比較|違い|最適|アルゴリズム|証明|計算|分析|解析|レビュー|バグ|デバッグ|不具合|"
    r"エラー|例外|リファクタ|検討|方針|トレードオフ|"
    r"\bwhy\b|explain|design|compare|debug|\bbug|error|exception|traceback|prove|analy[sz]|optimi[sz]|refactor|"
    r"algorithm|trade-?off|"
    r"tại sao|vì sao|so sánh|thiết kế|phân tích|tối ưu|lỗi",
    re.IGNORECASE,
)


def thinking_for(user_input: str) -> bool | None:
    """この依頼全体で思考を使うか。既定（auto）は難しい依頼だけ。思考なしの方が速く答える。

    EXCUDO_THINKING=server はモデルサーバーの既定に任せる（思考の設定を送らない）。
    """
    from . import config
    if config.THINKING in {"1", "true", "yes", "on"}:
        return True
    if config.THINKING in {"0", "false", "no", "off"}:
        return False
    if config.THINKING == "server":
        return None
    text = user_input or ""
    return bool("```" in text or len(text) >= 400 or text.count("\n") >= 6 or _DIFFICULT.search(text))


def options_for(user_input: str) -> RequestOptions:
    if is_plain_greeting(user_input):
        return RequestOptions(thinking=False, offer_tools=False, prompt_suffix=GREETING_SUFFIX)
    return RequestOptions()
