"""CLI承認の目的補足。実際の引数・差分や承認判定とは分離する。"""

from .security import redact_secrets


def approval_purpose(args: dict) -> str | None:
    """purpose引数があれば、秘密情報を伏せたAI由来の補足を返す。"""
    purpose = args.get("purpose")
    if not isinstance(purpose, str) or not purpose.strip():
        return None
    # モデルの説明を操作の根拠にしない。引数・差分や既存警告は呼び出し側で先に表示する。
    purpose = redact_secrets(purpose.strip())
    return (f"目的（AIの説明）：{purpose}\n"
            "※AIの説明は正確とは限りません。実際の引数や差分も確認してください。")
