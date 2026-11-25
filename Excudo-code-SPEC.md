# Excudo CLI / App エージェント - 全体設計仕様書 (`SPEC.md`)

## 1. 概要とアーキテクチャ

本ドキュメントは、ローカル LLM（vLLM 上の Qwen3）を活用するコーディングエージェント「Excudo」の全体設計仕様書である。CLI およびデスクトップアプリの 2 つの UI モジュールと、経費精算機能（Keichan）を備える。

### モジュール構造とレイヤー関係

1. **UI / UX Layer** (`main.py` / `excudo/app`)
   - 画面表示、ユーザー入力受付、イベント受信
2. **Harness Layer** (`excudo/harness.py` / `excudo/messages.py`)
   - イベント制御、会話状態管理、Model呼び出し
3. **Model Layer** (`excudo/model.py`)
   - vLLM Engine との通信
4. **Tools & Safety Layer** (`excudo/tools.py` / `excudo/permissions.py` / `excudo/security.py`)
   - 各種ツールの実行、権限判定、セキュリティ制御

- **依存の一方向性**: 上位層が下位層を呼び出し、Harness や Tools は UI ライブラリ（Rich 等）を直接 import しない。
- **イベント駆動・ジェネレータ構造**: Harness はイベントを返すジェネレータとして動作し、CLI とデスクトップアプリで全く同じ Harness ループを共有する。

---

## 2. モジュール間共通データ定義・状態管理 (State)

### 2.1 メッセージ型構造 (`excudo/messages.py`)

OpenAI 互換形式の dict に対し、Excudo 独自の `metadata` を保持する。

- **role**: system / user / assistant / tool
- **content**: 本文テキスト、または `text` / `image_url` パートの配列。画像はMIME付きのbase64 data URLで送信する。
- **tool_calls**: ツール要求オブジェクト（assistant のみ）
- **tool_call_id**: 対応するツール要求 ID（tool のみ）
- **metadata**: 内部状態データ（LLM 送信前に除外する）

#### metadata の内部構造
- **created_at**: 作成日時
- **turn**: 実行ターン数
- **tokens**: 概算トークン数
- **finish_reason**: ターンの終了理由（ターン最後のメッセージに記録）
- **tool_call_source**: ツール要求の取得方式（native / text）
- **kind**: 圧縮メッセージの種類（summary / note）

※ 思考ログ（reasoning）は画面ストリーミング表示のみに使用し、会話履歴へは保持しない。

#### Python CLI の画像添付

- `excudo/attachments.py` が送信前の画像をメモリー内で管理する。`/attach`、`/attachments`、`/detach 番号`、`/detach *` の操作方法は README の「画像を添付して質問する」を参照。
- `/attach` の1行目だけをパスとして解析し、2行目以降は質問として同じリクエストで送信する。失敗時はパスを含むエラーを表示し、選択中の画像を変更せず、質問も送信しない。
- 静止画 PNG・JPEG・WebP、入力20 MiB・1600万画素まで。長辺1280pxに縮小後、1枚4 MiB・合計8 MiB・4枚まで。
- ファイルツールと同じ `workspace_path` / `regular_file` の制約を適用する。画像を含むメッセージは `metadata.untrusted` を付ける。
- 次の質問時には過去の画像を最新の1枚だけ残し、新しい画像を送る際は過去の画像をすべてファイル名のテキストに置き換える。保存履歴には画像データが含まれ、既存の16 MiB保存上限も適用される。

### 2.2 セッション保存・圧縮仕様 (`excudo/session.py`)

- **保存場所**: `.excudo/sessions/<ID>.json`（metadata も含めて各ターン終了時に自動保存）
- **セッション ID**: 英数字・`_`・`-` のみ許可（ディレクトリトラバーサルを防止）
- **履歴圧縮（要約）ルール**:
  - **発動条件**: 履歴が 40 件を超える、または概算 40,000 トークンを超えた場合（新しい質問の送信直前に判定）
  - **残存データ**: システムプロンプト ＋ 直近会話（最大 38 件・上限の半分のトークン量まで）
  - **旧会話の処理**: `generate_response` を用いて要約し、1 件の system メッセージとして格納

  ---

## 3. 役割別詳細仕様

### 3.1 Model 層 (`excudo/model.py`)
- **責任範囲**: LLM との通信に専念し、ファイル操作やコマンド実行は行わない。
- **主要メソッド**:
  - `generate_response(messages) -> str`: 単一応答（履歴要約等で使用）
  - `stream(messages, tools)`: トークン単位のストリーミング応答（エージェントループで使用）
  - `complete(messages, tools)`: 一括応答
- **送信制約**: リクエスト送信前に Excudo 独自の `metadata` を必ず除去する。

### 3.2 Harness 層 (`excudo/harness.py`)
- **制御ループ**: 最大 `MAX_TURNS`（既定 15）回のくり返し処理を実施。
- **終了判定 (`done` イベントおよび `finish_reason`)**:
  - `completed`: モデルがツールを要求せず正常完了
  - `max_turns`: `MAX_TURNS` 到達による上限エラー
  - `interrupted`: ユーザー中断（未実行ツールに「中断」結果を補い履歴整合性を保持）
  - `error`: 通信・モデル呼び出し例外

#### ツール呼び出しパース処理
1. **Native Function Calling**: `tool_calls` オブジェクトから取得。
2. **Text Parsing (予備)**: Qwen3 の `<tool_call>{"name": "...", "arguments": {...}}</tool_call>` タグをテキスト解析して取得。

### 3.3 Tools & Safety 層 (`excudo/tools.py`, `permissions.py`, `security.py`)
- **提供ツール**:
  - **ファイル**: `read_file`, `write_file`, `edit_file`, `delete_file`, `list_dir`, `grep_search`
  - **コマンド**: `run_command`, ターミナル操作 (`open_terminal` 等)
  - **検索**: `web_search` (DuckDuckGo → Wikipedia → Stack Overflow)
  - **その他**: `load_skill`, `parse_receipt`, `save_expense_record`

#### 権限およびサンドボックス制御 (`permissions.py`)
- **読み取り (`read_file`等)**: 自動許可
- **ファイル作成・編集 (`write_file`等)**: 確認要 (`/auto` 時は自動承認)
- **ファイル削除 (`delete_file` / `DELETE`)**: `/auto on` でも毎回確認。外部の内容を読んだ後は、CLIで `yes` の入力、デスクトップで追加の確認チェックが必要
- **コマンド実行・社外送信**: **常に確認** (`/auto` でも省略不可)
- **機密ファイル (`.env`, 鍵, 設定等)**: **常に確認** (`grep_search` 対象外)
- **危険コマンド・領域外アクセス**: **常に拒否**

#### 安全機構 (`security.py`)
- **マスキング**: ツール出力内の API キー・秘密鍵を `[REDACTED]` に置き換える。
- **プロンプトインジェクション対策**: 外部検索結果に注記を付与し、受領ターンでは `/auto` を一時停止する。
- **監査ログ**: 承認および拒否操作を `.excudo/audit.log` へ書き込む。

### 3.4 UI & UX 層 (`excudo/ui.py`, `terminal_ui.py`, `cli_controller.py`, `app`)
- **責任範囲**: 状態や操作内容の視覚化。**承認判断やファイル保存などの処理ロジックは保持しない**。

#### ファイルと役割の境界
- `excudo/ui.py`: 色・枠・余白・表示文言の設定（ロジックを含めない）
- `excudo/terminal_ui.py`: Rich によるストリーミングおよび TUI 描画
- `excudo/cli_controller.py`: 表示データの組み立てと承認入力解釈

#### 表示・承認フロー仕様
- **カラーリング**: 主色 cyan、入力 blue、完了 green、確認 yellow、エラー red、本文 white。
- **ストリーミング**: `answer_delta` / `reasoning_delta` をバッファへ蓄積し描画（20回/秒更新）。生成完了時に最終プレビューを消去し、全文を一括確定表示する。
- **承認パネル**: 承認が必要な操作時に Rich Panel を表示。CLI・デスクトップとも通常の確認・コマンド実行は黄色、ファイル削除・外部送信・機密操作は赤色枠で識別する。削除には「削除すると元に戻せません」を表示する。`y`/`yes` で許可、空入力・`n`・Ctrl+C で拒否。
- **多言語対応**: 日本語 (`JA_MESSAGES`) / 英語 (`EN_MESSAGES`) の切り替えに対応（会話内容やモデル設定は保持）。

---

## 4. 経費精算モジュール (Keichan)

- **解析ロジック**: Qwen3 (vision) によりレシート画像を直接解析し、Markdown 表と JSON に変換。
- **保存・出力**:
  - レコード保存先: `経費精算/records/YYYY-MM.json`（重複チェック機能付き）
  - 書式出力: `経費精算/exports/` へ Excel 形式の経費申請書を出力。設定値は `経費精算/settings.json` に個別に管理。
