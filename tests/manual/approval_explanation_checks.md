# 承認目的の補足・実端末確認

`C:\Users\kodamaky\Desktop\excudo-approval-explanations` で `uv run main.py` を起動する。
今回の表示整理後のWindows実端末確認は未実施。以前の表示の確認結果とは区別する。

1. `bash_execで echo hi を実行して。目的は実行機能の確認です。`
   - コマンドが上側、目的（AIの説明）と短い注意書きが下側に表示される。
   - purposeが引数一覧と重複せず、操作・影響の定型文は表示されない。
   - 半角y＋Enterで実行。n・空Enterでは拒否。Ctrl+Cで入力へ復帰する。
2. `write_fileで approval-review-demo.txt に hello と保存して。`
   - 新規作成の内容と既存の作業ディレクトリが表示され、目的未指定の説明は増えない。
3. `edit_fileで approval-review-demo.txt の hello を goodbye に変えて。`
   - 既存の変更差分が表示される。nで拒否すると元の内容を維持する。
4. 承認画面が出る前に端末の幅を狭め、長い日本語の目的が末尾まで折り返されることを確認する。

外部内容後の半角yes必須・危険な操作の拒否・削除警告・マスキングは自動テストでも確認する。
#61のマージを反映し、通常承認は全角ｙ・ＹＥＳも受け付ける。外部内容後の追加確認は従来どおり半角yesのみ。
Mac/Linux実端末の目視は未実施。実モデルでの確認はユーザーが実施する。
