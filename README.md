# Image Craft Studio

画像を分割線で複数の領域に切り出し、個別画像や APNG アニメーションとして書き出す Windows 用デスクトップアプリ（PyQt6）。

## 主な機能

- 分割線による領域分割（枠ごとの個別調整、Shift+ドラッグで2本線化）
- 複数画像のタブ管理
- 消しゴム・スポイトによる領域ごとの編集
- 連番ファイル名での一括書き出し、バッチ処理
- APNG 作成（FPS / 合計時間 / ループ回数指定）

## 動作環境

- Windows 11
- Python 3.10 以上

## セットアップと起動

```bat
install_deps.bat
起動.bat
```

または:

```bash
pip install PyQt6 Pillow numpy apng
python image_craft_studio.py
```

## exe ビルド

```bash
pyinstaller image_craft_studio.spec
```

`dist/image_craft_studio.exe` が生成されます。
