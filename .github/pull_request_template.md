## 何を変えたか / What this changes

<!-- 1〜3 行で。関連する Issue があれば "Closes #123" -->

## なぜ / Why

<!-- 困っていた状況。再現手順があれば -->

## 実機で確認したこと / What you actually ran

<!--
「たぶん動く」ではなく、**実際に通したもの**を書いてください。
このプロジェクトは推測で直して壊した実績があるので、動かした証拠を重視します。
環境（GPU と VRAM / OS / ComfyUI のバージョンと --reserve-vram の有無）も書いてください。
-->

- 環境:
- 通した操作:
- 結果:

---

## チェックリスト / Checklist

- [ ] `python tools/make_example_config.py --check` が通る（`config.example.json` は手書きせず自動生成）
- [ ] `python tools/sync_vendor.py --check` が通る（正本が無い配布環境では何もせず正常終了します）
- [ ] `python tools/validate_graph.py` が通る（ComfyUI を起動している場合のみ。GPU は使いません）
- [ ] **ローカルの絶対パスを持ち込んでいない**（環境依存は全部 `config.json`。コードにパスを書かない）
- [ ] **改行は LF**（`.gitattributes` で強制していますが、Python から書いたファイルは `newline="\n"` を明示したか確認してください。既定で CRLF に化けた実績があります）
- [ ] 画面や README に**数値**（速度・VRAM・尺）を足した場合、それは**自分の環境の実測値**であり、どの環境のものかを明記した

---

## `vendor/system_h3.txt` に触れる変更（LLM のシステムプロンプト）

**この 1 ファイルだけは、bench で回帰を測るまでマージしません。**

<!-- 触れていない場合は、この節ごと消してかまいません -->

- [ ] `vendor/system_h3.txt` に触れた

### なぜ 56 本 × 2 なのか

**1 回 28 本の合格数で判定してはいけません。** 同じ版でも実行ごとに一発率が ±3 ぶれます
（LM Studio の投機デコードにより `seed` が再現性を与えないため）。
シードを変えて 2 回・**通算 56 本**回し、確定的な根拠にできるのは **合格数の低下** と **error の増加** だけです。

### 結果 / Bench results

| 回 | seed | 合格 / 本数 | error 件数 |
|---|---|---|---|
| 1 回目（変更前） |  |  /28 |  |
| 1 回目（変更後） |  |  /28 |  |
| 2 回目（変更前） |  |  /28 |  |
| 2 回目（変更後） |  |  /28 |  |
| **通算（変更後）** | — | **  /56** |  |

使ったモデル / 実効 ctx（`lms ps` の値）:

<!--
bench のスクリプトは配布物に含まれていません（開発側のリポジトリにあります）。
手元で回せない場合はこの表を空のまま残し、その旨を書いてください。**メンテナ側で回してから判断します。**
The bench harness is not shipped with this repository. If you cannot run it,
leave the table empty and say so — a maintainer will run it before merging.
-->

- [ ] 変更前の版を `system_h3_v<版>_<何を足したか>.txt` として退避してある
      （正本と配布コピーが 1 操作で同時に上書きされる構造のため、退避しないと検証済みの版が**どこにも残りません**。実際に消失した事故があります）

---

## 取り込みについて / How this gets merged

このリポジトリは**配布用のコピー**です。開発の正本は別にあり、
外部から来た PR はメンテナが**正本側へ反映して実機で確認してから**取り込みます。
そのため、マージまでに時間がかかることと、取り込みの形が変わることがあります。

This repository is a distribution copy; development happens against a separate source of truth.
Incoming PRs are replayed there and verified on real hardware before merging.
