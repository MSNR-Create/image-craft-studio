@echo off
chcp 65001 >nul
echo Image Craft Studio — 依存パッケージのインストール
echo ================================================
pip install PyQt6 Pillow numpy apng
echo.
echo インストール完了。「起動.bat」を実行してください。
pause
