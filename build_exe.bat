@echo off
chcp 65001 >nul
cd /d "%~dp0"

echo ============================================
echo  Image Craft Studio — exe ビルド
echo ============================================
echo.

pyinstaller ^
  --name "ImageCraftStudio" ^
  --onedir ^
  --windowed ^
  --clean ^
  --noconfirm ^
  --add-data "." ^
  --hidden-import "PIL._tkinter_finder" ^
  --hidden-import "PyQt6.sip" ^
  --collect-all "PyQt6" ^
  --collect-all "PIL" ^
  --collect-all "numpy" ^
  --collect-all "apng" ^
  image_craft_studio.py

echo.
if exist "dist\ImageCraftStudio\ImageCraftStudio.exe" (
  echo ビルド成功！
  echo 配布フォルダ: dist\ImageCraftStudio\
  echo 起動ファイル: dist\ImageCraftStudio\ImageCraftStudio.exe
) else (
  echo ビルドに失敗しました。上のログを確認してください。
)
echo.
pause
