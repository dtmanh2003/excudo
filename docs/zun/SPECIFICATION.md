# Excudo - モジュール間共通仕様書 (`SPECIFICATION.md`)

ブランチ: `zun_loc` ／ 2026-10-07 時点の実装に基づく

## 1. 概要

本ドキュメントは、ローカル LLM（vLLM 上の Qwen3）を使うエージェント「Excudo」の**モジュール間インターフェース、共通データ型、各層の責任範囲**を定義する。記載内容はすべて現在のコードと一致している（PR / compare 時の確認用）。

---

## 2. システムアーキテクチャと依存関係

依存は一方向（上位層 → 下位層）。下位層は UI ライブラリを参照せず、Harness は**イベントを返すジェネレータ**で UI に通知する。承認だけは PermissionGate のコールバックで UI に問い合わせる。

```text
[ UI Layer ]  main.py (CLI / Rich) ・ excudo/app (デスクトップアプリ / pywebview)
      │  user_input ／ イベント受領 (Iterator[dict])
      ▼
[ Harness Layer ]  harness.py ・ messages.py ──(OpenAI 互換 API)──▶ [ Model Layer ] model.py
      │  ツール呼出 ＋ 権限判定
      ▼
[ Tools & Safety Layer ]  tools.py ・ permissions.py ・ security.py
      │
[ Persistence ]  session.py (.excudo/sessions/)
```

| モジュール | 責任 | 依存してよい先 |
| --- | --- | --- |
| `main.py` / `excudo/app` | 入力、表示、承認 UI | Harness、Model、PermissionGate、session、i18n |
| `harness.py` | エージェントループ、会話履歴、圧縮 | messages、model、permissions、security、tools |
| `model.py` | LLM との通信のみ | messages |
| `tools.py` | ツールの実装と登録 | config、security、skills |
| `permissions.py` / `security.py` | 権限判定、サンドボックス、秘密情報のマスク | config |

---

## 3. 共通データ型

### 3.1 メッセージ（`excudo/messages.py`）

OpenAI Chat Completions 形式の dict。Excudo 独自の情報は `metadata` に入れ、**API 送信前に `to_api()` で除去**する。

```python
Role = Literal["system", "user", "assistant", "tool"]
FinishReason = Literal["completed", "max_turns", "interrupted", "error"]


class MessageMetadata(TypedDict, total=False):
    created_at: str                                # ISO 8601
    turn: int                                      # ユーザーターン番号 (1 始まり)
    tokens: int                                    # トークン数の概算
    finish_reason: FinishReason                    # ターン最後のメッセージにのみ付与
    tool_call_source: Literal["native", "text"]    # ツール要求の取得方法
    kind: Literal["summary", "note"]               # 圧縮で生成された system メッセージ


class ToolCall(TypedDict):
    id: str
    type: Literal["function"]
    function: dict                                 # {"name": str, "arguments": str (JSON)}


class Message(TypedDict):
    role: Role
    content: str | None
    tool_calls: NotRequired[list[ToolCall]]        # assistant のツール要求
    tool_call_id: NotRequired[str]                 # tool 結果と要求の対応付け
    metadata: NotRequired[MessageMetadata]


def make_message(role: Role, content: str | None, *, turn: int | None = None, **fields) -> Message: ...
def to_api(messages: list[dict]) -> list[dict]: ...      # metadata を除いたコピー
def estimate_tokens(message: dict) -> int: ...
def total_tokens(messages: list[dict]) -> int: ...
```

> `tool_calls` / `tool_call_id` は vLLM の function calling に必須（ないとツール使用後のリクエストがエラーになる）。

### 3.2 ツール（`excudo/tools.py`）

```python
@dataclass
class Tool:
    name: str                       # ツール識別子
    description: str                # LLM に提示する説明文
    parameters: dict                # JSON Schema (type / properties / required)
    handler: Callable[..., str]     # 実行関数。結果は必ず str で返す

    def schema(self) -> dict: ...   # OpenAI function calling 形式に変換


REGISTRY: dict[str, Tool]           # 全ツールの登録先 (Harness が参照)
```

承認の要否は `Tool` には持たせず、`permissions.TOOL_RISK_MAP` で一元管理する（3.3）。

### 3.3 権限（`excudo/permissions.py`）

```python
class ActionRisk(Enum):
    SAFE = "safe"                            # 読み取りのみ → 自動許可
    MODIFY = "modify"                        # ファイル変更 → 確認 (/auto 時は自動許可)
    EXECUTE = "execute"                      # コマンド実行 → 常に確認
    EXTERNAL = "external"                    # 社外への送信 → 常に確認
    SENSITIVE = "sensitive"                  # 機密ファイル → 常に確認
    BLOCKED = "blocked"                      # 危険なコマンド → 常に拒否
    OUTSIDE_WORKSPACE = "outside_workspace"  # ワークスペース外 → 常に拒否


@dataclass
class PermissionResult:
    allowed: bool
    reason: str
    risk: ActionRisk


# UI 側の承認コールバック: (ツール名, 引数, リスク) -> 許可なら True
ApprovalCallback = Callable[[str, dict, ActionRisk], bool]

TOOL_RISK_MAP: dict[str, ActionRisk]         # ツールごとの基本リスク (未登録は EXECUTE)


class PermissionGate:
    def __init__(self, workspace_root: Path | None = None, auto_approve: bool | None = None,
                 approval_callback: ApprovalCallback | None = None, audit_log: Path | None = None): ...
    def check(self, tool_name: str, args: dict) -> PermissionResult: ...
    def begin_turn(self) -> None: ...        # ターン開始時に Harness が呼ぶ
    def mark_untrusted(self) -> None: ...    # 外部データ受信後、そのターンの自動承認を停止


def is_within_workspace(path_str: str, workspace_root: Path | None = None) -> bool: ...
```

### 3.4 Harness → UI イベント

`Harness.run()` が yield する dict。UI はこれだけを見て描画する。

| `type` | フィールド | タイミング |
| --- | --- | --- |
| `reasoning_delta` | `text: str` | 思考の断片を受信 |
| `answer_delta` | `text: str` | 回答の断片を受信 |
| `tool_call` | `name: str`, `args: dict`, `result: str` | ツールの実行（または拒否）が完了 |
| `error` | `content: str` | 通信エラー、最大ステップ数到達 |
| `done` | `reason: FinishReason` | **常に最後**に 1 回 |

### 3.5 セッション（`excudo/session.py`）

```python
@dataclass
class Session:
    id: str              # YYYYMMDD_HHMMSS_<6桁16進>。英数字・"_"・"-" のみ許可
    created_at: str
    updated_at: str
    title: str
    history: list[dict]  # Message のリスト (metadata 含む)

def save_session(session: Session) -> Path: ...
def load_session(session_id: str) -> Session | None: ...      # 完全一致 or 一意な前方一致
def delete_session(session_id: str) -> str | None: ...
def list_sessions() -> list[Session]: ...
def get_latest_session() -> Session | None: ...
```

---

## 4. 役割別詳細仕様

### 役割 1: Harness（`excudo/harness.py`）

#### 責任範囲
- **Model との境界:** Model にはメッセージ（`to_api()` 済み）とツール定義だけを渡す。
- **会話状態管理:** 履歴（`list[Message]`）を一元管理し、圧縮・要約する。
- **制御ループ:** ストリーミング → ツール検出 → `PermissionGate.check()` → 実行 → 結果を履歴に追加 → 再要求。

#### 公開クラス定義

```python
class Harness:
    def __init__(self, model: Model, system_prompt: str,
                 permission_gate: PermissionGate | None = None): ...

    @property
    def history(self) -> list[dict]: ...                 # コピーを返す
    def reset(self) -> None: ...
    def load_history(self, history: list[dict]) -> None: ...   # system は現在のプロンプトに置換
    def compact_history(self) -> None: ...               # 40 件 or 4 万トークン超で古い会話を要約
    def run(self, user_input: str) -> Iterator[dict]: ...      # 1 ターン実行、イベントを yield (3.4)
```

#### ループの規則
- 最大 `MAX_TURNS`（既定 15）回まで繰り返す。ツール要求がなければ完了（`done: completed`）。
- ツール要求の取得: ネイティブ function calling（`index` ごとに断片を連結）。本文に `<tool_call>` タグが残った場合のみテキスト解析で補完。
- エラーで停止しない。不正な JSON・未登録ツール・拒否・例外は、理由を `str` で `tool` メッセージとしてモデルに返す。
- ツール結果は秘密情報をマスク（`security.redact_secrets`）し、履歴には最大 2 万文字で保存する。
- Ctrl+C / 中断時: 未実行のツール要求に「中断」結果を補い、`finish_reason = "interrupted"` を記録する。

### 役割 2: Model（`excudo/model.py`）

```python
class Model:
    thinking_enabled: bool
    def stream(self, messages: list[dict], tools: list[dict]) -> Iterator: ...   # エージェントループ用
    def complete(self, messages: list[dict], tools: list[dict]): ...
    def generate_response(self, messages: list[dict], timeout: float = 120) -> str: ...  # テキストのみ (要約用)
```

- 3 メソッドとも送信前に `metadata` を除去する。思考モードは `chat_template_kwargs.enable_thinking` で切り替える。

### 役割 3: Tools & Safety（`tools.py` / `permissions.py` / `security.py`）

#### 責任範囲
- ツールの実装と `REGISTRY` への登録。
- **安全なエラーハンドリング:** 例外を外に出さず、原因を `str` で返す。
- **権限判定:** すべてのツール呼出は Harness から `PermissionGate.check()` を通る。判定順は「ワークスペース外 → 危険コマンド → リスク分類 → 自動承認 → UI 承認」。

#### ツール一覧（17 種）

| ツール | 主な引数 | リスク |
| --- | --- | --- |
| `read_file` | `path`, `offset`, `limit` | SAFE |
| `list_dir` | `path`, `max_depth` | SAFE |
| `grep_search` | `query`, `path`, `is_regex` | SAFE |
| `write_file` | `path`, `content` | MODIFY |
| `edit_file` | `path`, `target`, `replacement` | MODIFY |
| `delete_file` | `path` | MODIFY |
| `run_command` | `command`, `timeout`, `input_text` | EXECUTE |
| `open_terminal` / `run_terminal_command` / `send_terminal_input` / `close_terminal` | `path`・`terminal_id`・`command` など | EXECUTE |
| `read_terminal` / `list_terminals` | `terminal_id` | SAFE |
| `web_search` | `query` | EXTERNAL |
| `load_skill` | `name` | SAFE |
| `parse_receipt` | `image_path` | SAFE（社内 LLM サーバーにのみ送信） |
| `save_expense_record` | `record`, `image_path` | MODIFY |

※ 機密ファイルへのパスは SENSITIVE、ネットワークを使うコマンド（curl、git push など）は EXTERNAL に引き上げる。

#### サンドボックス（`is_within_workspace`）

```python
def is_within_workspace(path_str: str, workspace_root: Path | None = None) -> bool:
    root = Path(workspace_root).resolve()
    target = Path(path_str)
    if not target.is_absolute():
        target = root / target                      # 相対パスはワークスペース基準
    return target.resolve().is_relative_to(root)    # .. ・シンボリックリンク・ジャンクションを解決して判定
```

- `path` と `*_path` の全引数が対象。ワークスペース外は確認なしで拒否（`/auto` でも不可）。
- `startswith` による判定は使わない（`C:\proj2` が `C:\proj` 配下と誤判定されるため）。

---

## 5. 結合判定・ルール確認項目 (Checklist)

PR 作成・承認時に以下を確認する。

- [ ] すべてのツールの handler は、例外発生時もクラッシュせず `str` を返却しているか。
- [ ] 新しいツールを追加した場合、`TOOL_RISK_MAP` にリスクを登録しているか（社外送信は `EXTERNAL`）。
- [ ] ツールは Harness 経由（`PermissionGate.check()` を通過）でのみ実行されているか。
- [ ] Harness・Model・Tools が UI ライブラリ（rich・pywebview 等）を import していないか。
- [ ] メッセージは `make_message()` で作成し、API へは `to_api()` 済みのものを送っているか。
- [ ] UI は `Harness.run()` のイベント（3.4）だけで描画し、`done` で 1 ターンの終了を判定しているか。
- [ ] `uv run pytest` がすべて成功しているか。
