"""
Image Craft Studio — AI生成画像の仕上げ・分割・規格化・APNG制作
Windows 11 スタンドアロン / Python + PyQt6
"""

APP_VERSION = "1.1.0"

import sys
import os
import json
import copy
import struct
import zlib
from pathlib import Path
from typing import Optional

from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QHBoxLayout, QVBoxLayout,
    QToolBar, QStatusBar, QDockWidget, QScrollArea, QLabel,
    QSplitter, QListWidget, QListWidgetItem, QSlider, QSpinBox,
    QDoubleSpinBox, QComboBox, QCheckBox, QPushButton, QGroupBox,
    QFileDialog, QMessageBox, QDialog, QFormLayout, QLineEdit,
    QTabWidget, QSizePolicy, QFrame, QColorDialog, QProgressDialog,
    QAbstractItemView, QMenu, QInputDialog, QToolButton,
    QGraphicsView, QGraphicsScene, QGraphicsPixmapItem, QGraphicsRectItem,
    QGraphicsItem, QRubberBand, QGridLayout, QButtonGroup, QRadioButton,
    QScrollBar, QDialogButtonBox, QTabBar, QStackedWidget
)
from PyQt6.QtCore import (
    Qt, QRectF, QPointF, QSizeF, QSize, QRect, QPoint,
    pyqtSignal, QThread, QObject, QTimer, QMimeData,
    QSettings, QByteArray
)
from PyQt6.QtGui import (
    QPixmap, QImage, QPainter, QPen, QBrush, QColor, QFont,
    QAction, QIcon, QKeySequence, QPainterPath, QCursor,
    QDragEnterEvent, QDropEvent, QWheelEvent, QMouseEvent,
    QTransform, QPolygonF
)

from PIL import Image, ImageEnhance, ImageFilter
import io


# ──────────────────────────────────────────────
# グローバル設定（線の色・太さ）
# ──────────────────────────────────────────────

LINE_SETTINGS: dict = {
    'split_color': '#2F6FEB',   # 分割線
    'split_alpha': 220,
    'split_width': 1,
    'margin_color': '#FFA500',  # 外周余白線
    'margin_alpha': 220,
    'margin_width': 1,
    'overlap_color': '#00DC64',  # 重複線
    'overlap_alpha': 220,
    'overlap_width': 1,
}

def _ls_pen(key_color: str, key_alpha: str, key_width: str,
            style=None) -> 'QPen':
    """LINE_SETTINGS から QPen を生成するヘルパー。"""
    c = QColor(LINE_SETTINGS[key_color])
    c.setAlpha(LINE_SETTINGS[key_alpha])
    pen = QPen(c, LINE_SETTINGS[key_width], style or Qt.PenStyle.SolidLine)
    pen.setCosmetic(True)
    return pen


# ──────────────────────────────────────────────
# Data model
# ──────────────────────────────────────────────

class Region:
    """1つの分割セルを表す"""
    def __init__(self, rid: int, x: int, y: int, w: int, h: int):
        self.id = rid
        self.crop = QRect(x, y, w, h)
        self.enabled = True
        self.name = f"{rid:02d}"
        # layout
        self.canvas_w = 512
        self.canvas_h = 512
        self.margin_top = 24
        self.margin_bottom = 24
        self.margin_left = 24
        self.margin_right = 24
        self.fit_mode = "Fit"   # Fit / Fill / Original
        self.anchor = "center"
        self.bg_color = QColor(0, 0, 0, 0)
        # transform
        self.flip_h = False
        self.flip_v = False
        self.rotation = 0
        # mask: stored as PIL image or None
        self._mask_pil: Optional[Image.Image] = None
        # eraser edit: RGBA PIL image replacing the raw crop, or None
        self._erased_pil: Optional[Image.Image] = None
        # user adjustments (from eraser preview dialog)
        self.user_scale: float = 1.0    # 拡大縮小倍率（render_region で適用）
        self.user_offset_x: int = 0     # 位置補正X px
        self.user_offset_y: int = 0     # 位置補正Y px
        # color
        self.brightness = 1.0
        self.contrast = 1.0
        self.saturation = 1.0

    def copy(self):
        r = Region(self.id, self.crop.x(), self.crop.y(), self.crop.width(), self.crop.height())
        r.__dict__.update({k: copy.deepcopy(v) for k, v in self.__dict__.items()
                           if k not in ('_mask_pil', '_erased_pil')})
        r._mask_pil = self._mask_pil.copy() if self._mask_pil else None
        r._erased_pil = self._erased_pil.copy() if self._erased_pil else None
        return r


class AnimFrame:
    """APNGの1フレーム"""
    def __init__(self, region_id: int, duration_ms: int = 100, doc=None):
        self.region_id = region_id
        self.duration_ms = duration_ms
        self.doc = doc       # Document参照（None = 保存時の現在ドキュメント）
        self.offset_x = 0
        self.offset_y = 0
        self.scale_w: float = 1.0
        self.scale_h: float = 1.0
        self.enabled = True


class Document:
    """1つの画像ドキュメント"""
    def __init__(self, source_path: str, pil_image: Image.Image):
        self.source_path = source_path
        self.source_pil = pil_image.convert("RGBA")
        self.regions: list[Region] = []
        self.anim_frames: list[AnimFrame] = []
        self.anim_canvas_w = 512
        self.anim_canvas_h = 512
        self.anim_loop = 0  # 0=infinite
        self.anim_bg = QColor(0, 0, 0, 0)
        # global output settings (defaults applied to new regions)
        self.out_w = 512
        self.out_h = 512
        self.out_fmt = "PNG"
        self.out_quality = 90
        self.save_dest = "same"  # same / custom
        self.save_dir = ""
        self.name_prefix = ""
        self.name_suffix = "_part"
        self.name_start = 1
        self.name_digits = 2
        self.timestamp = False


class CommandHistory:
    def __init__(self, limit=100):
        self._stack: list[tuple] = []
        self._pos = -1
        self._limit = limit

    def push(self, snapshot):
        self._stack = self._stack[:self._pos + 1]
        self._stack.append(snapshot)
        if len(self._stack) > self._limit:
            self._stack.pop(0)
        self._pos = len(self._stack) - 1

    def undo(self):
        if self._pos > 0:
            self._pos -= 1
            return self._stack[self._pos]
        return None

    def redo(self):
        if self._pos < len(self._stack) - 1:
            self._pos += 1
            return self._stack[self._pos]
        return None

    def can_undo(self): return self._pos > 0
    def can_redo(self): return self._pos < len(self._stack) - 1


# ──────────────────────────────────────────────
# Canvas: split line item
# ──────────────────────────────────────────────

class DocumentTab:
    """1タブ分の状態（ドキュメント・履歴・キャンバス分割状態）を保持する。"""
    def __init__(self, doc: 'Document'):
        self.doc = doc
        self.history = CommandHistory()
        self.vsegs: list = []
        self.hsegs: list = []
        self.vseg_pair: list = []   # _vseg_pair の保存用（ペア線右側X）
        self.hseg_pair: list = []   # _hseg_pair の保存用（ペア線下側Y）
        self.outer_bounds: tuple = (0, 0, 0, 0)
        self.overlap_x: int = 0
        self.overlap_y: int = 0
        self.split_settings: dict = {}   # SplitPanel の UI 値スナップショット


class SplitLineItem(QGraphicsItem):
    def __init__(self, is_horizontal: bool, pos_val: float, length: float, parent=None):
        super().__init__(parent)
        self.is_horizontal = is_horizontal
        self.pos_val = pos_val
        self.length = length
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsMovable)
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemSendsGeometryChanges)
        self.setCursor(Qt.CursorShape.SizeVerCursor if is_horizontal else Qt.CursorShape.SizeHorCursor)
        self._pen = QPen(QColor(0x2F, 0x6F, 0xEB, 180), 1, Qt.PenStyle.DashLine)

    def boundingRect(self):
        if self.is_horizontal:
            return QRectF(0, -4, self.length, 8)
        return QRectF(-4, 0, 8, self.length)

    def paint(self, painter, option, widget=None):
        painter.setPen(self._pen)
        if self.is_horizontal:
            painter.drawLine(QPointF(0, 0), QPointF(self.length, 0))
        else:
            painter.drawLine(QPointF(0, 0), QPointF(0, self.length))


class RegionRectItem(QGraphicsRectItem):
    def __init__(self, region: Region, canvas: 'CanvasView'):
        super().__init__()
        self.region = region
        self.canvas = canvas
        self._selected = False
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsSelectable)
        self._update_rect()
        self._update_pen()

    def _update_rect(self):
        r = self.region.crop
        self.setRect(QRectF(r.x(), r.y(), r.width(), r.height()))

    def _update_pen(self):
        color = QColor(0x2F, 0x6F, 0xEB) if self._selected else QColor(0x2F, 0x6F, 0xEB, 120)
        lw = 2 if self._selected else 1
        self.setPen(QPen(color, lw, Qt.PenStyle.SolidLine))
        fill = QColor(0x2F, 0x6F, 0xEB, 30) if self._selected else QColor(0x2F, 0x6F, 0xEB, 10)
        self.setBrush(QBrush(fill))

    def set_selected(self, sel: bool):
        self._selected = sel
        self._update_pen()


# ──────────────────────────────────────────────
# Canvas view
# ──────────────────────────────────────────────

class CanvasView(QGraphicsView):
    region_selected = pyqtSignal(list)   # list of region ids
    region_moved = pyqtSignal()
    split_lines_changed = pyqtSignal(list, list)   # (vlines, hlines) 分割線ドラッグ完了
    outer_margin_changed = pyqtSignal(int, int, int, int)  # top, bottom, left, right
    overlap_changed = pyqtSignal(int, int)          # ovx, ovy

    def __init__(self, parent=None):
        super().__init__(parent)
        self._scene = QGraphicsScene(self)
        self.setScene(self._scene)
        self.setRenderHint(QPainter.RenderHint.Antialiasing)
        self.setDragMode(QGraphicsView.DragMode.NoDrag)
        self.setTransformationAnchor(QGraphicsView.ViewportAnchor.AnchorUnderMouse)
        self.setResizeAnchor(QGraphicsView.ViewportAnchor.AnchorUnderMouse)
        self.setBackgroundBrush(QBrush(QColor(45, 45, 48)))
        self.setAcceptDrops(True)
        self.viewport().setAcceptDrops(True)  # QGraphicsView はビューポートへの設定も必要
        self.setMouseTracking(True)           # mouseMoveEvent をボタンなしでも受け取る
        self._open_files_callback = None  # MainWindow から注入

        self._doc: Optional[Document] = None
        self._pixmap_item: Optional[QGraphicsPixmapItem] = None
        self._region_items: dict[int, RegionRectItem] = {}
        self._selected_ids: list[int] = []
        self._tool = "select"
        self._panning = False
        self._pan_start = QPoint()
        self._show_grid = True
        self._show_guide = True
        self._show_whitespace = True
        self._bg_mode = "checker"  # checker / white / black
        self._onion_prev = False
        self._onion_next = False
        self._onion_opacity = 50
        self._current_frame_idx = -1
        self._onion_items: list[QGraphicsPixmapItem] = []
        self._zoom = 1.0
        self._mask_cache: dict[int, QPixmap] = {}

        # ── 分割線・外周余白・重複ドラッグ ──
        # _vsegs[row_band][line_idx]: 行バンドごとの垂直分割線X座標
        # _hsegs[col_band][line_idx]: 列バンドごとの水平分割線Y座標
        self._vsegs: list[list[float]] = []   # shape: rows × (cols-1)
        self._hsegs: list[list[float]] = []   # shape: cols × (rows-1)
        self._outer_bounds = (0, 0, 0, 0)  # (left, top, right_x, bottom_y)
        self._overlap_x: int = 0
        self._overlap_y: int = 0
        self._vline_items: list = []   # フラット list[QGraphicsLineItem], row_band*n_vlines + line_idx
        self._hline_items: list = []   # フラット list[QGraphicsLineItem], col_band*n_hlines + line_idx
        self._outer_edge_items: dict = {}  # 'top'/'bottom'/'left'/'right' -> QGraphicsLineItem
        self._ovlp_v_item = None           # X重複ハンドル（緑縦線）
        self._ovlp_h_item = None           # Y重複ハンドル（緑横線）
        self._drag_type: str = ''          # 'vseg','hseg','outer_top/bottom/left/right','ovlp_x','ovlp_y','cell_ovlp_v','cell_ovlp_h'
        self._drag_row_band: int = -1      # vseg / outer ドラッグ時の行バンド
        self._drag_col_band: int = -1      # hseg / outer ドラッグ時の列バンド
        self._drag_idx: int = -1           # 分割線インデックス
        self._drag_orig_val: float = 0.0
        self._drag_prev_val: float = 0.0
        self._drag_subtype: str = ''       # '' | 'left' | 'right'  ペア線のどちらをドラッグするか
        # ペア線：_vseg_pair[row][li] = 右側線のX座標（Noneなら1本線）
        # _vsegs[row][li] が左側（または単独）、_vseg_pair が右側
        self._vseg_pair: list[list] = []
        self._hseg_pair: list[list] = []
        self._vline_pair_items: list = []  # ペア右側線のシーンアイテム（Noneまたは QGraphicsLineItem）
        self._hline_pair_items: list = []

        # per-band 外周オフセット（Ctrl ドラッグで個別設定）
        # _outer_top_offsets[col_band], _outer_bottom_offsets[col_band]
        # _outer_left_offsets[row_band], _outer_right_offsets[row_band]
        self._outer_top_offsets: list[float] = []
        self._outer_bottom_offsets: list[float] = []
        self._outer_left_offsets: list[float] = []
        self._outer_right_offsets: list[float] = []

        # per-cell 重複（Alt ドラッグで個別設定）
        # _cell_ovlp_v[row][col_idx]: 列分割線 col_idx の左右重複（row バンド）
        # _cell_ovlp_h[col][row_idx]: 行分割線 row_idx の上下重複（col バンド）
        self._cell_ovlp_v: list[list[int]] = []   # shape: rows × (cols-1)
        self._cell_ovlp_h: list[list[int]] = []   # shape: cols × (rows-1)

        # per-cell 重複のシーンアイテム（薄緑の点線）
        self._cell_ovlp_v_items: list[list] = []  # shape: rows × (cols-1)
        self._cell_ovlp_h_items: list[list] = []  # shape: cols × (rows-1)

    def set_document(self, doc: Document):
        self._doc = doc
        self._selected_ids = []
        self._region_items = {}
        self._mask_cache = {}
        self._rebuild_scene()

    def _rebuild_scene(self):
        self._scene.clear()
        self._pixmap_item = None
        self._region_items = {}
        # _scene.clear() がC++アイテムを削除するので参照も全リセット
        self._vline_items = []
        self._hline_items = []
        self._outer_edge_items = {}
        self._ovlp_v_item = None
        self._ovlp_h_item = None
        self._vsegs = []
        self._hsegs = []
        self._vseg_pair = []
        self._hseg_pair = []
        self._vline_pair_items = []
        self._hline_pair_items = []
        self._outer_bounds = (0, 0, 0, 0)
        if not self._doc:
            return
        img = self._doc.source_pil
        self._source_qimg = pil_to_qimage(img)
        bg_pix = self._make_bg_pixmap(img.width, img.height)
        self._bg_item = self._scene.addPixmap(bg_pix)
        self._bg_item.setZValue(-1)
        self._pixmap_item = QGraphicsPixmapItem(QPixmap.fromImage(self._source_qimg))
        self._scene.addItem(self._pixmap_item)
        self._pixmap_item.setZValue(0)
        for r in self._doc.regions:
            self._add_region_item(r)
        self._scene.setSceneRect(QRectF(0, 0, img.width, img.height))

    def _make_bg_pixmap(self, w, h) -> QPixmap:
        pix = QPixmap(w, h)
        p = QPainter(pix)
        if self._bg_mode == "white":
            p.fillRect(0, 0, w, h, Qt.GlobalColor.white)
        elif self._bg_mode == "black":
            p.fillRect(0, 0, w, h, Qt.GlobalColor.black)
        else:  # checker
            cs = 16
            for y in range(0, h, cs):
                for x in range(0, w, cs):
                    c = QColor(200, 200, 200) if (x // cs + y // cs) % 2 == 0 else QColor(150, 150, 150)
                    p.fillRect(x, y, min(cs, w - x), min(cs, h - y), c)
        p.end()
        return pix

    def _add_region_item(self, region: Region):
        item = RegionRectItem(region, self)
        self._scene.addItem(item)
        item.setZValue(1)
        # number label
        label = self._scene.addText(region.name)
        label.setDefaultTextColor(QColor(0x2F, 0x6F, 0xEB))
        label.setPos(region.crop.x() + 2, region.crop.y() + 2)
        label.setZValue(2)
        self._region_items[region.id] = item

    def refresh_regions(self):
        # Remove old
        for item in list(self._region_items.values()):
            self._scene.removeItem(item)
        self._region_items = {}
        # Remove labels (text items at z=2)
        for item in self._scene.items():
            if hasattr(item, 'toPlainText'):
                self._scene.removeItem(item)
        if not self._doc:
            return
        for r in self._doc.regions:
            self._add_region_item(r)
        # Re-apply selection highlight
        for rid in self._selected_ids:
            if rid in self._region_items:
                self._region_items[rid].set_selected(True)

    def refresh_bg(self):
        if not self._doc:
            return
        img = self._doc.source_pil
        pix = self._make_bg_pixmap(img.width, img.height)
        if self._bg_item:
            self._bg_item.setPixmap(pix)

    # ── 分割線管理 ──

    def set_split_lines(self, vlines: list, hlines: list, outer_bounds: tuple):
        """MainWindow から呼ばれ、分割線を設定してシーンに描画する。
        vlines/hlines はフラットリスト（均等初期値）。全バンドを同じ値で初期化する。
        """
        n_vlines = len(vlines)
        n_hlines = len(hlines)
        rows = n_hlines + 1
        cols = n_vlines + 1
        # 全行バンドを同じ垂直線位置で初期化
        self._vsegs = [[float(v) for v in vlines] for _ in range(rows)]
        # 全列バンドを同じ水平線位置で初期化
        self._hsegs = [[float(h) for h in hlines] for _ in range(cols)]
        self._outer_bounds = outer_bounds
        self._reset_per_band_data(cols, rows)
        self._rebuild_split_line_items()

    def set_split_lines_2d(self, vsegs: list, hsegs: list, outer_bounds: tuple):
        """自動分割用：行ごと・列ごとに異なる分割線位置を直接設定する。"""
        self._vsegs = [[float(x) for x in row] for row in vsegs]
        self._hsegs = [[float(y) for y in col] for col in hsegs]
        self._outer_bounds = outer_bounds
        rows = len(vsegs) if vsegs else 1
        cols = len(hsegs) if hsegs else 1
        self._reset_per_band_data(cols, rows)
        self._rebuild_split_line_items()

    def _reset_per_band_data(self, cols: int, rows: int):
        """per-band 外周オフセットと per-cell 重複をゼロリセットする。"""
        self._outer_top_offsets    = [0.0] * cols
        self._outer_bottom_offsets = [0.0] * cols
        self._outer_left_offsets   = [0.0] * rows
        self._outer_right_offsets  = [0.0] * rows
        n_vlines = cols - 1
        n_hlines = rows - 1
        self._cell_ovlp_v = [[0] * n_vlines for _ in range(rows)]
        self._cell_ovlp_h = [[0] * n_hlines for _ in range(cols)]
        self._vseg_pair   = [[None] * n_vlines for _ in range(rows)]
        self._hseg_pair   = [[None] * n_hlines for _ in range(cols)]

    def _rebuild_split_line_items(self):
        for item in self._vline_items + self._hline_items + list(self._outer_edge_items.values()):
            self._scene.removeItem(item)
        for item in [self._ovlp_v_item, self._ovlp_h_item]:
            if item:
                self._scene.removeItem(item)
        for item in self._vline_pair_items + self._hline_pair_items:
            if item is not None:
                self._scene.removeItem(item)
        self._vline_items = []
        self._hline_items = []
        self._vline_pair_items = []
        self._hline_pair_items = []
        self._outer_edge_items = {}
        self._ovlp_v_item = None
        self._ovlp_h_item = None

        if not self._doc:
            return

        ol, ot, ox, oy = self._outer_bounds
        n_hlines = len(self._hsegs[0]) if self._hsegs else 0
        n_vlines = len(self._vsegs[0]) if self._vsegs else 0
        rows = n_hlines + 1
        cols = n_vlines + 1

        # per-band 外周オフセットが存在しなければリセット
        if len(self._outer_top_offsets) != cols:
            self._reset_per_band_data(cols, rows)

        edge_pen = _ls_pen('margin_color', 'margin_alpha', 'margin_width', Qt.PenStyle.DashLine)
        band_edge_pen = _ls_pen('margin_color', 'margin_alpha', 'margin_width', Qt.PenStyle.DashDotLine)

        # 外周余白の4辺をグローバル1本で描画
        for key, x1, y1, x2, y2 in [
            ('top',    ol, ot, ox, ot),
            ('bottom', ol, oy, ox, oy),
            ('left',   ol, ot, ol, oy),
            ('right',  ox, ot, ox, oy),
        ]:
            item = self._scene.addLine(x1, y1, x2, y2, edge_pen)
            item.setZValue(1.5)
            self._outer_edge_items[key] = item

        # per-band 外周辺（Ctrl で設定された個別オフセット付き辺）
        for c in range(cols):
            bx1 = ol if c == 0 else self._vsegs[0][c - 1]
            bx2 = ox if c == cols - 1 else self._vsegs[0][c]
            bt = ot + self._outer_top_offsets[c] if c < len(self._outer_top_offsets) else ot
            bb = oy + self._outer_bottom_offsets[c] if c < len(self._outer_bottom_offsets) else oy
            if self._outer_top_offsets[c] if c < len(self._outer_top_offsets) else False:
                item = self._scene.addLine(bx1, bt, bx2, bt, band_edge_pen)
                item.setZValue(1.6)
                self._outer_edge_items[f'top_{c}'] = item
            if self._outer_bottom_offsets[c] if c < len(self._outer_bottom_offsets) else False:
                item = self._scene.addLine(bx1, bb, bx2, bb, band_edge_pen)
                item.setZValue(1.6)
                self._outer_edge_items[f'bottom_{c}'] = item
        for r in range(rows):
            by1 = ot if r == 0 else self._hsegs[0][r - 1]
            by2 = oy if r == rows - 1 else self._hsegs[0][r]
            bl = ol + self._outer_left_offsets[r] if r < len(self._outer_left_offsets) else ol
            br = ox + self._outer_right_offsets[r] if r < len(self._outer_right_offsets) else ox
            if self._outer_left_offsets[r] if r < len(self._outer_left_offsets) else False:
                item = self._scene.addLine(bl, by1, bl, by2, band_edge_pen)
                item.setZValue(1.6)
                self._outer_edge_items[f'left_{r}'] = item
            if self._outer_right_offsets[r] if r < len(self._outer_right_offsets) else False:
                item = self._scene.addLine(br, by1, br, by2, band_edge_pen)
                item.setZValue(1.6)
                self._outer_edge_items[f'right_{r}'] = item

        # 垂直分割線：行バンドごとにセグメントを描画
        v_pen = _ls_pen('split_color', 'split_alpha', 'split_width')
        for r in range(rows):
            bt = ot + (self._outer_top_offsets[0] if self._outer_top_offsets else 0)
            # 行バンドのY範囲（per-band 外周も考慮）
            y1 = (ot + self._outer_top_offsets[0]) if r == 0 else self._hsegs[0][r - 1]
            y2 = (oy + self._outer_bottom_offsets[0]) if r == rows - 1 else self._hsegs[0][r]
            row_pair = self._vseg_pair[r] if r < len(self._vseg_pair) else []
            for li, x in enumerate(self._vsegs[r] if r < len(self._vsegs) else []):
                item = self._scene.addLine(x, y1, x, y2, v_pen)
                item.setZValue(2)
                self._vline_items.append(item)
                # ペア右側線
                px = row_pair[li] if li < len(row_pair) else None
                if px is not None:
                    pitem = self._scene.addLine(px, y1, px, y2, v_pen)
                    pitem.setZValue(2)
                    self._vline_pair_items.append(pitem)
                else:
                    self._vline_pair_items.append(None)

        # 水平分割線：列バンドごとにセグメントを描画
        h_pen = _ls_pen('split_color', 'split_alpha', 'split_width')
        for c in range(cols):
            x1 = ol if c == 0 else self._vsegs[0][c - 1]
            x2 = ox if c == cols - 1 else self._vsegs[0][c]
            col_pair = self._hseg_pair[c] if c < len(self._hseg_pair) else []
            for li, y in enumerate(self._hsegs[c] if c < len(self._hsegs) else []):
                item = self._scene.addLine(x1, y, x2, y, h_pen)
                item.setZValue(2)
                self._hline_items.append(item)
                py = col_pair[li] if li < len(col_pair) else None
                if py is not None:
                    pitem = self._scene.addLine(x1, py, x2, py, h_pen)
                    pitem.setZValue(2)
                    self._hline_pair_items.append(pitem)
                else:
                    self._hline_pair_items.append(None)

        # グローバル重複/ギャップ調整ハンドル（緑点線）
        ovlp_pen = _ls_pen('overlap_color', 'overlap_alpha', 'overlap_width', Qt.PenStyle.DotLine)
        hx = ol - self._overlap_x
        self._ovlp_v_item = self._scene.addLine(hx, ot, hx, oy, ovlp_pen)
        self._ovlp_v_item.setZValue(1.8)
        hy = ot - self._overlap_y
        self._ovlp_h_item = self._scene.addLine(ol, hy, ox, hy, ovlp_pen)
        self._ovlp_h_item.setZValue(1.8)

        # per-cell 重複ハンドル（薄い緑点線）
        cell_ovlp_pen = _ls_pen('overlap_color', 'overlap_alpha', 'overlap_width', Qt.PenStyle.DotLine)
        self._cell_ovlp_v_items = []
        for r in range(rows):
            row_items = []
            y1 = ot if r == 0 else self._hsegs[0][r - 1]
            y2 = oy if r == rows - 1 else self._hsegs[0][r]
            row_v = self._cell_ovlp_v[r] if r < len(self._cell_ovlp_v) else []
            for li in range(n_vlines):
                ov = row_v[li] if li < len(row_v) else 0
                if ov and li < len(self._vsegs[r] if r < len(self._vsegs) else []):
                    x = self._vsegs[r][li]
                    item_l = self._scene.addLine(x - ov, y1, x - ov, y2, cell_ovlp_pen)
                    item_l.setZValue(1.7)
                    item_r = self._scene.addLine(x + ov, y1, x + ov, y2, cell_ovlp_pen)
                    item_r.setZValue(1.7)
                    row_items.append((item_l, item_r))
                else:
                    row_items.append((None, None))
            self._cell_ovlp_v_items.append(row_items)
        self._cell_ovlp_h_items = []
        for c in range(cols):
            col_items = []
            x1 = ol if c == 0 else self._vsegs[0][c - 1]
            x2 = ox if c == cols - 1 else self._vsegs[0][c]
            col_h = self._cell_ovlp_h[c] if c < len(self._cell_ovlp_h) else []
            for li in range(n_hlines):
                oh = col_h[li] if li < len(col_h) else 0
                if oh and li < len(self._hsegs[c] if c < len(self._hsegs) else []):
                    y = self._hsegs[c][li]
                    item_t = self._scene.addLine(x1, y - oh, x2, y - oh, cell_ovlp_pen)
                    item_t.setZValue(1.7)
                    item_b = self._scene.addLine(x1, y + oh, x2, y + oh, cell_ovlp_pen)
                    item_b.setZValue(1.7)
                    col_items.append((item_t, item_b))
                else:
                    col_items.append((None, None))
            self._cell_ovlp_h_items.append(col_items)

    def _update_outer_edge(self):
        """外周余白ドラッグ中の高速更新。外周辺アイテムの座標のみ更新する。"""
        ol, ot, ox, oy = self._outer_bounds
        # グローバル4辺
        for key, x1, y1, x2, y2 in [
            ('top',    ol, ot, ox, ot),
            ('bottom', ol, oy, ox, oy),
            ('left',   ol, ot, ol, oy),
            ('right',  ox, ot, ox, oy),
        ]:
            item = self._outer_edge_items.get(key)
            if item:
                item.setLine(x1, y1, x2, y2)
        # per-band 辺も更新
        n_vlines = len(self._vsegs[0]) if self._vsegs else 0
        cols = n_vlines + 1
        n_hlines = len(self._hsegs[0]) if self._hsegs else 0
        rows = n_hlines + 1
        for c in range(cols):
            bx1 = ol if c == 0 else self._vsegs[0][c - 1]
            bx2 = ox if c == cols - 1 else self._vsegs[0][c]
            ot_c = ot + (self._outer_top_offsets[c] if c < len(self._outer_top_offsets) else 0)
            ob_c = oy + (self._outer_bottom_offsets[c] if c < len(self._outer_bottom_offsets) else 0)
            it = self._outer_edge_items.get(f'top_{c}')
            if it: it.setLine(bx1, ot_c, bx2, ot_c)
            ib = self._outer_edge_items.get(f'bottom_{c}')
            if ib: ib.setLine(bx1, ob_c, bx2, ob_c)
        for r in range(rows):
            by1 = ot if r == 0 else self._hsegs[0][r - 1]
            by2 = oy if r == rows - 1 else self._hsegs[0][r]
            ol_r = ol + (self._outer_left_offsets[r] if r < len(self._outer_left_offsets) else 0)
            or_r = ox + (self._outer_right_offsets[r] if r < len(self._outer_right_offsets) else 0)
            il = self._outer_edge_items.get(f'left_{r}')
            if il: il.setLine(ol_r, by1, ol_r, by2)
            ir = self._outer_edge_items.get(f'right_{r}')
            if ir: ir.setLine(or_r, by1, or_r, by2)
        # 分割線セグメントの端点も更新
        self._update_seg_items()

    def _update_seg_items(self):
        """_vsegs / _hsegs の値をシーンアイテムに反映する（位置のみ更新）"""
        ol, ot, ox, oy = self._outer_bounds
        n_hlines = len(self._hsegs[0]) if self._hsegs else 0
        rows = n_hlines + 1
        n_vlines = len(self._vsegs[0]) if self._vsegs else 0
        cols = n_vlines + 1
        idx = 0
        for r in range(rows):
            y1 = ot if r == 0 else self._hsegs[0][r - 1]
            y2 = oy if r == rows - 1 else self._hsegs[0][r]
            row_pair = self._vseg_pair[r] if r < len(self._vseg_pair) else []
            for li in range(n_vlines):
                if idx < len(self._vline_items):
                    x = self._vsegs[r][li] if r < len(self._vsegs) and li < len(self._vsegs[r]) else 0
                    self._vline_items[idx].setLine(x, y1, x, y2)
                    px = row_pair[li] if li < len(row_pair) else None
                    if idx < len(self._vline_pair_items) and self._vline_pair_items[idx] is not None:
                        if px is not None:
                            self._vline_pair_items[idx].setLine(px, y1, px, y2)
                idx += 1
        idx = 0
        for c in range(cols):
            x1 = ol if c == 0 else self._vsegs[0][c - 1]
            x2 = ox if c == cols - 1 else self._vsegs[0][c]
            col_pair = self._hseg_pair[c] if c < len(self._hseg_pair) else []
            for li in range(n_hlines):
                if idx < len(self._hline_items):
                    y = self._hsegs[c][li] if c < len(self._hsegs) and li < len(self._hsegs[c]) else 0
                    self._hline_items[idx].setLine(x1, y, x2, y)
                    py = col_pair[li] if li < len(col_pair) else None
                    if idx < len(self._hline_pair_items) and self._hline_pair_items[idx] is not None:
                        if py is not None:
                            self._hline_pair_items[idx].setLine(x1, py, x2, py)
                idx += 1

    def _update_cell_ovlp_items(self):
        """per-cell 重複ハンドルアイテムをリアルタイム更新。アイテムが未生成なら生成する。"""
        ol, ot, ox, oy = self._outer_bounds
        n_hlines = len(self._hsegs[0]) if self._hsegs else 0
        rows = n_hlines + 1
        n_vlines = len(self._vsegs[0]) if self._vsegs else 0
        cell_pen = _ls_pen('overlap_color', 'overlap_alpha', 'overlap_width', Qt.PenStyle.DotLine)
        # 配列の形状が合わなければ初期化
        if len(self._cell_ovlp_v_items) != rows:
            self._cell_ovlp_v_items = [[(None, None)] * n_vlines for _ in range(rows)]
        for r in range(rows):
            y1 = ot if r == 0 else self._hsegs[0][r - 1]
            y2 = oy if r == rows - 1 else self._hsegs[0][r]
            if len(self._cell_ovlp_v_items[r]) != n_vlines:
                self._cell_ovlp_v_items[r] = [(None, None)] * n_vlines
            for li in range(n_vlines):
                ov = self._cell_ovlp_v[r][li] if r < len(self._cell_ovlp_v) and li < len(self._cell_ovlp_v[r]) else 0
                x = self._vsegs[r][li] if r < len(self._vsegs) and li < len(self._vsegs[r]) else 0
                item_l, item_r = self._cell_ovlp_v_items[r][li]
                if ov:
                    if item_l is None:
                        item_l = self._scene.addLine(x - ov, y1, x - ov, y2, cell_pen)
                        item_l.setZValue(1.7)
                        item_r = self._scene.addLine(x + ov, y1, x + ov, y2, cell_pen)
                        item_r.setZValue(1.7)
                        self._cell_ovlp_v_items[r][li] = (item_l, item_r)
                    else:
                        item_l.setLine(x - ov, y1, x - ov, y2)
                        item_r.setLine(x + ov, y1, x + ov, y2)
                else:
                    if item_l is not None:
                        self._scene.removeItem(item_l)
                        self._scene.removeItem(item_r)
                        self._cell_ovlp_v_items[r][li] = (None, None)

    def _update_overlap_handles(self):
        """重複ハンドルの位置だけ更新"""
        ol, ot, ox, oy = self._outer_bounds
        if self._ovlp_v_item:
            hx = ol - self._overlap_x
            self._ovlp_v_item.setLine(hx, ot, hx, oy)
        if self._ovlp_h_item:
            hy = ot - self._overlap_y
            self._ovlp_h_item.setLine(ol, hy, ox, hy)

    def _find_near_line(self, sx: float, sy: float) -> Optional[tuple]:
        """画像座標 (sx, sy) に近い分割線・ハンドルを返す。
        Returns:
          ('vseg', row_band, line_idx) | ('hseg', col_band, line_idx)
          | ('outer_*', -1) | ('ovlp_*', -1)
        """
        scale = self.transform().m11()
        thr = 7.0 / max(scale, 0.01)
        ol, ot, ox, oy = self._outer_bounds

        n_hlines = len(self._hsegs[0]) if self._hsegs else 0
        rows = n_hlines + 1
        n_vlines = len(self._vsegs[0]) if self._vsegs else 0
        cols = n_vlines + 1

        # 垂直分割線セグメント：行バンドのY範囲内かつX位置が近い
        for r in range(rows):
            band_y1 = ot if r == 0 else self._hsegs[0][r - 1]
            band_y2 = oy if r == rows - 1 else self._hsegs[0][r]
            if not (band_y1 - thr <= sy <= band_y2 + thr):
                continue
            row_pair = self._vseg_pair[r] if r < len(self._vseg_pair) else []
            for li in range(n_vlines):
                x = self._vsegs[r][li] if r < len(self._vsegs) else 0
                px = row_pair[li] if li < len(row_pair) else None
                # ペア右側線を優先チェック
                if px is not None and abs(sx - px) <= thr:
                    return ('vseg', r, li, 'right')
                if abs(sx - x) <= thr:
                    sub = 'left' if px is not None else 'single'
                    return ('vseg', r, li, sub)

        # 水平分割線セグメント：列バンドのX範囲内かつY位置が近い
        for c in range(cols):
            band_x1 = ol if c == 0 else self._vsegs[0][c - 1]
            band_x2 = ox if c == cols - 1 else self._vsegs[0][c]
            if not (band_x1 - thr <= sx <= band_x2 + thr):
                continue
            col_pair = self._hseg_pair[c] if c < len(self._hseg_pair) else []
            for li in range(n_hlines):
                y = self._hsegs[c][li] if c < len(self._hsegs) else 0
                py = col_pair[li] if li < len(col_pair) else None
                if py is not None and abs(sy - py) <= thr:
                    return ('hseg', c, li, 'right')
                if abs(sy - y) <= thr:
                    sub = 'left' if py is not None else 'single'
                    return ('hseg', c, li, sub)

        # 外周余白辺（第3要素 = ヒットした列/行バンドインデックス）
        if abs(sy - ot) <= thr and ol - thr <= sx <= ox + thr:
            c_band = next((c for c in range(cols - 1, -1, -1)
                           if sx >= (ol if c == 0 else self._vsegs[0][c - 1])), 0)
            return ('outer_top', -1, c_band)
        if abs(sy - oy) <= thr and ol - thr <= sx <= ox + thr:
            c_band = next((c for c in range(cols - 1, -1, -1)
                           if sx >= (ol if c == 0 else self._vsegs[0][c - 1])), 0)
            return ('outer_bottom', -1, c_band)
        if abs(sx - ol) <= thr and ot - thr <= sy <= oy + thr:
            r_band = next((r for r in range(rows - 1, -1, -1)
                           if sy >= (ot if r == 0 else self._hsegs[0][r - 1])), 0)
            return ('outer_left', -1, r_band)
        if abs(sx - ox) <= thr and ot - thr <= sy <= oy + thr:
            r_band = next((r for r in range(rows - 1, -1, -1)
                           if sy >= (ot if r == 0 else self._hsegs[0][r - 1])), 0)
            return ('outer_right', -1, r_band)

        # 重複/ギャップハンドル
        if abs(sx - (ol - self._overlap_x)) <= thr:
            return ('ovlp_x', -1, -1)
        if abs(sy - (ot - self._overlap_y)) <= thr:
            return ('ovlp_y', -1, -1)

        return None

    def set_overlap(self, ovx: int, ovy: int):
        self._overlap_x = ovx
        self._overlap_y = ovy

    def clear_split_lines(self):
        self.set_split_lines([], [], (0, 0, 0, 0))

    def set_tool(self, tool: str):
        self._tool = tool
        if tool == "pan":
            self.setCursor(Qt.CursorShape.OpenHandCursor)
        else:
            self.setCursor(Qt.CursorShape.ArrowCursor)

    def fit_in_view(self):
        if self._doc:
            img = self._doc.source_pil
            self.fitInView(QRectF(0, 0, img.width, img.height), Qt.AspectRatioMode.KeepAspectRatio)

    def set_zoom(self, factor: float):
        self.resetTransform()
        self.scale(factor, factor)
        self._zoom = factor

    def wheelEvent(self, event: QWheelEvent):
        if event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            factor = 1.01 if event.angleDelta().y() > 0 else 1 / 1.01
            self.scale(factor, factor)
        else:
            super().wheelEvent(event)

    def mousePressEvent(self, event: QMouseEvent):
        # ── 中ボタン or 手のひらツール → パン ──
        if event.button() == Qt.MouseButton.MiddleButton or (
                self._tool == "pan" and event.button() == Qt.MouseButton.LeftButton):
            self._panning = True
            self._pan_start = event.pos()
            self.setCursor(Qt.CursorShape.ClosedHandCursor)
            event.accept()
            return

        if event.button() == Qt.MouseButton.LeftButton:
            sp = self.mapToScene(event.pos())
            sx, sy = sp.x(), sp.y()

            # ── 分割線・ハンドルドラッグ判定 ──
            if self._tool in ("select", "split_rect") and self._outer_bounds != (0, 0, 0, 0):
                hit = self._find_near_line(sx, sy)
                if hit:
                    ctrl  = bool(event.modifiers() & Qt.KeyboardModifier.ControlModifier)
                    alt   = bool(event.modifiers() & Qt.KeyboardModifier.AltModifier)
                    shift = bool(event.modifiers() & Qt.KeyboardModifier.ShiftModifier)
                    self._drag_type = hit[0]
                    self._drag_subtype = ''
                    if self._drag_type == 'vseg':
                        _, self._drag_row_band, self._drag_idx = hit[1], hit[1], hit[2]
                        self._drag_row_band = hit[1]
                        self._drag_col_band = -1
                        r  = self._drag_row_band
                        li = self._drag_idx
                        hit_sub = hit[3] if len(hit) > 3 else 'single'
                        self._drag_prev_val = sx
                        if alt:
                            # Alt: per-cell 重複調整モード
                            self._drag_type = 'cell_ovlp_v'
                            self._drag_orig_val = (
                                self._cell_ovlp_v[r][li]
                                if r < len(self._cell_ovlp_v) and li < len(self._cell_ovlp_v[r])
                                else 0
                            )
                        elif shift:
                            # Shift: このバンドのみペア線操作（常に個別）
                            if hit_sub == 'single':
                                # 1本線 → このバンドだけ分割
                                if r < len(self._vseg_pair) and li < len(self._vseg_pair[r]):
                                    self._vseg_pair[r][li] = self._vsegs[r][li]
                                self._drag_subtype = 'right'
                                self._rebuild_split_line_items()
                            else:
                                self._drag_subtype = hit_sub
                            self._drag_orig_val = (
                                self._vseg_pair[r][li] if self._drag_subtype == 'right' and
                                r < len(self._vseg_pair) and li < len(self._vseg_pair[r]) and
                                self._vseg_pair[r][li] is not None
                                else self._vsegs[r][li]
                            )
                        else:
                            # 通常: ペアのどちらのサブ線かを記録
                            self._drag_subtype = '' if hit_sub == 'single' else hit_sub
                            self._drag_orig_val = self._vsegs[r][li]
                    elif self._drag_type == 'hseg':
                        self._drag_col_band = hit[1]
                        self._drag_idx      = hit[2]
                        self._drag_row_band = -1
                        c  = self._drag_col_band
                        li = self._drag_idx
                        hit_sub = hit[3] if len(hit) > 3 else 'single'
                        self._drag_prev_val = sy
                        if alt:
                            self._drag_type = 'cell_ovlp_h'
                            self._drag_orig_val = (
                                self._cell_ovlp_h[c][li]
                                if c < len(self._cell_ovlp_h) and li < len(self._cell_ovlp_h[c])
                                else 0
                            )
                        elif shift:
                            # Shift: このバンドのみペア線操作（常に個別）
                            if hit_sub == 'single':
                                if c < len(self._hseg_pair) and li < len(self._hseg_pair[c]):
                                    self._hseg_pair[c][li] = self._hsegs[c][li]
                                self._drag_subtype = 'right'
                                self._rebuild_split_line_items()
                            else:
                                self._drag_subtype = hit_sub
                            self._drag_orig_val = (
                                self._hseg_pair[c][li] if self._drag_subtype == 'right' and
                                c < len(self._hseg_pair) and li < len(self._hseg_pair[c]) and
                                self._hseg_pair[c][li] is not None
                                else self._hsegs[c][li]
                            )
                        else:
                            self._drag_subtype = '' if hit_sub == 'single' else hit_sub
                            self._drag_orig_val = self._hsegs[c][li]
                    else:
                        _, _, band_idx = hit
                        self._drag_idx = hit[1]
                        # Ctrl: per-band 外周ドラッグ用にバンドインデックスを保存
                        if self._drag_type in ('outer_top', 'outer_bottom'):
                            self._drag_col_band = band_idx if ctrl else -1
                            self._drag_row_band = -1
                        else:
                            self._drag_row_band = band_idx if ctrl else -1
                            self._drag_col_band = -1
                    event.accept()
                    return

            # ── 領域選択（選択ツール） ──
            if self._tool == "select":
                hit_rid = None
                for rid, item in self._region_items.items():
                    if item.rect().contains(sp):
                        hit_rid = rid
                        break
                if hit_rid is not None:
                    if event.modifiers() & Qt.KeyboardModifier.ControlModifier:
                        if hit_rid in self._selected_ids:
                            self._selected_ids.remove(hit_rid)
                        else:
                            self._selected_ids.append(hit_rid)
                    else:
                        self._selected_ids = [hit_rid]
                else:
                    self._selected_ids = []
                self._apply_selection()
                event.accept()
                return

            # ── その他のツール → クラッシュ防止のためイベントを消費 ──
            event.accept()
            return

        super().mousePressEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent):
        # ── パン ──
        if self._panning:
            delta = event.pos() - self._pan_start
            self._pan_start = event.pos()
            self.horizontalScrollBar().setValue(self.horizontalScrollBar().value() - delta.x())
            self.verticalScrollBar().setValue(self.verticalScrollBar().value() - delta.y())
            event.accept()
            return

        # ── ドラッグ中 ──
        if self._drag_type:
            sp = self.mapToScene(event.pos())
            sx, sy = sp.x(), sp.y()
            ol, ot, ox, oy = self._outer_bounds
            sw = self._doc.source_pil.width if self._doc else 9999
            sh = self._doc.source_pil.height if self._doc else 9999

            if self._drag_type == 'vseg':
                ctrl  = bool(event.modifiers() & Qt.KeyboardModifier.ControlModifier)
                shift = bool(event.modifiers() & Qt.KeyboardModifier.ShiftModifier)
                r  = self._drag_row_band
                li = self._drag_idx
                rows = len(self._vsegs)
                MERGE_THR = 4.0

                def _vseg_bounds(r2, li):
                    seg = self._vsegs[r2]
                    min_x = (seg[li - 1] + 1) if li > 0 else ol + 1
                    max_x = (seg[li + 1] - 1) if li < len(seg) - 1 else ox - 1
                    return min_x, max_x

                sub = self._drag_subtype  # '' | 'left' | 'right'

                if sub == 'right':
                    # 右側ペア線を移動（常にこのバンドのみ）
                    _, max_x = _vseg_bounds(r, li)
                    min_x = self._vsegs[r][li] + 1
                    new_val = max(min_x, min(max_x, sx))
                    if r < len(self._vseg_pair) and li < len(self._vseg_pair[r]):
                        self._vseg_pair[r][li] = new_val
                    # マージ判定（Shift保持かつ距離 < threshold）
                    if shift and abs(new_val - self._vsegs[r][li]) < MERGE_THR:
                        if r < len(self._vseg_pair) and li < len(self._vseg_pair[r]):
                            self._vseg_pair[r][li] = None
                        self._drag_subtype = ''
                        self._rebuild_split_line_items()
                elif sub == 'left':
                    # 左側ペア線（_vsegs）を移動（常にこのバンドのみ）
                    min_x, _ = _vseg_bounds(r, li)
                    px = self._vseg_pair[r][li] if r < len(self._vseg_pair) and li < len(self._vseg_pair[r]) and self._vseg_pair[r][li] is not None else None
                    max_x = (px - 1) if px is not None else _vseg_bounds(r, li)[1]
                    new_val = max(min_x, min(max_x, sx))
                    self._vsegs[r][li] = new_val
                    if shift and px is not None and abs(px - new_val) < MERGE_THR:
                        if r < len(self._vseg_pair) and li < len(self._vseg_pair[r]):
                            self._vseg_pair[r][li] = None
                        self._drag_subtype = ''
                        self._rebuild_split_line_items()
                else:
                    # 通常（ペアなし）の移動（既存ロジック）
                    if ctrl:
                        seg = self._vsegs[r]
                        min_x, max_x = _vseg_bounds(r, li)
                        self._vsegs[r][li] = max(min_x, min(max_x, sx))
                    else:
                        delta = sx - self._drag_prev_val
                        if delta != 0:
                            for r2 in range(rows):
                                min_x, max_x = _vseg_bounds(r2, li)
                                self._vsegs[r2][li] = max(min_x, min(max_x, self._vsegs[r2][li] + delta))
                                # ペアも一緒に動かす
                                if r2 < len(self._vseg_pair) and li < len(self._vseg_pair[r2]) and self._vseg_pair[r2][li] is not None:
                                    self._vseg_pair[r2][li] = max(min_x, min(max_x, self._vseg_pair[r2][li] + delta))

                self._drag_prev_val = sx
                self._update_seg_items()
                self.setCursor(Qt.CursorShape.SizeHorCursor)

            elif self._drag_type == 'hseg':
                ctrl  = bool(event.modifiers() & Qt.KeyboardModifier.ControlModifier)
                shift = bool(event.modifiers() & Qt.KeyboardModifier.ShiftModifier)
                c  = self._drag_col_band
                li = self._drag_idx
                cols = len(self._hsegs)
                MERGE_THR = 4.0

                def _hseg_bounds(c2, li):
                    seg = self._hsegs[c2]
                    min_y = (seg[li - 1] + 1) if li > 0 else ot + 1
                    max_y = (seg[li + 1] - 1) if li < len(seg) - 1 else oy - 1
                    return min_y, max_y

                sub = self._drag_subtype

                if sub == 'right':
                    # 下側ペア線を移動（常にこのバンドのみ）
                    _, max_y = _hseg_bounds(c, li)
                    min_y = self._hsegs[c][li] + 1
                    new_val = max(min_y, min(max_y, sy))
                    if c < len(self._hseg_pair) and li < len(self._hseg_pair[c]):
                        self._hseg_pair[c][li] = new_val
                    if shift and abs(new_val - self._hsegs[c][li]) < MERGE_THR:
                        if c < len(self._hseg_pair) and li < len(self._hseg_pair[c]):
                            self._hseg_pair[c][li] = None
                        self._drag_subtype = ''
                        self._rebuild_split_line_items()
                elif sub == 'left':
                    # 上側ペア線（_hsegs）を移動（常にこのバンドのみ）
                    min_y, _ = _hseg_bounds(c, li)
                    py = self._hseg_pair[c][li] if c < len(self._hseg_pair) and li < len(self._hseg_pair[c]) and self._hseg_pair[c][li] is not None else None
                    max_y = (py - 1) if py is not None else _hseg_bounds(c, li)[1]
                    new_val = max(min_y, min(max_y, sy))
                    self._hsegs[c][li] = new_val
                    if shift and py is not None and abs(py - new_val) < MERGE_THR:
                        if c < len(self._hseg_pair) and li < len(self._hseg_pair[c]):
                            self._hseg_pair[c][li] = None
                        self._drag_subtype = ''
                        self._rebuild_split_line_items()
                else:
                    if ctrl:
                        min_y, max_y = _hseg_bounds(c, li)
                        self._hsegs[c][li] = max(min_y, min(max_y, sy))
                    else:
                        delta = sy - self._drag_prev_val
                        if delta != 0:
                            for c2 in range(cols):
                                min_y, max_y = _hseg_bounds(c2, li)
                                self._hsegs[c2][li] = max(min_y, min(max_y, self._hsegs[c2][li] + delta))
                                if c2 < len(self._hseg_pair) and li < len(self._hseg_pair[c2]) and self._hseg_pair[c2][li] is not None:
                                    self._hseg_pair[c2][li] = max(min_y, min(max_y, self._hseg_pair[c2][li] + delta))

                self._drag_prev_val = sy
                self._update_seg_items()
                self.setCursor(Qt.CursorShape.SizeVerCursor)

            elif self._drag_type == 'outer_top':
                new_y = max(0, min(oy - 4, sy))
                c_band = self._drag_col_band
                if c_band >= 0 and c_band < len(self._outer_top_offsets):
                    # Ctrl: 列バンド個別
                    self._outer_top_offsets[c_band] = new_y - ot
                else:
                    # 通常: グローバル移動
                    self._outer_bounds = (ol, new_y, ox, oy)
                    self.outer_margin_changed.emit(int(new_y), sh - int(oy), int(ol), sw - int(ox))
                self._update_outer_edge()
                self.setCursor(Qt.CursorShape.SizeVerCursor)

            elif self._drag_type == 'outer_bottom':
                new_y = max(ot + 4, min(sh, sy))
                c_band = self._drag_col_band
                if c_band >= 0 and c_band < len(self._outer_bottom_offsets):
                    self._outer_bottom_offsets[c_band] = new_y - oy
                else:
                    self._outer_bounds = (ol, ot, ox, new_y)
                    self.outer_margin_changed.emit(int(ot), sh - int(new_y), int(ol), sw - int(ox))
                self._update_outer_edge()
                self.setCursor(Qt.CursorShape.SizeVerCursor)

            elif self._drag_type == 'outer_left':
                new_x = max(0, min(ox - 4, sx))
                r_band = self._drag_row_band
                if r_band >= 0 and r_band < len(self._outer_left_offsets):
                    self._outer_left_offsets[r_band] = new_x - ol
                else:
                    self._outer_bounds = (new_x, ot, ox, oy)
                    self.outer_margin_changed.emit(int(ot), sh - int(oy), int(new_x), sw - int(ox))
                self._update_outer_edge()
                self.setCursor(Qt.CursorShape.SizeHorCursor)

            elif self._drag_type == 'outer_right':
                new_x = max(ol + 4, min(sw, sx))
                r_band = self._drag_row_band
                if r_band >= 0 and r_band < len(self._outer_right_offsets):
                    self._outer_right_offsets[r_band] = new_x - ox
                else:
                    self._outer_bounds = (ol, ot, new_x, oy)
                    self.outer_margin_changed.emit(int(ot), sh - int(oy), int(ol), sw - int(new_x))
                self._update_outer_edge()
                self.setCursor(Qt.CursorShape.SizeHorCursor)

            elif self._drag_type == 'cell_ovlp_v':
                # Alt+vseg: per-cell 重複調整（マウス位置と分割線の距離が重複量）
                r = self._drag_row_band; li = self._drag_idx
                ref_x = self._vsegs[r][li] if r < len(self._vsegs) and li < len(self._vsegs[r]) else sx
                delta = max(0, int(abs(sx - ref_x)))
                if r < len(self._cell_ovlp_v) and li < len(self._cell_ovlp_v[r]):
                    self._cell_ovlp_v[r][li] = delta
                self._update_cell_ovlp_items()
                self.setCursor(Qt.CursorShape.SizeHorCursor)

            elif self._drag_type == 'cell_ovlp_h':
                # Alt+hseg: per-cell 重複調整
                c = self._drag_col_band; li = self._drag_idx
                ref_y = self._hsegs[c][li] if c < len(self._hsegs) and li < len(self._hsegs[c]) else sy
                delta = max(0, int(abs(sy - ref_y)))
                if c < len(self._cell_ovlp_h) and li < len(self._cell_ovlp_h[c]):
                    self._cell_ovlp_h[c][li] = delta
                self._update_cell_ovlp_items()
                self.setCursor(Qt.CursorShape.SizeVerCursor)

            elif self._drag_type == 'ovlp_x':
                new_ovx = max(-500, min(500, int(ol - sx)))
                self._overlap_x = new_ovx
                self._update_overlap_handles()
                self.setCursor(Qt.CursorShape.SizeHorCursor)
                self.overlap_changed.emit(new_ovx, self._overlap_y)

            elif self._drag_type == 'ovlp_y':
                new_ovy = max(-500, min(500, int(ot - sy)))
                self._overlap_y = new_ovy
                self._update_overlap_handles()
                self.setCursor(Qt.CursorShape.SizeVerCursor)
                self.overlap_changed.emit(self._overlap_x, new_ovy)

            event.accept()
            return

        # ── ホバー時カーソル変更 ──
        if self._tool in ("select", "split_rect") and self._outer_bounds != (0, 0, 0, 0):
            sp = self.mapToScene(event.pos())
            hit = self._find_near_line(sp.x(), sp.y())
            if hit:
                t = hit[0]
                if t in ('vseg', 'outer_left', 'outer_right', 'ovlp_x'):
                    self.setCursor(Qt.CursorShape.SizeHorCursor)
                else:
                    self.setCursor(Qt.CursorShape.SizeVerCursor)
            elif self._tool != "pan":
                self.setCursor(Qt.CursorShape.ArrowCursor)

        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent):
        # ── パン終了 ──
        if self._panning:
            self._panning = False
            self.setCursor(Qt.CursorShape.OpenHandCursor if self._tool == "pan" else Qt.CursorShape.ArrowCursor)
            event.accept()
            return

        # ── ドラッグ終了 ──
        if self._drag_type and event.button() == Qt.MouseButton.LeftButton:
            drag_type = self._drag_type
            self._drag_type = ''
            self._drag_idx = -1
            self.setCursor(Qt.CursorShape.ArrowCursor)

            if drag_type.startswith('outer_'):
                self.split_lines_changed.emit(self._vsegs, self._hsegs)
            elif drag_type in ('ovlp_x', 'ovlp_y'):
                self.overlap_changed.emit(self._overlap_x, self._overlap_y)
                self.split_lines_changed.emit(self._vsegs, self._hsegs)
            else:
                self.split_lines_changed.emit(self._vsegs, self._hsegs)

            event.accept()
            return

        super().mouseReleaseEvent(event)

    def _apply_selection(self):
        for rid, item in self._region_items.items():
            item.set_selected(rid in self._selected_ids)
        self.region_selected.emit(self._selected_ids)

    def set_selected_regions(self, ids: list[int]):
        self._selected_ids = ids
        self._apply_selection()

    def get_selected_ids(self) -> list[int]:
        return list(self._selected_ids)

    # Drag & drop
    def dragEnterEvent(self, event: QDragEnterEvent):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dragMoveEvent(self, event):  # これを override しないと Qt が drop を拒否する
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dropEvent(self, event: QDropEvent):
        urls = event.mimeData().urls()
        paths = [u.toLocalFile() for u in urls if u.isLocalFile()]
        if paths and callable(self._open_files_callback):
            self._open_files_callback(paths)

    def update_onion_skin(self, frames: list[AnimFrame], current_idx: int, doc: Document):
        for item in self._onion_items:
            self._scene.removeItem(item)
        self._onion_items = []
        if not self._onion_prev and not self._onion_next:
            return
        offsets = []
        if self._onion_prev and current_idx > 0:
            offsets.append((current_idx - 1, self._onion_opacity))
        if self._onion_next and current_idx < len(frames) - 1:
            offsets.append((current_idx + 1, self._onion_opacity))
        for fidx, opacity in offsets:
            frame = frames[fidx]
            region = next((r for r in doc.regions if r.id == frame.region_id), None)
            if not region:
                continue
            rendered = render_region(doc.source_pil, region)
            qimg = pil_to_qimage(rendered)
            pix = QPixmap.fromImage(qimg)
            item = QGraphicsPixmapItem(pix)
            item.setOpacity(opacity / 100.0)
            item.setPos(frame.offset_x, frame.offset_y)
            item.setZValue(3)
            self._scene.addItem(item)
            self._onion_items.append(item)


# ──────────────────────────────────────────────
# Image processing helpers
# ──────────────────────────────────────────────

def pil_to_qimage(pil_img: Image.Image) -> QImage:
    pil_img = pil_img.convert("RGBA")
    data = pil_img.tobytes("raw", "RGBA")
    qimg = QImage(data, pil_img.width, pil_img.height, QImage.Format.Format_RGBA8888)
    return qimg.copy()


def render_region(source: Image.Image, region: Region) -> Image.Image:
    """Render one region to its output canvas."""
    cr = region.crop
    if region._erased_pil:
        cropped = region._erased_pil.copy().convert("RGBA")
    else:
        cropped = source.crop((cr.x(), cr.y(), cr.x() + cr.width(), cr.y() + cr.height()))

    if not region._erased_pil and region._mask_pil:
        mask_resized = region._mask_pil.resize(cropped.size, Image.NEAREST)
        if cropped.mode != "RGBA":
            cropped = cropped.convert("RGBA")
        r_ch, g_ch, b_ch, a_ch = cropped.split()
        a_ch = Image.fromarray(
            __import__('numpy').minimum(
                __import__('numpy').array(a_ch),
                __import__('numpy').array(mask_resized.convert("L"))
            )
        )
        cropped = Image.merge("RGBA", (r_ch, g_ch, b_ch, a_ch))

    if region.flip_h:
        cropped = cropped.transpose(Image.FLIP_LEFT_RIGHT)
    if region.flip_v:
        cropped = cropped.transpose(Image.FLIP_TOP_BOTTOM)

    # color adjustments
    if region.brightness != 1.0:
        cropped = ImageEnhance.Brightness(cropped.convert("RGB")).enhance(region.brightness)
        cropped = cropped.convert("RGBA")
    if region.contrast != 1.0:
        cropped = ImageEnhance.Contrast(cropped.convert("RGB")).enhance(region.contrast)
        cropped = cropped.convert("RGBA")

    cw = region.canvas_w
    ch = region.canvas_h
    ml, mt, mr, mb = region.margin_left, region.margin_top, region.margin_right, region.margin_bottom
    avail_w = cw - ml - mr
    avail_h = ch - mt - mb

    if avail_w <= 0 or avail_h <= 0:
        avail_w = max(1, avail_w)
        avail_h = max(1, avail_h)

    # 透明ボーダーをトリムして実コンテンツのみをスケーリング対象にする
    if cropped.mode == "RGBA":
        bbox = cropped.getbbox()
        if bbox:
            cropped = cropped.crop(bbox)

    iw, ih = cropped.size
    if region.fit_mode == "Fit":
        scale = min(avail_w / iw, avail_h / ih) if iw and ih else 1
        nw, nh = int(iw * scale), int(ih * scale)
    elif region.fit_mode == "Fill":
        scale = max(avail_w / iw, avail_h / ih) if iw and ih else 1
        nw, nh = int(iw * scale), int(ih * scale)
    else:
        nw, nh = iw, ih

    # ユーザー調整スケールを適用
    us = getattr(region, 'user_scale', 1.0)
    if us != 1.0:
        nw, nh = int(nw * us), int(nh * us)

    nw, nh = max(1, nw), max(1, nh)
    resized = cropped.resize((nw, nh), Image.LANCZOS)

    canvas = Image.new("RGBA", (cw, ch), (
        region.bg_color.red(), region.bg_color.green(),
        region.bg_color.blue(), region.bg_color.alpha()
    ))

    anchor = region.anchor
    if anchor == "center":
        px = ml + (avail_w - nw) // 2
        py = mt + (avail_h - nh) // 2
    elif anchor == "top-left":
        px, py = ml, mt
    elif anchor == "top":
        px = ml + (avail_w - nw) // 2
        py = mt
    elif anchor == "top-right":
        px = ml + avail_w - nw
        py = mt
    elif anchor == "left":
        px = ml
        py = mt + (avail_h - nh) // 2
    elif anchor == "right":
        px = ml + avail_w - nw
        py = mt + (avail_h - nh) // 2
    elif anchor == "bottom-left":
        px = ml
        py = mt + avail_h - nh
    elif anchor == "bottom":
        px = ml + (avail_w - nw) // 2
        py = mt + avail_h - nh
    elif anchor == "bottom-right":
        px = ml + avail_w - nw
        py = mt + avail_h - nh
    else:
        px = ml + (avail_w - nw) // 2
        py = mt + (avail_h - nh) // 2

    if region.fit_mode == "Fill":
        crop_x = max(0, (nw - avail_w) // 2)
        crop_y = max(0, (nh - avail_h) // 2)
        resized = resized.crop((crop_x, crop_y, crop_x + avail_w, crop_y + avail_h))
        px, py = ml, mt

    # ユーザー位置補正を加算
    px += getattr(region, 'user_offset_x', 0)
    py += getattr(region, 'user_offset_y', 0)

    canvas.paste(resized, (px, py), resized if resized.mode == "RGBA" else None)
    return canvas


def apply_color_transparency(pil_img: Image.Image, color: QColor, tolerance: int,
                              contiguous: bool = True, seed_x: int = 0, seed_y: int = 0) -> Image.Image:
    """Replace pixels near `color` with transparent."""
    import numpy as np
    img = pil_img.convert("RGBA")
    arr = np.array(img, dtype=np.int32)
    tr, tg, tb = color.red(), color.green(), color.blue()
    dist = np.sqrt((arr[:, :, 0] - tr) ** 2 + (arr[:, :, 1] - tg) ** 2 + (arr[:, :, 2] - tb) ** 2)
    mask = dist <= tolerance
    if contiguous:
        # flood fill from seed point
        from collections import deque
        h, w = mask.shape
        visited = np.zeros((h, w), dtype=bool)
        q = deque()
        sy, sx = max(0, min(seed_y, h - 1)), max(0, min(seed_x, w - 1))
        if mask[sy, sx]:
            q.append((sy, sx))
            visited[sy, sx] = True
        while q:
            cy, cx = q.popleft()
            for dy, dx in [(-1, 0), (1, 0), (0, -1), (0, 1)]:
                ny, nx = cy + dy, cx + dx
                if 0 <= ny < h and 0 <= nx < w and not visited[ny, nx] and mask[ny, nx]:
                    visited[ny, nx] = True
                    q.append((ny, nx))
        arr[visited, 3] = 0
    else:
        arr[mask, 3] = 0
    return Image.fromarray(arr.astype(np.uint8), "RGBA")


def make_apng(frames_pil: list[Image.Image], durations: list[int], loop: int = 0,
              offsets: list[tuple] = None, canvas_size: tuple = None) -> bytes:
    """Build APNG bytes.
    各フレームをフルキャンバスサイズに合成してから書き込む。
    IHDR と IDAT/fdAT のサイズを一致させることで PNG ビューアでも正常表示できる。
    offsets = [(x,y),...] per frame. canvas_size = (w,h) or None=auto.
    """
    if not frames_pil:
        return b""

    def png_chunk(name: bytes, data: bytes) -> bytes:
        crc = struct.pack(">I", zlib.crc32(name + data) & 0xFFFFFFFF)
        return struct.pack(">I", len(data)) + name + data + crc

    def frame_to_raw(img: Image.Image) -> bytes:
        """RGBA PIL image → PNG filter-type-0 raw scanlines."""
        img = img.convert("RGBA")
        w, h = img.size
        raw_bytes = img.tobytes()
        row_len = w * 4
        raw = b""
        for row in range(h):
            raw += b"\x00" + raw_bytes[row * row_len:(row + 1) * row_len]
        return raw

    if offsets is None:
        offsets = [(0, 0)] * len(frames_pil)

    # キャンバスサイズを全フレームが収まる大きさに
    if canvas_size is None:
        cw = max(img.width  + max(0, ox) for img, (ox, oy) in zip(frames_pil, offsets))
        ch = max(img.height + max(0, oy) for img, (ox, oy) in zip(frames_pil, offsets))
        canvas_size = (cw, ch)
    cw, ch = canvas_size
    n = len(frames_pil)

    # 各フレームをフルキャンバスに合成した PIL 画像リストを作る
    composited = []
    for img, (ox, oy) in zip(frames_pil, offsets):
        canvas = Image.new("RGBA", (cw, ch), (0, 0, 0, 0))
        src = img.convert("RGBA")
        px = max(0, min(ox, cw - src.width))
        py = max(0, min(oy, ch - src.height))
        canvas.alpha_composite(src, (px, py))
        composited.append(canvas)

    out = b"\x89PNG\r\n\x1a\n"
    # IHDR: キャンバスサイズ, 8bit, RGBA(6), compression/filter/interlace=0
    out += png_chunk(b"IHDR", struct.pack(">IIBBBBB", cw, ch, 8, 6, 0, 0, 0))
    # acTL: フレーム数, ループ回数
    out += png_chunk(b"acTL", struct.pack(">II", n, loop))

    seq = 0
    for i, (canvas_frame, dur) in enumerate(zip(composited, durations)):
        # fcTL: フルキャンバスサイズ・オフセット0でシンプルに
        fctl = struct.pack(">IIIIIHHBB",
                           seq,       # sequence_number
                           cw, ch,    # width, height（フルキャンバス）
                           0, 0,      # x_offset, y_offset
                           dur, 1000, # delay_num, delay_den → dur/1000 秒
                           1,         # dispose_op: APNG_DISPOSE_OP_BACKGROUND
                           0)         # blend_op:   APNG_BLEND_OP_SOURCE
        out += png_chunk(b"fcTL", fctl)
        seq += 1

        compressed = zlib.compress(frame_to_raw(canvas_frame), 6)
        if i == 0:
            out += png_chunk(b"IDAT", compressed)
        else:
            out += png_chunk(b"fdAT", struct.pack(">I", seq) + compressed)
            seq += 1

    out += png_chunk(b"IEND", b"")
    return out


# ──────────────────────────────────────────────
# Right inspector panels
# ──────────────────────────────────────────────

class SplitPanel(QWidget):
    split_requested      = pyqtSignal(int, int, int, int, int, int)  # cols, rows, ovx, ovy, gx, gy
    auto_split_requested = pyqtSignal(int, int, int)                  # cols, rows, tolerance
    update_requested     = pyqtSignal()
    batch_requested      = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 6, 6, 6)

        # ── 外周余白グループ ──
        margin_grp = QGroupBox("外周余白（シートの周囲を除外）")
        mf = QFormLayout(margin_grp)
        self.outer_top    = QSpinBox(); self.outer_top.setRange(0, 2000);    self.outer_top.setValue(0)
        self.outer_bottom = QSpinBox(); self.outer_bottom.setRange(0, 2000); self.outer_bottom.setValue(0)
        self.outer_left   = QSpinBox(); self.outer_left.setRange(0, 2000);   self.outer_left.setValue(0)
        self.outer_right  = QSpinBox(); self.outer_right.setRange(0, 2000);  self.outer_right.setValue(0)
        mf.addRow("上 px", self.outer_top)
        mf.addRow("下 px", self.outer_bottom)
        mf.addRow("左 px", self.outer_left)
        mf.addRow("右 px", self.outer_right)
        layout.addWidget(margin_grp)

        grp = QGroupBox("分割設定")
        form = QFormLayout(grp)

        self.cols = QSpinBox(); self.cols.setRange(1, 40); self.cols.setValue(4)
        self.rows = QSpinBox(); self.rows.setRange(1, 40); self.rows.setValue(3)
        form.addRow("横分割（列数）", self.cols)
        form.addRow("縦分割（行数）", self.rows)

        btn_row_w = QWidget(grp)
        btn_row_lay = QHBoxLayout(btn_row_w)
        btn_row_lay.setContentsMargins(0, 0, 0, 0)
        self.btn_update = QPushButton("分割線を更新", btn_row_w)
        self.btn_auto   = QPushButton("🔍 自動分割", btn_row_w)
        btn_row_lay.addWidget(self.btn_update)
        btn_row_lay.addWidget(self.btn_auto)
        form.addRow(btn_row_w)

        self.bg_tolerance = QSpinBox(); self.bg_tolerance.setRange(1, 128); self.bg_tolerance.setValue(20)
        form.addRow("背景色許容誤差", self.bg_tolerance)

        # 自動分割の境界色ピッカー（None = 透過）
        self._auto_bg_color: 'Optional[QColor]' = None
        self.btn_bg_color = QPushButton("境界色: 透過", self)
        self.btn_bg_color.clicked.connect(self._pick_auto_bg_color)
        form.addRow("", self.btn_bg_color)

        self.overlap_x = QSpinBox(); self.overlap_x.setRange(-500, 500); self.overlap_x.setValue(20)
        self.overlap_y = QSpinBox(); self.overlap_y.setRange(-500, 500); self.overlap_y.setValue(20)
        form.addRow("重複(横) px", self.overlap_x)
        form.addRow("重複(縦) px", self.overlap_y)

        self.gap_x = QSpinBox(); self.gap_x.setRange(0, 200); self.gap_x.setValue(0)
        self.gap_y = QSpinBox(); self.gap_y.setRange(0, 200); self.gap_y.setValue(0)
        form.addRow("ギャップ(横) px", self.gap_x)
        form.addRow("ギャップ(縦) px", self.gap_y)

        layout.addWidget(grp)

        # ── 出力設定グループ ──
        out_grp = QGroupBox("出力設定")
        out_layout = QVBoxLayout(out_grp)
        out_layout.setSpacing(4)

        # テンプレート行（親ウィジェットを先に作る）
        tpl_w = QWidget(out_grp)
        tpl_row = QHBoxLayout(tpl_w)
        tpl_row.setContentsMargins(0, 0, 0, 0)
        self._lbl_tpl = QLabel("テンプレ:", tpl_w)   # 親を明示してGC防止
        tpl_row.addWidget(self._lbl_tpl)
        self.tpl_combo = QComboBox(tpl_w); self.tpl_combo.setMinimumWidth(90)
        tpl_row.addWidget(self.tpl_combo, 1)
        self.tpl_save_btn = QPushButton("保存", tpl_w); self.tpl_save_btn.setFixedWidth(44)
        self.tpl_delete_btn = QPushButton("削除", tpl_w); self.tpl_delete_btn.setFixedWidth(44)
        tpl_row.addWidget(self.tpl_save_btn)
        tpl_row.addWidget(self.tpl_delete_btn)
        out_layout.addWidget(tpl_w)

        # フォームは out_grp を親にして作る
        out_form_w = QWidget(out_grp)
        out_form = QFormLayout(out_form_w)
        out_form.setSpacing(3)
        out_form.setContentsMargins(0, 0, 0, 0)

        # キャンバスサイズ（親ウィジェット確立後に addWidget）
        size_w = QWidget(out_form_w)
        size_row = QHBoxLayout(size_w)
        size_row.setContentsMargins(0, 0, 0, 0)
        self.out_w = QSpinBox(size_w); self.out_w.setRange(1, 4096); self.out_w.setValue(512)
        self._lbl_x = QLabel("×", size_w)            # 親を明示してGC防止
        self.out_h = QSpinBox(size_w); self.out_h.setRange(1, 4096); self.out_h.setValue(512)
        size_row.addWidget(self.out_w)
        size_row.addWidget(self._lbl_x)
        size_row.addWidget(self.out_h)
        out_form.addRow("サイズ W×H", size_w)

        # 余白（4辺個別）— QGroupBox を親にして QFormLayout
        mg_grp = QGroupBox("余白（内側）", out_form_w)
        mg_form = QFormLayout(mg_grp)
        mg_form.setSpacing(2)
        mg_form.setContentsMargins(4, 4, 4, 4)
        self.mg_top    = QSpinBox(mg_grp); self.mg_top.setRange(0, 500);    self.mg_top.setValue(24)
        self.mg_bottom = QSpinBox(mg_grp); self.mg_bottom.setRange(0, 500); self.mg_bottom.setValue(24)
        self.mg_left   = QSpinBox(mg_grp); self.mg_left.setRange(0, 500);   self.mg_left.setValue(24)
        self.mg_right  = QSpinBox(mg_grp); self.mg_right.setRange(0, 500);  self.mg_right.setValue(24)
        mg_form.addRow("上 px", self.mg_top)
        mg_form.addRow("下 px", self.mg_bottom)
        mg_form.addRow("左 px", self.mg_left)
        mg_form.addRow("右 px", self.mg_right)
        out_form.addRow(mg_grp)

        # フィットモード
        self.fit_mode = QComboBox(out_form_w)
        self.fit_mode.addItems(["Fit", "Fill", "Original"])
        out_form.addRow("フィットモード", self.fit_mode)

        # イラスト配置（アンカー）
        anchor_grp = QGroupBox("イラスト配置", out_form_w)
        anchor_grid = QGridLayout(anchor_grp)
        anchor_grid.setSpacing(2)
        anchor_grid.setContentsMargins(4, 4, 4, 4)
        self._anchor_btns: dict[str, QPushButton] = {}
        positions = [
            ("top-left",     0, 0, "↖"), ("top",    0, 1, "↑"), ("top-right",    0, 2, "↗"),
            ("left",         1, 0, "←"), ("center", 1, 1, "＋"), ("right",       1, 2, "→"),
            ("bottom-left",  2, 0, "↙"), ("bottom", 2, 1, "↓"), ("bottom-right", 2, 2, "↘"),
        ]
        self._anchor_group = QButtonGroup(self)
        for anc, r, c, lbl in positions:
            btn = QPushButton(lbl, anchor_grp)  # 親を anchor_grp に明示
            btn.setCheckable(True); btn.setFixedSize(30, 26)
            btn.setProperty("anchor", anc)
            self._anchor_group.addButton(btn)
            anchor_grid.addWidget(btn, r, c)
            self._anchor_btns[anc] = btn
        self._anchor_btns["center"].setChecked(True)
        out_form.addRow(anchor_grp)

        # 背景色
        self.bg_btn = QPushButton("　透過　", out_form_w)
        self.bg_btn.setStyleSheet("background: transparent; border: 1px solid #888;")
        self._bg_color = QColor(0, 0, 0, 0)
        self.bg_btn.clicked.connect(self._pick_bg)
        out_form.addRow("背景色", self.bg_btn)

        # 出力形式
        self.fmt = QComboBox(out_form_w)
        self.fmt.addItems(["PNG (透過)", "JPEG", "PNG+JPEG"])
        out_form.addRow("出力形式", self.fmt)

        out_layout.addWidget(out_form_w)

        # 出力ボタン
        self.btn_export = QPushButton("▶ 出力", out_grp)
        self.btn_export.setStyleSheet("font-weight:bold; padding:6px; background:#2F6FEB; color:white;")
        out_layout.addWidget(self.btn_export)

        layout.addWidget(out_grp)
        layout.addStretch()

        self.btn_update.clicked.connect(self._emit_split)
        self.btn_auto.clicked.connect(
            lambda: self.auto_split_requested.emit(
                self.cols.value(), self.rows.value(), self.bg_tolerance.value()
            )
        )

        # バッチ処理ボタン
        self.btn_batch = QPushButton("📁 バッチ処理（複数ファイル）")
        self.btn_batch.clicked.connect(self.batch_requested.emit)
        layout.addWidget(self.btn_batch)

        # テンプレート操作
        self._templates: dict[str, dict] = {}
        self.tpl_combo.currentTextChanged.connect(self._load_template)
        self.tpl_save_btn.clicked.connect(self._save_template)
        self.tpl_delete_btn.clicked.connect(self._delete_template)

        # 設定の自動ロード
        self._load_settings()

    def _pick_bg(self):
        c = QColorDialog.getColor(self._bg_color, self, options=QColorDialog.ColorDialogOption.ShowAlphaChannel)
        if c.isValid():
            self._bg_color = c
            if c.alpha() == 0:
                self.bg_btn.setStyleSheet("background: transparent; border: 1px solid #888;")
                self.bg_btn.setText("　透過　")
            else:
                self.bg_btn.setStyleSheet(f"background: {c.name()}; border: 1px solid #888;")
                self.bg_btn.setText("　　　　")

    def _pick_auto_bg_color(self):
        from PyQt6.QtWidgets import QColorDialog, QMenu
        from PyQt6.QtGui import QPalette
        menu = QMenu(self)
        act_trans = menu.addAction("透過（アルファ=0）")
        act_pick  = menu.addAction("色を選択...")
        chosen = menu.exec(self.btn_bg_color.mapToGlobal(self.btn_bg_color.rect().bottomLeft()))
        if chosen == act_trans:
            self._auto_bg_color = None
            self.btn_bg_color.setText("境界色: 透過")
            self.btn_bg_color.setStyleSheet("")
        elif chosen == act_pick:
            init = self._auto_bg_color if self._auto_bg_color else QColor(255, 255, 255)
            col = QColorDialog.getColor(init, self, "境界色を選択")
            if col.isValid():
                self._auto_bg_color = col
                self.btn_bg_color.setText(f"境界色: {col.name()}")
                self.btn_bg_color.setStyleSheet(
                    f"background:{col.name()}; color:{'#000' if col.lightness() > 128 else '#fff'};"
                )

    def get_auto_bg_color(self) -> 'Optional[tuple]':
        """Returns (R,G,B) tuple or None for transparent."""
        if self._auto_bg_color is None:
            return None
        c = self._auto_bg_color
        return (c.red(), c.green(), c.blue())

    def _emit_split(self):
        self.split_requested.emit(
            self.cols.value(), self.rows.value(),
            self.overlap_x.value(), self.overlap_y.value(),
            self.gap_x.value(), self.gap_y.value()
        )

    def get_outer_margins(self):
        return (self.outer_top.value(), self.outer_bottom.value(),
                self.outer_left.value(), self.outer_right.value())

    def get_anchor(self) -> str:
        checked = self._anchor_group.checkedButton()
        return checked.property("anchor") if checked else "center"

    def get_output_settings(self) -> dict:
        return {
            "w": self.out_w.value(), "h": self.out_h.value(),
            "margin_top": self.mg_top.value(),
            "margin_bottom": self.mg_bottom.value(),
            "margin_left": self.mg_left.value(),
            "margin_right": self.mg_right.value(),
            "fit_mode": self.fit_mode.currentText(),
            "anchor": self.get_anchor(),
            "bg": self._bg_color,
            "fmt": self.fmt.currentText()
        }

    def _settings_to_dict(self) -> dict:
        bg = self._bg_color
        return {
            "w": self.out_w.value(), "h": self.out_h.value(),
            "mg_top": self.mg_top.value(), "mg_bottom": self.mg_bottom.value(),
            "mg_left": self.mg_left.value(), "mg_right": self.mg_right.value(),
            "fit_mode": self.fit_mode.currentText(),
            "anchor": self.get_anchor(),
            "bg_r": bg.red(), "bg_g": bg.green(), "bg_b": bg.blue(), "bg_a": bg.alpha(),
            "fmt": self.fmt.currentText()
        }

    def _apply_settings_dict(self, d: dict):
        self.out_w.setValue(d.get("w", 512))
        self.out_h.setValue(d.get("h", 512))
        self.mg_top.setValue(d.get("mg_top", d.get("margin", 24)))
        self.mg_bottom.setValue(d.get("mg_bottom", d.get("margin", 24)))
        self.mg_left.setValue(d.get("mg_left", d.get("margin", 24)))
        self.mg_right.setValue(d.get("mg_right", d.get("margin", 24)))
        self.fit_mode.setCurrentText(d.get("fit_mode", "Fit"))
        anchor = d.get("anchor", "center")
        if anchor in self._anchor_btns:
            self._anchor_btns[anchor].setChecked(True)
        bg = QColor(d.get("bg_r", 0), d.get("bg_g", 0), d.get("bg_b", 0), d.get("bg_a", 0))
        self._bg_color = bg
        if bg.alpha() == 0:
            self.bg_btn.setStyleSheet("background: transparent; border: 1px solid #888;")
            self.bg_btn.setText("　透過　")
        else:
            self.bg_btn.setStyleSheet(f"background: {bg.name()}; border: 1px solid #888;")
            self.bg_btn.setText("　　　　")
        self.fmt.setCurrentText(d.get("fmt", "PNG (透過)"))

    # ── テンプレート ──

    def _load_settings(self):
        qs = QSettings("ImageCraftStudio", "SplitPanel")
        # テンプレートロード
        tpl_raw = qs.value("templates", "{}")
        try:
            self._templates = json.loads(tpl_raw) if isinstance(tpl_raw, str) else {}
        except Exception:
            self._templates = {}
        self._refresh_tpl_combo()
        # 前回設定ロード
        last_raw = qs.value("last_output_settings", "{}")
        try:
            last = json.loads(last_raw) if isinstance(last_raw, str) else {}
        except Exception:
            last = {}
        if last:
            self._apply_settings_dict(last)

        split_raw = qs.value("last_split_settings", "{}")
        try:
            split = json.loads(split_raw) if isinstance(split_raw, str) else {}
        except Exception:
            split = {}
        if split:
            for sb, key in [
                (self.cols,         "cols"),
                (self.rows,         "rows"),
                (self.overlap_x,    "overlap_x"),
                (self.overlap_y,    "overlap_y"),
                (self.gap_x,        "gap_x"),
                (self.gap_y,        "gap_y"),
                (self.outer_top,    "outer_top"),
                (self.outer_bottom, "outer_bottom"),
                (self.outer_left,   "outer_left"),
                (self.outer_right,  "outer_right"),
            ]:
                if key in split:
                    sb.blockSignals(True)
                    sb.setValue(int(split[key]))
                    sb.blockSignals(False)

    def save_settings(self):
        """メインウィンドウ終了時に呼ぶ。"""
        qs = QSettings("ImageCraftStudio", "SplitPanel")
        qs.setValue("last_output_settings", json.dumps(self._settings_to_dict()))
        qs.setValue("templates", json.dumps(self._templates))
        split = {
            "cols": self.cols.value(),
            "rows": self.rows.value(),
            "overlap_x": self.overlap_x.value(),
            "overlap_y": self.overlap_y.value(),
            "gap_x": self.gap_x.value(),
            "gap_y": self.gap_y.value(),
            "outer_top":    self.outer_top.value(),
            "outer_bottom": self.outer_bottom.value(),
            "outer_left":   self.outer_left.value(),
            "outer_right":  self.outer_right.value(),
        }
        qs.setValue("last_split_settings", json.dumps(split))

    def _refresh_tpl_combo(self):
        self.tpl_combo.blockSignals(True)
        prev = self.tpl_combo.currentText()
        self.tpl_combo.clear()
        self.tpl_combo.addItem("（テンプレなし）")
        for name in sorted(self._templates.keys()):
            self.tpl_combo.addItem(name)
        idx = self.tpl_combo.findText(prev)
        if idx >= 0:
            self.tpl_combo.setCurrentIndex(idx)
        self.tpl_combo.blockSignals(False)

    def _load_template(self, name: str):
        if name in self._templates:
            self._apply_settings_dict(self._templates[name])

    def _save_template(self):
        name, ok = QInputDialog.getText(self, "テンプレート保存",
                                        "テンプレート名を入力してください:",
                                        text=self.tpl_combo.currentText() if self.tpl_combo.currentIndex() > 0 else "")
        if not ok or not name.strip():
            return
        name = name.strip()
        self._templates[name] = self._settings_to_dict()
        self._refresh_tpl_combo()
        self.tpl_combo.setCurrentText(name)
        QSettings("ImageCraftStudio", "SplitPanel").setValue("templates", json.dumps(self._templates))

    def _delete_template(self):
        name = self.tpl_combo.currentText()
        if name not in self._templates:
            return
        if QMessageBox.question(self, "確認", f"「{name}」を削除しますか？") != QMessageBox.StandardButton.Yes:
            return
        del self._templates[name]
        self._refresh_tpl_combo()
        QSettings("ImageCraftStudio", "SplitPanel").setValue("templates", json.dumps(self._templates))


class MaskPanel(QWidget):
    transparency_requested = pyqtSignal(QColor, int, bool)
    erase_requested = pyqtSignal(int)

    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 6, 6, 6)

        grp = QGroupBox("色指定透過")
        form = QFormLayout(grp)
        self.color_btn = QPushButton("　　　　")
        self.color_btn.setStyleSheet("background: white; border: 1px solid #888;")
        self._color = QColor(255, 255, 255)
        self.color_btn.clicked.connect(self._pick_color)
        form.addRow("基準色", self.color_btn)

        self.tolerance = QSlider(Qt.Orientation.Horizontal)
        self.tolerance.setRange(0, 255)
        self.tolerance.setValue(30)
        self.tol_label = QLabel("30")
        self.tolerance.valueChanged.connect(lambda v: self.tol_label.setText(str(v)))
        row = QHBoxLayout()
        row.addWidget(self.tolerance)
        row.addWidget(self.tol_label)
        w = QWidget(); w.setLayout(row)
        form.addRow("許容値", w)

        self.contiguous = QCheckBox("連続領域のみ")
        self.contiguous.setChecked(True)
        form.addRow(self.contiguous)

        self.btn_apply = QPushButton("透過を適用")
        form.addRow(self.btn_apply)
        self.btn_apply.clicked.connect(self._emit_transparency)
        layout.addWidget(grp)

        grp2 = QGroupBox("消しゴム / 復元")
        form2 = QFormLayout(grp2)
        self.brush_size = QSpinBox(); self.brush_size.setRange(1, 200); self.brush_size.setValue(20)
        form2.addRow("ブラシサイズ", self.brush_size)
        row2 = QHBoxLayout()
        self.btn_erase = QPushButton("消しゴム")
        self.btn_restore = QPushButton("復元")
        row2.addWidget(self.btn_erase)
        row2.addWidget(self.btn_restore)
        w2 = QWidget(); w2.setLayout(row2)
        form2.addRow(w2)
        layout.addWidget(grp2)
        layout.addStretch()

    def _pick_color(self):
        c = QColorDialog.getColor(self._color, self)
        if c.isValid():
            self._color = c
            self.color_btn.setStyleSheet(f"background: {c.name()}; border: 1px solid #888;")

    def _emit_transparency(self):
        self.transparency_requested.emit(self._color, self.tolerance.value(), self.contiguous.isChecked())


class FramePositionDialog(QDialog):
    """1フレームの位置（オフセット）とスケールを調整するダイアログ。前フレームをオニオンスキン表示。"""

    def __init__(self, frame: AnimFrame, frame_img: Image.Image,
                 prev_img: Optional[Image.Image], parent=None):
        super().__init__(parent)
        self.setWindowTitle("フレーム位置・スケール調整")
        self.resize(680, 560)
        self._frame = frame
        self._frame_img = frame_img.convert("RGBA")
        self._prev_img = prev_img.convert("RGBA") if prev_img else None
        self._ox = frame.offset_x
        self._oy = frame.offset_y
        self._sw = getattr(frame, 'scale_w', 1.0)
        self._sh = getattr(frame, 'scale_h', 1.0)
        self._fpd_info: dict = {}
        self._drag_mode: Optional[str] = None   # 'move' | 'resize'
        self._drag_handle: Optional[str] = None
        self._drag_start: Optional[tuple] = None
        self._drag_ox0 = 0; self._drag_oy0 = 0
        self._drag_sw0 = 1.0; self._drag_sh0 = 1.0
        self._drag_iw0 = 0; self._drag_ih0 = 0

        layout = QVBoxLayout(self)

        # ツールバー
        tb = QHBoxLayout()
        tb.addWidget(QLabel("前フレーム透過度:"))
        self._opacity_sl = QSlider(Qt.Orientation.Horizontal)
        self._opacity_sl.setRange(0, 90); self._opacity_sl.setValue(40)
        self._opacity_sl.setFixedWidth(100)
        self._opacity_sl.valueChanged.connect(self._refresh)
        tb.addWidget(self._opacity_sl)
        tb.addWidget(QLabel("  |  X:"))
        self._x_spin = QSpinBox(); self._x_spin.setRange(-4096, 4096); self._x_spin.setValue(self._ox); self._x_spin.setFixedWidth(70)
        self._x_spin.valueChanged.connect(self._on_spin_changed)
        tb.addWidget(self._x_spin)
        tb.addWidget(QLabel("Y:"))
        self._y_spin = QSpinBox(); self._y_spin.setRange(-4096, 4096); self._y_spin.setValue(self._oy); self._y_spin.setFixedWidth(70)
        self._y_spin.valueChanged.connect(self._on_spin_changed)
        tb.addWidget(self._y_spin)
        tb.addWidget(QLabel("  |  スケール W:"))
        self._sw_spin = QSpinBox(); self._sw_spin.setRange(10, 400); self._sw_spin.setValue(int(self._sw * 100)); self._sw_spin.setSuffix("%"); self._sw_spin.setFixedWidth(70)
        tb.addWidget(self._sw_spin)
        tb.addWidget(QLabel("H:"))
        self._sh_spin = QSpinBox(); self._sh_spin.setRange(10, 400); self._sh_spin.setValue(int(self._sh * 100)); self._sh_spin.setSuffix("%"); self._sh_spin.setFixedWidth(70)
        tb.addWidget(self._sh_spin)
        self._lock_chk = QCheckBox("縦横比固定"); self._lock_chk.setChecked(True)
        tb.addWidget(self._lock_chk)
        apply_btn = QPushButton("適用"); apply_btn.setFixedWidth(50)
        apply_btn.clicked.connect(self._apply_scale)
        tb.addWidget(apply_btn)
        tb.addStretch()
        layout.addLayout(tb)
        self._sw_spin.valueChanged.connect(lambda v: self._sh_spin.setValue(v) if self._lock_chk.isChecked() else None)
        self._sh_spin.valueChanged.connect(lambda v: self._sw_spin.setValue(v) if self._lock_chk.isChecked() else None)

        # キャンバス
        self._canvas_label = QLabel(self)
        self._canvas_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._canvas_label.setStyleSheet("background: #222;")
        self._canvas_label.setMinimumSize(500, 380)
        self._canvas_label.setMouseTracking(True)
        layout.addWidget(self._canvas_label, 1)

        btns = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel, self)
        btns.accepted.connect(self.accept)
        btns.rejected.connect(self.reject)
        layout.addWidget(btns)

        self._canvas_label.mousePressEvent   = self._mouse_press
        self._canvas_label.mouseMoveEvent    = self._mouse_move
        self._canvas_label.mouseReleaseEvent = self._mouse_release
        self._refresh()

    def _apply_scale(self):
        self._sw = self._sw_spin.value() / 100.0
        self._sh = self._sh_spin.value() / 100.0
        if self._lock_chk.isChecked():
            self._sh = self._sw
        self._refresh()

    def _on_spin_changed(self):
        self._ox = self._x_spin.value()
        self._oy = self._y_spin.value()
        self._refresh()

    def _get_handles(self) -> list:
        ri = self._fpd_info
        if not ri:
            return []
        ix, iy = ri['lbl_fx'], ri['lbl_fy']
        iw2, ih2 = ri['lbl_fw'], ri['lbl_fh']
        mx, my = ix + iw2 // 2, iy + ih2 // 2
        return [
            ('NW', ix, iy), ('N', mx, iy), ('NE', ix + iw2, iy),
            ('W', ix, my),                  ('E', ix + iw2, my),
            ('SW', ix, iy + ih2), ('S', mx, iy + ih2), ('SE', ix + iw2, iy + ih2),
        ]

    def _find_handle(self, x, y) -> Optional[str]:
        for name, hx, hy in self._get_handles():
            if abs(x - hx) <= 7 and abs(y - hy) <= 7:
                return name
        return None

    def _handle_cursor(self, name: str) -> Qt.CursorShape:
        return {'NW': Qt.CursorShape.SizeFDiagCursor, 'SE': Qt.CursorShape.SizeFDiagCursor,
                'NE': Qt.CursorShape.SizeBDiagCursor, 'SW': Qt.CursorShape.SizeBDiagCursor,
                'N': Qt.CursorShape.SizeVerCursor, 'S': Qt.CursorShape.SizeVerCursor,
                'W': Qt.CursorShape.SizeHorCursor, 'E': Qt.CursorShape.SizeHorCursor,
                }.get(name, Qt.CursorShape.SizeAllCursor)

    def _refresh(self):
        fw0, fh0 = self._frame_img.size
        fw = max(1, int(fw0 * self._sw)); fh = max(1, int(fh0 * self._sh))
        PAD = 40
        cw = max(fw + abs(self._ox) + PAD * 2, 400)
        ch = max(fh + abs(self._oy) + PAD * 2, 300)
        canvas = Image.new("RGBA", (cw, ch), (40, 40, 40, 255))

        if self._prev_img:
            opa = self._opacity_sl.value()
            if opa > 0:
                prev = self._prev_img.copy()
                r, g, b, a = prev.split()
                import numpy as np
                a_arr = (np.array(a) * opa / 100).astype(np.uint8)
                prev = Image.merge("RGBA", (r, g, b, Image.fromarray(a_arr)))
                canvas.alpha_composite(prev, (PAD, PAD))

        frame_scaled = self._frame_img.resize((fw, fh), Image.LANCZOS)
        px = PAD + self._ox; py = PAD + self._oy
        # clip to canvas
        sx0 = max(0, -px); sy0 = max(0, -py)
        dx0 = max(0, px); dy0 = max(0, py)
        pw = min(fw - sx0, cw - dx0); ph = min(fh - sy0, ch - dy0)
        if pw > 0 and ph > 0:
            canvas.alpha_composite(frame_scaled.crop((sx0, sy0, sx0+pw, sy0+ph)), (dx0, dy0))

        lw = max(self._canvas_label.width(), 400)
        lh = max(self._canvas_label.height(), 300)
        scale = min(lw / cw, lh / ch, 2.0)
        dw = max(1, int(cw * scale)); dh = max(1, int(ch * scale))
        disp = canvas.resize((dw, dh), Image.LANCZOS)

        # ラベル内のピクスマップ位置（AlignCenter）
        pix_ox = (lw - dw) // 2; pix_oy = (lh - dh) // 2

        # フレーム画像のラベル座標
        lbl_fx = pix_ox + int(px * scale)
        lbl_fy = pix_oy + int(py * scale)
        lbl_fw = int(fw * scale); lbl_fh = int(fh * scale)
        self._fpd_info = {
            'scale': scale, 'pix_ox': pix_ox, 'pix_oy': pix_oy,
            'dw': dw, 'dh': dh, 'lw': lw, 'lh': lh,
            'px': px, 'py': py, 'fw0': fw0, 'fh0': fh0,
            'lbl_fx': lbl_fx, 'lbl_fy': lbl_fy, 'lbl_fw': lbl_fw, 'lbl_fh': lbl_fh,
        }

        pm = QPixmap.fromImage(pil_to_qimage(disp))
        painter = QPainter(pm)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, False)
        # フレーム枠（水色）- ピクスマップ座標
        pfx = int(px * scale); pfy = int(py * scale)
        painter.setPen(QPen(QColor(0, 200, 255, 160), 1, Qt.PenStyle.DashLine))
        painter.drawRect(pfx, pfy, lbl_fw - 1, lbl_fh - 1)
        # ハンドル
        hs = 7
        painter.setPen(QPen(QColor(0, 120, 255), 1))
        for _, hx, hy in self._get_handles():
            hx_pm = hx - pix_ox; hy_pm = hy - pix_oy
            painter.fillRect(hx_pm - hs//2, hy_pm - hs//2, hs, hs, QColor(255, 255, 255, 220))
            painter.drawRect(hx_pm - hs//2, hy_pm - hs//2, hs, hs)
        painter.end()

        self._canvas_label.setPixmap(pm)

    def _mouse_press(self, e):
        if e.button() != Qt.MouseButton.LeftButton:
            return
        self.setFocus()
        x, y = e.pos().x(), e.pos().y()
        handle = self._find_handle(x, y)
        if handle:
            self._drag_mode = 'resize'
            self._drag_handle = handle
            self._drag_start = (x, y)
            ri = self._fpd_info
            self._drag_iw0 = ri.get('lbl_fw', 100)
            self._drag_ih0 = ri.get('lbl_fh', 100)
            self._drag_sw0 = self._sw
            self._drag_sh0 = self._sh
        else:
            self._drag_mode = 'move'
            self._drag_start = (x, y)
            self._drag_ox0 = self._ox
            self._drag_oy0 = self._oy

    def _mouse_move(self, e):
        x, y = e.pos().x(), e.pos().y()
        if self._drag_mode == 'move' and self._drag_start:
            sx, sy = self._drag_start
            scale = self._fpd_info.get('scale', 1.0) or 1.0
            dx = int((x - sx) / scale); dy = int((y - sy) / scale)
            self._ox = self._drag_ox0 + dx
            self._oy = self._drag_oy0 + dy
            self._x_spin.blockSignals(True); self._x_spin.setValue(self._ox); self._x_spin.blockSignals(False)
            self._y_spin.blockSignals(True); self._y_spin.setValue(self._oy); self._y_spin.blockSignals(False)
            self._refresh()
        elif self._drag_mode == 'resize' and self._drag_start:
            sx, sy = self._drag_start
            dx, dy = x - sx, y - sy
            handle = self._drag_handle
            old_w = self._drag_iw0; old_h = self._drag_ih0
            dw = -dx if handle in ('NW','W','SW') else (dx if handle in ('NE','E','SE') else 0)
            dh = -dy if handle in ('NW','N','NE') else (dy if handle in ('SW','S','SE') else 0)
            new_w = max(10, old_w + dw); new_h = max(10, old_h + dh)
            locked = self._lock_chk.isChecked()
            if locked:
                if handle in ('N','S'):
                    new_w = old_w * new_h / old_h
                elif handle in ('W','E'):
                    new_h = old_h * new_w / old_w
                else:
                    ratio = (new_w/old_w) if abs(dw) >= abs(dh) else (new_h/old_h)
                    new_w = old_w * ratio; new_h = old_h * ratio
            ri = self._fpd_info
            scale = ri.get('scale', 1.0) or 1.0
            fw0 = ri.get('fw0', 1); fh0 = ri.get('fh0', 1)
            self._sw = max(0.1, min(4.0, new_w / (fw0 * scale)))
            self._sh = max(0.1, min(4.0, new_h / (fh0 * scale)))
            self._sw_spin.blockSignals(True); self._sw_spin.setValue(int(self._sw * 100)); self._sw_spin.blockSignals(False)
            self._sh_spin.blockSignals(True); self._sh_spin.setValue(int(self._sh * 100)); self._sh_spin.blockSignals(False)
            self._refresh()
        else:
            handle = self._find_handle(x, y)
            self._canvas_label.setCursor(self._handle_cursor(handle) if handle else Qt.CursorShape.SizeAllCursor)

    def _mouse_release(self, e):
        self._drag_mode = None
        self._drag_handle = None
        self._drag_start = None

    def keyPressEvent(self, e):
        step = 1; k = e.key()
        if k == Qt.Key.Key_Left:   self._ox -= step; self._x_spin.blockSignals(True); self._x_spin.setValue(self._ox); self._x_spin.blockSignals(False); self._refresh()
        elif k == Qt.Key.Key_Right: self._ox += step; self._x_spin.blockSignals(True); self._x_spin.setValue(self._ox); self._x_spin.blockSignals(False); self._refresh()
        elif k == Qt.Key.Key_Up:    self._oy -= step; self._y_spin.blockSignals(True); self._y_spin.setValue(self._oy); self._y_spin.blockSignals(False); self._refresh()
        elif k == Qt.Key.Key_Down:  self._oy += step; self._y_spin.blockSignals(True); self._y_spin.setValue(self._oy); self._y_spin.blockSignals(False); self._refresh()
        else: super().keyPressEvent(e)

    def resizeEvent(self, e):
        super().resizeEvent(e)
        QTimer.singleShot(0, self._refresh)

    def result_offset(self) -> tuple:
        return (self._ox, self._oy)

    def result_scale(self) -> tuple:
        return (self._sw, self._sh)


class APNGPreviewWidget(QWidget):
    """APNGタブ選択時にメインエリアに表示するプレビューウィジェット。
    左：フレーム一覧（チェック・ドラッグ）、右：PIL合成プレビュー＋オニオンスキン＋マウス位置調整。
    """

    def __init__(self, anim_panel: 'AnimPanel', parent=None):
        super().__init__(parent)
        self._panel = anim_panel
        self._cur_idx = 0          # 現在表示中のフレームインデックス
        self._playing = False
        self._play_timer = QTimer(self)
        self._play_timer.setSingleShot(True)
        self._play_timer.timeout.connect(self._play_tick)
        self._drag_start: Optional[QPoint] = None
        self._ox_start = self._oy_start = 0
        self._scale = 1.0
        self._ignore_row_change = False  # プログラム的な行変更中フラグ

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # ── 左：フレーム一覧 ──
        left = QWidget(); left.setFixedWidth(200)
        left.setStyleSheet("background:#1e1e1e;")
        lv = QVBoxLayout(left); lv.setContentsMargins(4, 4, 4, 4); lv.setSpacing(4)

        lv.addWidget(QLabel("フレーム一覧"))
        self._frame_list = QListWidget()
        self._frame_list.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)
        self._frame_list.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self._frame_list.setIconSize(QSize(48, 48))
        # ユーザーのクリックによる行変更のみ処理（プログラム的な変更は _ignore_row_change で無視）
        self._frame_list.currentRowChanged.connect(self._on_user_row_changed)
        self._frame_list.model().rowsMoved.connect(self._on_rows_moved)
        lv.addWidget(self._frame_list, 1)

        nav_row = QHBoxLayout()
        btn_prev = QPushButton("◀ 前"); btn_prev.clicked.connect(self.go_prev)
        btn_next = QPushButton("次 ▶"); btn_next.clicked.connect(self.go_next)
        nav_row.addWidget(btn_prev); nav_row.addWidget(btn_next)
        lv.addLayout(nav_row)

        play_row = QHBoxLayout()
        self._btn_play = QPushButton("▶ 再生"); self._btn_play.clicked.connect(self.toggle_play)
        btn_stop = QPushButton("■ 停止"); btn_stop.clicked.connect(self.stop)
        play_row.addWidget(self._btn_play); play_row.addWidget(btn_stop)
        lv.addLayout(play_row)

        layout.addWidget(left)

        # ── 右：プレビュー＋コントロール ──
        right = QWidget()
        rv = QVBoxLayout(right); rv.setContentsMargins(4, 4, 4, 4); rv.setSpacing(4)

        self._preview_label = QLabel()
        self._preview_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._preview_label.setStyleSheet("background:#2a2a2a; border:1px solid #444;")
        self._preview_label.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self._preview_label.mousePressEvent   = self._mouse_press
        self._preview_label.mouseMoveEvent    = self._mouse_move
        self._preview_label.mouseReleaseEvent = self._mouse_release
        self._preview_label.setCursor(Qt.CursorShape.SizeAllCursor)
        rv.addWidget(self._preview_label, 1)

        ctrl_row = QHBoxLayout()
        ctrl_row.addWidget(QLabel("前フレーム透過度:"))
        self._opacity_sl = QSlider(Qt.Orientation.Horizontal)
        self._opacity_sl.setRange(0, 90); self._opacity_sl.setValue(40)
        self._opacity_sl.valueChanged.connect(self._refresh_preview)
        ctrl_row.addWidget(self._opacity_sl, 2)

        ctrl_row.addWidget(QLabel("  X:"))
        self._x_spin = QSpinBox(); self._x_spin.setRange(-4096, 4096); self._x_spin.setFixedWidth(70)
        self._x_spin.valueChanged.connect(self._on_x_spin_changed)
        ctrl_row.addWidget(self._x_spin)
        ctrl_row.addWidget(QLabel("Y:"))
        self._y_spin = QSpinBox(); self._y_spin.setRange(-4096, 4096); self._y_spin.setFixedWidth(70)
        self._y_spin.valueChanged.connect(self._on_y_spin_changed)
        ctrl_row.addWidget(self._y_spin)
        rv.addLayout(ctrl_row)

        self._info_label = QLabel("フレーム: -")
        rv.addWidget(self._info_label)

        layout.addWidget(right, 1)

    # ── フレームリスト同期 ──

    def refresh(self):
        """AnimPanel のフレームリストと同期して表示を更新する。"""
        frames = self._panel._frames
        self._ignore_row_change = True
        self._frame_list.clear()
        for i, f in enumerate(frames):
            icon = self._panel._thumbs.get(id(f), QIcon())
            item = QListWidgetItem(icon, f"{i+1:02d}  {f.duration_ms} ms")
            item.setData(Qt.ItemDataRole.UserRole, i)
            item.setCheckState(Qt.CheckState.Checked if f.enabled else Qt.CheckState.Unchecked)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsDragEnabled)
            self._frame_list.addItem(item)

        if frames:
            self._cur_idx = max(0, min(self._cur_idx, len(frames) - 1))
            self._frame_list.setCurrentRow(self._cur_idx)
        else:
            self._cur_idx = 0
        self._ignore_row_change = False
        self._refresh_preview()

    def _on_user_row_changed(self, row: int):
        """ユーザーがリストをクリックして行が変わったとき。"""
        if self._ignore_row_change or row < 0:
            return
        self._goto(row)

    def _on_rows_moved(self, *_):
        """ドラッグ＆ドロップで行が移動したとき、panel._frames の順序を更新する。"""
        frames = self._panel._frames
        new_order = []
        for i in range(self._frame_list.count()):
            old_idx = self._frame_list.item(i).data(Qt.ItemDataRole.UserRole)
            if isinstance(old_idx, int) and 0 <= old_idx < len(frames):
                new_order.append(frames[old_idx])
        self._panel._frames = new_order
        self._panel._refresh_list()
        # インデックスをリセットして再表示
        self._cur_idx = self._frame_list.currentRow()
        if self._cur_idx < 0:
            self._cur_idx = 0
        self.refresh()

    # ── フレーム移動（内部・外部共通） ──

    def _goto(self, idx: int):
        """指定インデックスのフレームに移動して表示更新。スピンボックスも同期。"""
        frames = self._panel._frames
        if not frames:
            return
        self._cur_idx = max(0, min(idx, len(frames) - 1))
        # リスト選択をプログラム的に更新（シグナル抑制）
        self._ignore_row_change = True
        self._frame_list.setCurrentRow(self._cur_idx)
        self._ignore_row_change = False
        # スピンボックス同期（シグナル抑制）
        f = frames[self._cur_idx]
        self._x_spin.blockSignals(True); self._x_spin.setValue(f.offset_x); self._x_spin.blockSignals(False)
        self._y_spin.blockSignals(True); self._y_spin.setValue(f.offset_y); self._y_spin.blockSignals(False)
        self._refresh_preview()

    def go_prev(self):
        frames = self._panel._frames
        if not frames:
            return
        self._goto((self._cur_idx - 1) % len(frames))

    def go_next(self):
        frames = self._panel._frames
        if not frames:
            return
        self._goto((self._cur_idx + 1) % len(frames))

    # ── 再生 ──

    def toggle_play(self):
        if self._playing:
            self.stop()
        else:
            self.play()

    def play(self):
        frames = self._panel._frames
        if not frames:
            return
        if self._playing:
            return
        self._playing = True
        self._btn_play.setText("⏸ 一時停止")
        self._schedule_next()

    def _schedule_next(self):
        frames = self._panel._frames
        if not frames or not self._playing:
            return
        ms = max(10, frames[self._cur_idx].duration_ms)
        self._play_timer.start(ms)

    def _play_tick(self):
        if not self._playing:
            return
        frames = self._panel._frames
        if not frames:
            self.stop(); return
        next_idx = (self._cur_idx + 1) % len(frames)
        # 再生中はスピンボックスを更新しない（位置調整との干渉を避ける）
        self._cur_idx = next_idx
        self._ignore_row_change = True
        self._frame_list.setCurrentRow(self._cur_idx)
        self._ignore_row_change = False
        self._refresh_preview()
        self._schedule_next()

    def stop(self):
        self._playing = False
        self._play_timer.stop()
        self._btn_play.setText("▶ 再生")

    # ── プレビュー描画 ──

    def _refresh_preview(self):
        frames = self._panel._frames
        if not frames or self._cur_idx >= len(frames):
            self._preview_label.setText("フレームなし")
            self._info_label.setText("フレーム: -")
            return

        f = frames[self._cur_idx]
        doc = f.doc
        if doc is None:
            self._preview_label.setText("ドキュメントなし")
            return
        region = next((r for r in doc.regions if r.id == f.region_id), None)
        if region is None:
            self._preview_label.setText("リージョンなし")
            return

        frame_img = render_region(doc.source_pil, region)
        fsw = getattr(f, 'scale_w', 1.0); fsh = getattr(f, 'scale_h', 1.0)
        if fsw != 1.0 or fsh != 1.0:
            frame_img = frame_img.resize((max(1, int(frame_img.width * fsw)), max(1, int(frame_img.height * fsh))), Image.LANCZOS)
        fw, fh = frame_img.size
        ox, oy = f.offset_x, f.offset_y

        # 出力キャンバスサイズ
        out_size = self._panel.get_canvas_size()
        if out_size is None:
            out_cw = max(fw + max(0, ox), 1)
            out_ch = max(fh + max(0, oy), 1)
        else:
            out_cw, out_ch = out_size

        # 前フレーム（オニオンスキン）
        prev_img = None
        if self._cur_idx > 0:
            pf = frames[self._cur_idx - 1]
            pdoc = pf.doc
            if pdoc:
                prev_r = next((r for r in pdoc.regions if r.id == pf.region_id), None)
                if prev_r:
                    prev_img = render_region(pdoc.source_pil, prev_r)

        # 余白付き描画キャンバス（出力枠を表示するため外側に余白）
        PAD = 40
        dc_w = out_cw + PAD * 2
        dc_h = out_ch + PAD * 2
        canvas = Image.new("RGBA", (dc_w, dc_h), (30, 30, 30, 255))

        def _composite_with_offset(dst: Image.Image, src: Image.Image, dx: int, dy: int):
            """dst に src を (dx, dy) の位置で合成。負オフセットも正しく処理する。"""
            dw2, dh2 = dst.size; sw2, sh2 = src.size
            # dst上の貼り付け開始座標（クランプ前）
            x0, y0 = dx, dy
            # src のどこから切り出すか
            sx0 = max(0, -x0); sy0 = max(0, -y0)
            # dst上の実際の貼り付け位置
            dx0 = max(0, x0); dy0 = max(0, y0)
            # 幅・高さ（dst境界内に収める）
            pw = min(sw2 - sx0, dw2 - dx0)
            ph = min(sh2 - sy0, dh2 - dy0)
            if pw <= 0 or ph <= 0:
                return
            dst.alpha_composite(src.crop((sx0, sy0, sx0 + pw, sy0 + ph)), (dx0, dy0))

        opa = self._opacity_sl.value()
        if prev_img is not None and opa > 0:
            pf = frames[self._cur_idx - 1]
            prev = prev_img.convert("RGBA")
            r_ch, g_ch, b_ch, a_ch = prev.split()
            import numpy as np
            a_arr = (np.array(a_ch) * opa / 100).astype(np.uint8)
            prev = Image.merge("RGBA", (r_ch, g_ch, b_ch, Image.fromarray(a_arr)))
            _composite_with_offset(canvas, prev, PAD + pf.offset_x, PAD + pf.offset_y)

        _composite_with_offset(canvas, frame_img, PAD + ox, PAD + oy)

        # 出力枠とチェッカー背景を QPainter で描画
        lw = max(self._preview_label.width(), 300)
        lh = max(self._preview_label.height(), 200)
        scale = min(lw / dc_w, lh / dc_h, 1.0)
        self._scale = scale
        dw = max(1, int(dc_w * scale)); dh = max(1, int(dc_h * scale))
        disp = canvas.resize((dw, dh), Image.LANCZOS)

        qimg = pil_to_qimage(disp)
        pm = QPixmap.fromImage(qimg)

        # QPainter でオーバーレイ描画
        painter = QPainter(pm)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        # 出力枠（水色の矩形）
        frame_rect_x = int(PAD * scale)
        frame_rect_y = int(PAD * scale)
        frame_rect_w = max(1, int(out_cw * scale))
        frame_rect_h = max(1, int(out_ch * scale))
        pen = QPen(QColor(0, 200, 255, 200))
        pen.setWidth(2)
        pen.setStyle(Qt.PenStyle.SolidLine)
        painter.setPen(pen)
        painter.drawRect(frame_rect_x, frame_rect_y, frame_rect_w, frame_rect_h)

        # 中心十字線
        cx = frame_rect_x + frame_rect_w // 2
        cy = frame_rect_y + frame_rect_h // 2
        pen2 = QPen(QColor(0, 200, 255, 100))
        pen2.setWidth(1)
        pen2.setStyle(Qt.PenStyle.DashLine)
        painter.setPen(pen2)
        painter.drawLine(frame_rect_x, cy, frame_rect_x + frame_rect_w, cy)
        painter.drawLine(cx, frame_rect_y, cx, frame_rect_y + frame_rect_h)

        # 目盛り（上辺・左辺に 50px 間隔）
        tick_color = QColor(180, 180, 180, 180)
        painter.setPen(QPen(tick_color))
        font = painter.font(); font.setPointSize(7); painter.setFont(font)
        tick_step = 50  # 画像座標px単位
        tick_px = tick_step * scale  # 表示px単位
        if tick_px >= 8:  # 小さすぎる場合は省略
            # 上辺（横軸）
            x = frame_rect_x
            v = 0
            while x <= frame_rect_x + frame_rect_w:
                painter.drawLine(int(x), frame_rect_y, int(x), frame_rect_y - 6)
                if v > 0:
                    painter.drawText(int(x) - 12, frame_rect_y - 8, f"{v}")
                x += tick_px; v += tick_step
            # 左辺（縦軸）
            y = frame_rect_y
            v = 0
            while y <= frame_rect_y + frame_rect_h:
                painter.drawLine(frame_rect_x, int(y), frame_rect_x - 6, int(y))
                if v > 0:
                    painter.drawText(frame_rect_x - 36, int(y) + 4, f"{v}")
                y += tick_px; v += tick_step

        painter.end()
        self._preview_label.setPixmap(pm)
        self._info_label.setText(
            f"フレーム: {self._cur_idx + 1} / {len(frames)}  |  "
            f"オフセット ({ox}, {oy})  |  {f.duration_ms} ms  |  "
            f"キャンバス {out_cw}×{out_ch}")

    # ── スピンボックス（オフセット調整） ──

    def _on_x_spin_changed(self, val: int):
        frames = self._panel._frames
        if 0 <= self._cur_idx < len(frames):
            frames[self._cur_idx].offset_x = val
            self._refresh_preview()

    def _on_y_spin_changed(self, val: int):
        frames = self._panel._frames
        if 0 <= self._cur_idx < len(frames):
            frames[self._cur_idx].offset_y = val
            self._refresh_preview()

    # ── マウスドラッグ（オフセット調整） ──

    def _mouse_press(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
            self._drag_start = e.pos()
            frames = self._panel._frames
            if 0 <= self._cur_idx < len(frames):
                self._ox_start = frames[self._cur_idx].offset_x
                self._oy_start = frames[self._cur_idx].offset_y

    def _mouse_move(self, e):
        if self._drag_start is None:
            return
        scale = self._scale or 1.0
        dx = int((e.pos().x() - self._drag_start.x()) / scale)
        dy = int((e.pos().y() - self._drag_start.y()) / scale)
        frames = self._panel._frames
        if 0 <= self._cur_idx < len(frames):
            frames[self._cur_idx].offset_x = self._ox_start + dx
            frames[self._cur_idx].offset_y = self._oy_start + dy
            self._x_spin.blockSignals(True); self._x_spin.setValue(frames[self._cur_idx].offset_x); self._x_spin.blockSignals(False)
            self._y_spin.blockSignals(True); self._y_spin.setValue(frames[self._cur_idx].offset_y); self._y_spin.blockSignals(False)
            self._refresh_preview()

    def _mouse_release(self, e):
        self._drag_start = None

    def resizeEvent(self, e):
        super().resizeEvent(e)
        QTimer.singleShot(0, self._refresh_preview)

    def add_current_frame(self, frame: AnimFrame):
        """外部からフレームを1枚追加してリフレッシュする。"""
        self._panel._frames.append(frame)
        self.refresh()


class AnimPanel(QWidget):
    frame_selected = pyqtSignal(int)
    apng_save_requested = pyqtSignal()
    frames_changed = pyqtSignal()          # フレームリスト変更（プレビュー更新用）

    def __init__(self, parent=None):
        super().__init__(parent)
        self._tabs: list = []              # DocumentTab リスト（MainWindow から設定）
        self._frames: list[AnimFrame] = [] # パネル独自のフレームリスト
        self._thumbs: dict = {}            # region_id -> QIcon

        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(4)

        # ── ソース選択 ──
        src_row = QHBoxLayout()
        src_row.addWidget(QLabel("ソース:"))
        self._src_combo = QComboBox(self)
        self._src_combo.setMinimumWidth(100)
        src_row.addWidget(self._src_combo, 1)
        self._btn_load = QPushButton("読み込む", self)
        self._btn_load.clicked.connect(self._load_from_source)
        src_row.addWidget(self._btn_load)
        layout.addLayout(src_row)

        # ── フレーム一覧 ──
        layout.addWidget(QLabel("フレーム一覧（ドラッグで順序変更、ダブルクリックで位置調整）"))
        self.frame_list = QListWidget(self)
        self.frame_list.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)
        self.frame_list.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.frame_list.setIconSize(QSize(48, 48))
        self.frame_list.setFixedHeight(200)
        self.frame_list.currentRowChanged.connect(self._on_row_changed)
        self.frame_list.itemDoubleClicked.connect(self._open_pos_dialog)
        self.frame_list.model().rowsMoved.connect(self._on_rows_moved)
        layout.addWidget(self.frame_list)

        btn_row = QHBoxLayout()
        self._btn_all   = QPushButton("全選択", self); self._btn_all.clicked.connect(self._check_all)
        self._btn_none  = QPushButton("全解除", self); self._btn_none.clicked.connect(self._uncheck_all)
        btn_row.addWidget(self._btn_all); btn_row.addWidget(self._btn_none); btn_row.addStretch()
        layout.addLayout(btn_row)

        # ── 時間設定 ──
        time_grp = QGroupBox("時間設定")
        tl = QVBoxLayout(time_grp)

        from PyQt6.QtWidgets import QRadioButton, QButtonGroup
        self._rb_fps   = QRadioButton("FPS指定", self)
        self._rb_total = QRadioButton("合計時間指定", self)
        self._rb_fps.setChecked(True)
        self._mode_grp = QButtonGroup(self)
        self._mode_grp.addButton(self._rb_fps)
        self._mode_grp.addButton(self._rb_total)
        rb_row = QHBoxLayout(); rb_row.addWidget(self._rb_fps); rb_row.addWidget(self._rb_total)
        tl.addLayout(rb_row)

        fps_row = QHBoxLayout()
        fps_row.addWidget(QLabel("FPS:"))
        self._fps_spin = QDoubleSpinBox(self)
        self._fps_spin.setRange(0.1, 120); self._fps_spin.setValue(10); self._fps_spin.setSingleStep(1)
        self._fps_spin.setDecimals(1)
        fps_row.addWidget(self._fps_spin)
        self._fps_info = QLabel("")
        fps_row.addWidget(self._fps_info); fps_row.addStretch()
        tl.addLayout(fps_row)

        tot_row = QHBoxLayout()
        tot_row.addWidget(QLabel("合計:"))
        self._total_spin = QDoubleSpinBox(self)
        self._total_spin.setRange(0.1, 3600); self._total_spin.setValue(1.0); self._total_spin.setSuffix(" 秒")
        self._total_spin.setDecimals(1)
        tot_row.addWidget(self._total_spin)
        self._total_info = QLabel("")
        tot_row.addWidget(self._total_info); tot_row.addStretch()
        tl.addLayout(tot_row)

        self._btn_apply = QPushButton("全フレームに適用", self)
        self._btn_apply.clicked.connect(self._apply_duration)
        tl.addWidget(self._btn_apply)

        layout.addWidget(time_grp)

        # ── ループ ──
        loop_row = QHBoxLayout()
        loop_row.addWidget(QLabel("ループ回数:"))
        self.loop_combo = QComboBox(self)
        self.loop_combo.addItems(["∞ (無限ループ)", "1回", "2回", "3回", "4回", "5回"])
        loop_row.addWidget(self.loop_combo); loop_row.addStretch()
        layout.addLayout(loop_row)

        # ── 出力サイズ ──
        size_grp = QGroupBox("出力サイズ（キャンバス）")
        sl = QVBoxLayout(size_grp)
        self._auto_size_chk = QCheckBox("フレームサイズから自動", self)
        self._auto_size_chk.setChecked(True)
        sl.addWidget(self._auto_size_chk)
        size_row = QHBoxLayout()
        size_row.addWidget(QLabel("幅:"))
        self._out_w_spin = QSpinBox(); self._out_w_spin.setRange(1, 8192); self._out_w_spin.setValue(512)
        size_row.addWidget(self._out_w_spin)
        size_row.addWidget(QLabel("高さ:"))
        self._out_h_spin = QSpinBox(); self._out_h_spin.setRange(1, 8192); self._out_h_spin.setValue(512)
        size_row.addWidget(self._out_h_spin)
        sl.addLayout(size_row)
        self._auto_size_chk.toggled.connect(lambda checked: (
            self._out_w_spin.setEnabled(not checked),
            self._out_h_spin.setEnabled(not checked)))
        self._out_w_spin.setEnabled(False)
        self._out_h_spin.setEnabled(False)
        self._auto_size_chk.toggled.connect(self.frames_changed)
        self._out_w_spin.valueChanged.connect(self.frames_changed)
        self._out_h_spin.valueChanged.connect(self.frames_changed)
        layout.addWidget(size_grp)

        # ── 保存 ──
        self.btn_save_apng = QPushButton("APNGとして保存...", self)
        self.btn_save_apng.setStyleSheet("font-weight:bold; padding:6px;")
        self.btn_save_apng.clicked.connect(self.apng_save_requested)
        layout.addWidget(self.btn_save_apng)
        layout.addStretch()

        # シグナル接続
        self._fps_spin.valueChanged.connect(self._update_time_info)
        self._total_spin.valueChanged.connect(self._update_time_info)
        self._rb_fps.toggled.connect(self._update_time_info)
        self.loop_combo.currentIndexChanged.connect(self._update_time_info)
        self._update_time_info()

    # ── タブ設定 ──

    def set_tabs(self, tabs: list):
        self._tabs = tabs
        self._refresh_src_combo()

    def _refresh_src_combo(self):
        self._src_combo.blockSignals(True)
        cur = self._src_combo.currentText()
        self._src_combo.clear()
        self._src_combo.addItem("現在のタブ")
        self._src_combo.addItem("全タブ")
        for tab in self._tabs:
            self._src_combo.addItem(Path(tab.doc.source_path).name)
        # restore selection
        idx = self._src_combo.findText(cur)
        self._src_combo.setCurrentIndex(max(0, idx))
        self._src_combo.blockSignals(False)

    # ── フレーム読み込み ──

    def _load_from_source(self):
        src = self._src_combo.currentText()
        if not self._tabs:
            return
        tabs_to_load = []
        if src == "現在のタブ":
            tabs_to_load = [self._tabs[0]] if self._tabs else []
        elif src == "全タブ":
            tabs_to_load = list(self._tabs)
        else:
            tabs_to_load = [t for t in self._tabs if Path(t.doc.source_path).name == src]

        self._frames = []
        self._thumbs = {}
        for tab in tabs_to_load:
            for r in tab.doc.regions:
                if not r.enabled:
                    continue
                f = AnimFrame(r.id, 100, doc=tab.doc)
                self._frames.append(f)
                # サムネイル生成
                cr = r.crop
                if r._erased_pil:
                    crop = r._erased_pil.copy()
                else:
                    crop = tab.doc.source_pil.crop(
                        (cr.x(), cr.y(), cr.x() + cr.width(), cr.y() + cr.height()))
                crop.thumbnail((48, 48))
                key = id(f)  # フレームインスタンスをキーに
                self._thumbs[key] = QIcon(QPixmap.fromImage(pil_to_qimage(crop)))
        self._refresh_list()
        self._update_time_info()
        self.frames_changed.emit()

    # ── リスト更新 ──

    def _refresh_list(self):
        self.frame_list.blockSignals(True)
        self.frame_list.clear()
        for i, f in enumerate(self._frames):
            icon = self._thumbs.get(id(f), QIcon())
            item = QListWidgetItem(icon, f"{i+1:02d}  {f.duration_ms} ms")
            item.setData(Qt.ItemDataRole.UserRole, i)
            item.setCheckState(Qt.CheckState.Checked if f.enabled else Qt.CheckState.Unchecked)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsDragEnabled)
            self.frame_list.addItem(item)
        self.frame_list.blockSignals(False)

    def _on_rows_moved(self, *_):
        """ドラッグ＆ドロップ後にフレームリストを並び替える。"""
        new_order = []
        for i in range(self.frame_list.count()):
            old_idx = self.frame_list.item(i).data(Qt.ItemDataRole.UserRole)
            new_order.append(self._frames[old_idx])
        self._frames = new_order
        self._refresh_list()

    def _check_all(self):
        for f in self._frames: f.enabled = True
        self._refresh_list()
        self.frames_changed.emit()

    def _uncheck_all(self):
        for f in self._frames: f.enabled = False
        self._refresh_list()
        self.frames_changed.emit()

    def _on_row_changed(self, row: int):
        self.frame_selected.emit(row)
        # チェック状態を同期
        if 0 <= row < self.frame_list.count():
            item = self.frame_list.item(row)
            if 0 <= row < len(self._frames):
                self._frames[row].enabled = (item.checkState() == Qt.CheckState.Checked)

    # ── 位置調整ダイアログ ──

    def _open_pos_dialog(self, item: QListWidgetItem):
        row = self.frame_list.row(item)
        if row < 0 or row >= len(self._frames):
            return
        f = self._frames[row]
        doc = f.doc
        if doc is None:
            return
        region = next((r for r in doc.regions if r.id == f.region_id), None)
        if not region:
            return
        frame_img = render_region(doc.source_pil, region)

        prev_img = None
        if row > 0:
            pf = self._frames[row - 1]
            pdoc = pf.doc
            if pdoc:
                prev_r = next((r for r in pdoc.regions if r.id == pf.region_id), None)
                if prev_r:
                    prev_img = render_region(pdoc.source_pil, prev_r)

        dlg = FramePositionDialog(f, frame_img, prev_img, self)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            f.offset_x, f.offset_y = dlg.result_offset()
            f.scale_w, f.scale_h = dlg.result_scale()
            self._refresh_list()

    # ── 時間設定 ──

    def _update_time_info(self):
        n = max(1, len(self._frames))
        fps_mode = self._rb_fps.isChecked()
        self._fps_spin.setEnabled(fps_mode)
        self._total_spin.setEnabled(not fps_mode)
        loop = self.get_loop()
        loop_label = "∞" if loop == 0 else f"{loop}回"
        if fps_mode:
            fps = self._fps_spin.value() or 1
            total_s = n / fps
            if loop == 0:
                loop_total = "∞"
            else:
                loop_total = f"{total_s * loop:.2f}"
            self._fps_info.setText(f"→ 合計 {total_s:.2f}秒 / ループ込み {loop_total}秒")
            self._total_info.setText("")
        else:
            total_s = self._total_spin.value() or 1
            fps = n / total_s
            if loop == 0:
                loop_total = "∞"
            else:
                loop_total = f"{total_s * loop:.2f}"
            self._total_info.setText(f"→ {fps:.2f} fps / ループ込み {loop_total}秒")
            self._fps_info.setText("")

    def _apply_duration(self):
        n = max(1, len(self._frames))
        if self._rb_fps.isChecked():
            fps = self._fps_spin.value() or 1
            ms = max(10, int(1000 / fps))
        else:
            total_s = self._total_spin.value() or 1
            ms = max(10, int(total_s * 1000 / n))
        for f in self._frames:
            f.duration_ms = ms
        self._refresh_list()
        self._update_time_info()
        self.frames_changed.emit()

    # ── 外部インターフェース ──

    def get_enabled_frames(self) -> list[AnimFrame]:
        # リストのチェック状態をフレームに反映してから返す
        for i in range(self.frame_list.count()):
            item = self.frame_list.item(i)
            if i < len(self._frames):
                self._frames[i].enabled = (item.checkState() == Qt.CheckState.Checked)
        return [f for f in self._frames if f.enabled]

    def get_canvas_size(self) -> Optional[tuple]:
        """出力キャンバスサイズ。自動の場合は None。"""
        if self._auto_size_chk.isChecked():
            return None
        return (self._out_w_spin.value(), self._out_h_spin.value())

    def get_loop(self) -> int:
        text = self.loop_combo.currentText()
        if "∞" in text:
            return 0
        return int(text.replace("回", ""))

    def set_preview(self, pil_img: Optional[Image.Image]):
        pass  # プレビューはキャンバスで確認するため廃止

    def populate_frames(self, frames: list[AnimFrame], thumbs: dict):
        pass  # 旧互換（新パネルは _load_from_source を使用）


# ──────────────────────────────────────────────
# Eraser dialog
# ──────────────────────────────────────────────

class EraserCanvas(QLabel):
    """消しゴム編集用キャンバス。左ドラッグで描画、スポイトモード時はクリックでサンプリング。"""
    stroke_begin = pyqtSignal()             # ストローク開始（アンドゥ用）
    erase_stroke = pyqtSignal(int, int)     # canvas座標 x, y（移動ごと）
    eyedrop_pick = pyqtSignal(int, int)     # canvas座標 x, y

    def __init__(self, pil_img: 'Image.Image', scale: float = 1.0, parent=None):
        super().__init__(parent)
        self._scale = scale
        self._pil = pil_img.convert("RGBA")
        self._painting = False
        self._eyedrop_mode = False
        self.show_center = True   # 中心線表示
        self.show_grid = False    # グリッド表示
        self.grid_step = 50       # グリッド間隔（画像px）
        self.setCursor(Qt.CursorShape.CrossCursor)
        self._refresh_pixmap()

    def set_eyedrop_mode(self, on: bool):
        self._eyedrop_mode = on
        self.setCursor(Qt.CursorShape.PointingHandCursor if on else Qt.CursorShape.CrossCursor)

    def update_image(self, pil_img: 'Image.Image'):
        self._pil = pil_img.convert("RGBA")
        self._refresh_pixmap()

    def _refresh_pixmap(self):
        w, h = self._pil.size
        qimg = QImage(self._pil.tobytes("raw", "RGBA"), w, h, QImage.Format.Format_RGBA8888)
        pix = QPixmap.fromImage(qimg)
        if self._scale != 1.0:
            pix = pix.scaled(int(w * self._scale), int(h * self._scale),
                             Qt.AspectRatioMode.KeepAspectRatio,
                             Qt.TransformationMode.SmoothTransformation)
        # オーバーレイ（中心線・グリッド）
        if self.show_center or self.show_grid:
            painter = QPainter(pix)
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, False)
            sw, sh = pix.width(), pix.height()
            if self.show_grid:
                pen = QPen(QColor(180, 180, 255, 120))
                pen.setWidth(1)
                painter.setPen(pen)
                step = max(1, int(self.grid_step * self._scale))
                x = step
                while x < sw:
                    painter.drawLine(x, 0, x, sh)
                    x += step
                y = step
                while y < sh:
                    painter.drawLine(0, y, sw, y)
                    y += step
            if self.show_center:
                pen = QPen(QColor(255, 80, 80, 200))
                pen.setWidth(1)
                pen.setStyle(Qt.PenStyle.DashLine)
                painter.setPen(pen)
                cx, cy = sw // 2, sh // 2
                painter.drawLine(cx, 0, cx, sh)
                painter.drawLine(0, cy, sw, cy)
            painter.end()
        self.setPixmap(pix)
        self.resize(pix.size())

    def _canvas_to_img(self, pos: QPoint):
        return int(pos.x() / self._scale), int(pos.y() / self._scale)

    def mousePressEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
            x, y = self._canvas_to_img(e.pos())
            if self._eyedrop_mode:
                self.eyedrop_pick.emit(x, y)
            else:
                self._painting = True
                self.stroke_begin.emit()      # ストローク開始を通知（1回だけ）
                self.erase_stroke.emit(x, y)

    def mouseMoveEvent(self, e):
        if self._painting and not self._eyedrop_mode:
            x, y = self._canvas_to_img(e.pos())
            self.erase_stroke.emit(x, y)

    def mouseReleaseEvent(self, e):
        self._painting = False


class EraserDialog(QDialog):
    """分割プレビューのダブルクリックで開く消しゴム編集ダイアログ。
    キャンバス枠・余白線のオーバーレイ表示、イラストのスケール調整を含む。
    """

    def __init__(self, region: 'Region', source_pil: 'Image.Image', parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"消しゴム編集 — {region.name}")
        self.resize(860, 680)

        self._region = region
        self._eraser_size = 20
        self._eraser_color: Optional[QColor] = None
        self._eyedrop_mode = False
        self._show_overlay = True          # キャンバス枠・余白線表示フラグ
        self._illust_scale = 100           # イラスト表示スケール(%)
        self._lock_aspect = True
        self._scale_w = 1.0
        self._scale_h = 1.0
        self._render_info: dict = {}
        self._show_center = True
        self._show_grid = False
        self._grid_step = 50
        self._drag_mode: Optional[str] = None   # 'resize', 'erase', 'move'
        self._drag_handle: Optional[str] = None
        self._drag_start_pos: Optional[tuple] = None
        self._drag_start_iw = 0
        self._drag_start_ih = 0
        self._drag_start_sw = 1.0
        self._drag_start_sh = 1.0
        self._eraser_mode = False       # True=消しゴム, False=移動（初期は移動）
        self._disp_zoom = 1.0           # 表示ズーム倍率
        self._disp_pan_x = 0            # ズーム時のパンオフセット（ピクセル）
        self._disp_pan_y = 0

        # 作業用 RGBA 画像を構築
        cr = region.crop
        if region._erased_pil:
            base = region._erased_pil.copy().convert("RGBA")
        else:
            base = source_pil.crop(
                (cr.x(), cr.y(), cr.x() + cr.width(), cr.y() + cr.height())
            ).convert("RGBA")
            if region._mask_pil:
                import numpy as np
                mask_l = region._mask_pil.resize(base.size, Image.NEAREST).convert("L")
                r_ch, g_ch, b_ch, a_ch = base.split()
                a_arr = np.minimum(np.array(a_ch), np.array(mask_l))
                base = Image.merge("RGBA", (r_ch, g_ch, b_ch, Image.fromarray(a_arr)))
        self._work = base

        self._setup_ui()

    def _setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setSpacing(4)

        # ── 行1：消しゴムツール ──
        tb = QHBoxLayout()
        tb.addWidget(QLabel("ブラシ:"))
        self._size_slider = QSlider(Qt.Orientation.Horizontal)
        self._size_slider.setRange(2, 120); self._size_slider.setValue(self._eraser_size)
        self._size_slider.setFixedWidth(100)
        self._size_slider.valueChanged.connect(lambda v: (setattr(self, '_eraser_size', v), self._update_brush_cursor()))
        tb.addWidget(self._size_slider)

        tb.addWidget(QLabel("  色:"))
        self._color_btn = QPushButton("透過")
        self._color_btn.setFixedWidth(60)
        self._color_btn.setStyleSheet("background: transparent; border:1px solid #888;")
        self._color_btn.clicked.connect(self._pick_color)
        tb.addWidget(self._color_btn)

        self._eyedrop_btn = QPushButton("スポイト")
        self._eyedrop_btn.setCheckable(True); self._eyedrop_btn.setFixedWidth(70)
        self._eyedrop_btn.toggled.connect(self._on_eyedrop_toggled)
        tb.addWidget(self._eyedrop_btn)

        self._undo_btn = QPushButton("元に戻す"); self._undo_btn.setFixedWidth(80)
        self._undo_btn.clicked.connect(self._undo)
        tb.addWidget(self._undo_btn)

        tb.addWidget(QLabel("  |"))
        self._mode_btn = QPushButton("✋ 移動")
        self._mode_btn.setCheckable(True); self._mode_btn.setChecked(False)
        self._mode_btn.setFixedWidth(90)
        self._mode_btn.toggled.connect(self._on_mode_toggled)
        tb.addWidget(self._mode_btn)

        tb.addWidget(QLabel("  |"))
        # ── 中心線・グリッド切替 ──
        self._center_chk = QCheckBox("中心線"); self._center_chk.setChecked(True)
        self._center_chk.toggled.connect(self._on_center_grid_toggled)
        tb.addWidget(self._center_chk)
        self._grid_chk = QCheckBox("グリッド"); self._grid_chk.setChecked(False)
        self._grid_chk.toggled.connect(self._on_center_grid_toggled)
        tb.addWidget(self._grid_chk)

        tb.addWidget(QLabel("  |"))
        # ── イラストスケール ──
        tb.addWidget(QLabel("スケール:"))
        self._scale_w_spin = QSpinBox(); self._scale_w_spin.setRange(10, 400)
        self._scale_w_spin.setValue(100); self._scale_w_spin.setSuffix("% W")
        self._scale_w_spin.setFixedWidth(80)
        tb.addWidget(self._scale_w_spin)
        self._scale_h_spin = QSpinBox(); self._scale_h_spin.setRange(10, 400)
        self._scale_h_spin.setValue(100); self._scale_h_spin.setSuffix("% H")
        self._scale_h_spin.setFixedWidth(80)
        tb.addWidget(self._scale_h_spin)
        self._lock_chk = QCheckBox("縦横比固定"); self._lock_chk.setChecked(True)
        tb.addWidget(self._lock_chk)
        self._apply_scale_btn = QPushButton("適用"); self._apply_scale_btn.setFixedWidth(50)
        self._apply_scale_btn.clicked.connect(self._apply_illust_scale)
        tb.addWidget(self._apply_scale_btn)

        self._scale_w_spin.valueChanged.connect(self._on_scale_w_changed)
        self._scale_h_spin.valueChanged.connect(self._on_scale_h_changed)

        tb.addWidget(QLabel("  |"))
        self._overlay_chk = QCheckBox("枠表示"); self._overlay_chk.setChecked(True)
        self._overlay_chk.toggled.connect(lambda c: (setattr(self, '_show_overlay', c), self._refresh_preview()))
        tb.addWidget(self._overlay_chk)

        tb.addStretch()
        layout.addLayout(tb)

        # ── メインエリア：出力プレビューのみ ──
        main_row = QHBoxLayout()

        self._preview_lbl = QLabel()
        self._preview_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._preview_lbl.setStyleSheet("background:#1a1a1a; border:1px solid #555;")
        self._preview_lbl.setMinimumSize(400, 400)
        self._preview_lbl.setCursor(Qt.CursorShape.CrossCursor)
        self._preview_lbl.setMouseTracking(True)
        main_row.addWidget(self._preview_lbl, 1)

        # プレビュー操作用の状態（位置・スケール）
        self._prev_drag_start: Optional[QPoint] = None
        self._prev_offset_x = 0    # 出力プレビュー上のイラストX位置補正（px）
        self._prev_offset_y = 0    # 同Y
        self._prev_illust_scale = 1.0   # 1.0 = 100%
        self._preview_lbl.mousePressEvent   = self._prev_mouse_press
        self._preview_lbl.mouseMoveEvent    = self._prev_mouse_move
        self._preview_lbl.mouseReleaseEvent = self._prev_mouse_release
        self._preview_lbl.wheelEvent        = self._prev_wheel

        layout.addLayout(main_row, 1)

        # アンドゥ履歴
        self._history: list['Image.Image'] = []

        # ── OK / Cancel ──
        btns = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        btns.accepted.connect(self.accept)
        btns.rejected.connect(self.reject)
        layout.addWidget(btns)

        # 初期プレビュー
        self._refresh_preview()

    # ── アンドゥ ──

    def _on_stroke_begin(self):
        """ストローク開始時に現在の画像を履歴へ保存（1ストローク=1履歴）。"""
        self._history.append(self._work.copy())
        if len(self._history) > 50:
            self._history.pop(0)

    # ── プレビュー操作（ドラッグ位置調整・ホイールスケール） ──

    def _prev_mouse_press(self, e):
        if e.button() != Qt.MouseButton.LeftButton:
            return
        self.setFocus()   # arrow keys: focus must be on dialog, not child widget
        x, y = e.pos().x(), e.pos().y()

        handle = self._find_handle_at(x, y)
        if handle:
            self._drag_mode = 'resize'
            self._drag_handle = handle
            self._drag_start_pos = (x, y)
            ri = self._render_info
            self._drag_start_iw = ri.get('img_w', 100)
            self._drag_start_ih = ri.get('img_h', 100)
            self._drag_start_sw = self._scale_w
            self._drag_start_sh = self._scale_h
            return

        if self._eyedrop_mode:
            pt = self._display_to_work(x, y)
            if pt:
                self._do_eyedrop(*pt)
            return

        if self._eraser_mode:
            self._drag_mode = 'erase'
            self._on_stroke_begin()
            pt = self._display_to_work(x, y, allow_outside=True)
            if pt:
                self._do_erase(*pt)
            return

        self._drag_mode = 'move'
        self._drag_start_pos = (x, y)

    def _prev_mouse_move(self, e):
        x, y = e.pos().x(), e.pos().y()

        if self._drag_mode == 'erase':
            pt = self._display_to_work(x, y, allow_outside=True)
            if pt:
                self._do_erase(*pt)
        elif self._drag_mode == 'move':
            if self._drag_start_pos:
                sx, sy = self._drag_start_pos
                dx, dy = x - sx, y - sy
                self._drag_start_pos = (x, y)
                sc = self._render_info.get('sc', 1.0) if self._render_info else 1.0
                self._prev_offset_x += int(dx / sc)
                self._prev_offset_y += int(dy / sc)
                self._refresh_preview()
        elif self._drag_mode == 'resize':
            self._handle_resize_move(x, y)
        else:
            # カーソル更新（ドラッグ中でない時）
            handle = self._find_handle_at(x, y)
            if handle:
                self._preview_lbl.setCursor(self._handle_cursor(handle))
            elif self._eyedrop_mode:
                self._preview_lbl.setCursor(Qt.CursorShape.PointingHandCursor)
            elif self._eraser_mode:
                self._update_brush_cursor()
            else:
                self._preview_lbl.setCursor(Qt.CursorShape.SizeAllCursor)

    def _prev_mouse_release(self, e):
        self._drag_mode = None
        self._drag_handle = None
        self._drag_start_pos = None

    def _prev_wheel(self, e):
        delta = e.angleDelta().y()
        factor = 1.01 if delta > 0 else 1 / 1.01
        ctrl = bool(e.modifiers() & Qt.KeyboardModifier.ControlModifier)
        if ctrl:
            # Ctrl+ホイール: 表示ズーム
            mx, my = e.position().x(), e.position().y()
            old_zoom = self._disp_zoom
            self._disp_zoom = max(0.25, min(8.0, self._disp_zoom * factor))
            # マウス位置を基準にパン補正
            ratio = self._disp_zoom / old_zoom
            self._disp_pan_x = int(self._disp_pan_x * ratio + mx * (ratio - 1))
            self._disp_pan_y = int(self._disp_pan_y * ratio + my * (ratio - 1))
        else:
            # 通常ホイール: 画像スケール
            self._scale_w = max(0.1, min(4.0, self._scale_w * factor))
            self._scale_h = max(0.1, min(4.0, self._scale_h * factor))
            self._prev_illust_scale = self._scale_w
            pct = int(self._scale_w * 100)
            self._scale_w_spin.blockSignals(True); self._scale_w_spin.setValue(pct); self._scale_w_spin.blockSignals(False)
            self._scale_h_spin.blockSignals(True); self._scale_h_spin.setValue(pct); self._scale_h_spin.blockSignals(False)
        self._refresh_preview()

    # ── スケール・オーバーレイ ──

    def _on_scale_w_changed(self, val: int):
        if self._lock_chk.isChecked():
            self._scale_h_spin.blockSignals(True)
            self._scale_h_spin.setValue(val)
            self._scale_h_spin.blockSignals(False)

    def _on_scale_h_changed(self, val: int):
        if self._lock_chk.isChecked():
            self._scale_w_spin.blockSignals(True)
            self._scale_w_spin.setValue(val)
            self._scale_w_spin.blockSignals(False)

    def _apply_illust_scale(self):
        self._scale_w = self._scale_w_spin.value() / 100.0
        self._scale_h = self._scale_h_spin.value() / 100.0
        if self._lock_chk.isChecked():
            self._scale_h = self._scale_w
        self._prev_illust_scale = self._scale_w
        self._refresh_preview()

    def _on_center_grid_toggled(self):
        self._show_center = self._center_chk.isChecked()
        self._show_grid = self._grid_chk.isChecked()
        self._refresh_preview()

    def _on_mode_toggled(self, checked: bool):
        self._eraser_mode = checked
        self._mode_btn.setText("✏ 消しゴム" if checked else "✋ 移動")
        self._update_brush_cursor()

    def _update_brush_cursor(self):
        """消しゴムモード時はブラシサイズに合わせた円カーソルを表示。"""
        if not self._eraser_mode or self._eyedrop_mode:
            self._preview_lbl.setCursor(
                Qt.CursorShape.PointingHandCursor if self._eyedrop_mode else Qt.CursorShape.SizeAllCursor)
            return
        ri = self._render_info
        eff_sc = ri.get('eff_sc', 1.0) if ri else 1.0
        total_w = ri.get('total_scale_w', 1.0) if ri else 1.0
        brush_disp = max(4, int(self._eraser_size * total_w * eff_sc))
        pm = QPixmap(brush_disp + 2, brush_disp + 2)
        pm.fill(Qt.GlobalColor.transparent)
        p = QPainter(pm)
        p.setPen(QPen(QColor(255, 255, 255, 200), 1))
        p.drawEllipse(1, 1, brush_disp, brush_disp)
        p.setPen(QPen(QColor(0, 0, 0, 120), 1))
        p.drawEllipse(0, 0, brush_disp + 1, brush_disp + 1)
        p.end()
        self._preview_lbl.setCursor(QCursor(pm, brush_disp // 2, brush_disp // 2))

    # ── ハンドル・座標変換ヘルパー ──

    def _get_handles(self) -> list:
        """8個のリサイズハンドル (name, cx, cy) を返す（表示座標）。"""
        ri = self._render_info
        if not ri:
            return []
        ix, iy, iw2, ih2 = ri['img_x'], ri['img_y'], ri['img_w'], ri['img_h']
        mx, my = ix + iw2 // 2, iy + ih2 // 2
        return [
            ('NW', ix,        iy),
            ('N',  mx,        iy),
            ('NE', ix + iw2,  iy),
            ('W',  ix,        my),
            ('E',  ix + iw2,  my),
            ('SW', ix,        iy + ih2),
            ('S',  mx,        iy + ih2),
            ('SE', ix + iw2,  iy + ih2),
        ]

    def _find_handle_at(self, x: int, y: int) -> Optional[str]:
        for name, hx, hy in self._get_handles():
            if abs(x - hx) <= 6 and abs(y - hy) <= 6:
                return name
        return None

    def _handle_cursor(self, name: str) -> Qt.CursorShape:
        return {
            'NW': Qt.CursorShape.SizeFDiagCursor,
            'SE': Qt.CursorShape.SizeFDiagCursor,
            'NE': Qt.CursorShape.SizeBDiagCursor,
            'SW': Qt.CursorShape.SizeBDiagCursor,
            'N':  Qt.CursorShape.SizeVerCursor,
            'S':  Qt.CursorShape.SizeVerCursor,
            'W':  Qt.CursorShape.SizeHorCursor,
            'E':  Qt.CursorShape.SizeHorCursor,
        }.get(name, Qt.CursorShape.SizeAllCursor)

    def _display_to_work(self, dx: int, dy: int, allow_outside: bool = False) -> Optional[tuple]:
        """表示（ラベル）座標 → _work 画像座標に変換。画像外なら None。"""
        ri = self._render_info
        if not ri:
            return None
        eff_sc = ri['eff_sc']
        crop_x, crop_y = ri['crop_x'], ri['crop_y']
        # ラベル座標 → ズームピクスマップ座標 → キャンバス座標
        zoom_x = dx + crop_x
        zoom_y = dy + crop_y
        cx = zoom_x / eff_sc
        cy = zoom_y / eff_sc
        px, py = ri['px'], ri['py']
        base_scale = ri['base_scale']
        bbox = ri['bbox']
        # キャンバス → クロップ画像座標
        img_cx = (cx - px) / (base_scale * self._scale_w) if base_scale * self._scale_w > 0 else 0
        img_cy = (cy - py) / (base_scale * self._scale_h) if base_scale * self._scale_h > 0 else 0
        # クロップ → _work 座標（bbox オフセット）
        bx0, by0 = (bbox[0], bbox[1]) if bbox else (0, 0)
        wx = int(img_cx + bx0)
        wy = int(img_cy + by0)
        iw, ih = self._work.size
        if allow_outside:
            return wx, wy
        if wx < 0 or wy < 0 or wx >= iw or wy >= ih:
            return None
        return wx, wy

    def _handle_resize_move(self, x: int, y: int):
        handle = self._drag_handle
        if not handle or not self._drag_start_pos:
            return
        sx, sy = self._drag_start_pos
        dx, dy = x - sx, y - sy
        old_w = self._drag_start_iw
        old_h = self._drag_start_ih
        if old_w <= 0 or old_h <= 0:
            return

        dw = -dx if handle in ('NW', 'W', 'SW') else (dx if handle in ('NE', 'E', 'SE') else 0)
        dh = -dy if handle in ('NW', 'N', 'NE') else (dy if handle in ('SW', 'S', 'SE') else 0)

        new_w = max(10, old_w + dw)
        new_h = max(10, old_h + dh)

        locked = self._lock_chk.isChecked()
        if locked:
            if handle in ('N', 'S'):
                ratio = new_h / old_h
                new_w = old_w * ratio
            elif handle in ('W', 'E'):
                ratio = new_w / old_w
                new_h = old_h * ratio
            else:
                ratio = (new_w / old_w) if abs(dw) >= abs(dh) else (new_h / old_h)
                new_w = old_w * ratio
                new_h = old_h * ratio

        ri = self._render_info
        if not ri:
            return
        base_scale = ri['base_scale']
        eff_sc = ri['eff_sc']
        iw_c = ri['iw']; ih_c = ri['ih']
        denom_w = iw_c * base_scale * eff_sc
        denom_h = ih_c * base_scale * eff_sc
        self._scale_w = max(0.1, min(4.0, new_w / denom_w if denom_w > 0 else 1.0))
        self._scale_h = max(0.1, min(4.0, new_h / denom_h if denom_h > 0 else 1.0))

        self._prev_illust_scale = self._scale_w
        self._scale_w_spin.blockSignals(True); self._scale_w_spin.setValue(int(self._scale_w * 100)); self._scale_w_spin.blockSignals(False)
        self._scale_h_spin.blockSignals(True); self._scale_h_spin.setValue(int(self._scale_h * 100)); self._scale_h_spin.blockSignals(False)
        self._refresh_preview()

    def _refresh_preview(self):
        """出力プレビューを更新する。ハンドル・オーバーレイも描画。"""
        r = self._region
        cw, ch = r.canvas_w, r.canvas_h
        ml, mt, mr, mb = r.margin_left, r.margin_top, r.margin_right, r.margin_bottom

        rendered = self._render_preview_image()
        if rendered is None:
            return

        ri = self._render_info
        sc   = ri['sc']
        eff_sc = ri['eff_sc']
        lw   = ri['lw']; lh = ri['lh']
        crop_x = ri['crop_x']; crop_y = ri['crop_y']

        # 表示ズームを含む全体ピクスマップを生成してクロップ
        zoom_w = max(1, int(cw * eff_sc)); zoom_h = max(1, int(ch * eff_sc))
        disp = rendered.resize((zoom_w, zoom_h), Image.LANCZOS)
        # ラベルサイズにクロップ（ズーム<1 のときはパディング）
        out = Image.new("RGBA", (lw, lh), (26, 26, 26, 255))
        src_x = max(0, crop_x); src_y = max(0, crop_y)
        dst_x = max(0, -crop_x); dst_y = max(0, -crop_y)
        copy_w = min(lw - dst_x, zoom_w - src_x)
        copy_h = min(lh - dst_y, zoom_h - src_y)
        if copy_w > 0 and copy_h > 0:
            region_crop = disp.crop((src_x, src_y, src_x + copy_w, src_y + copy_h))
            out.paste(region_crop, (dst_x, dst_y))

        pm = QPixmap.fromImage(pil_to_qimage(out))
        painter = QPainter(pm)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, False)

        if self._show_overlay:
            # 外枠（白）- ラベル座標に変換
            bx = int(0 * eff_sc) - crop_x; by = int(0 * eff_sc) - crop_y
            bw = zoom_w; bh = zoom_h
            pen_out = QPen(QColor(255, 255, 255, 180)); pen_out.setWidth(2)
            painter.setPen(pen_out)
            painter.drawRect(bx, by, bw - 1, bh - 1)

            # 余白線（黄色点線）
            pen_mg = QPen(QColor(255, 220, 0, 200)); pen_mg.setWidth(1)
            pen_mg.setStyle(Qt.PenStyle.DashLine)
            painter.setPen(pen_mg)
            left_px   = int(ml * eff_sc) - crop_x
            top_px    = int(mt * eff_sc) - crop_y
            right_px  = bw - int(mr * eff_sc) - 1 - crop_x
            bottom_px = bh - int(mb * eff_sc) - 1 - crop_y
            if 0 <= left_px < lw:
                painter.drawLine(left_px, 0, left_px, lh)
            if 0 <= right_px < lw:
                painter.drawLine(right_px, 0, right_px, lh)
            if 0 <= top_px < lh:
                painter.drawLine(0, top_px, lw, top_px)
            if 0 <= bottom_px < lh:
                painter.drawLine(0, bottom_px, lw, bottom_px)

        ri = self._render_info
        if ri:
            ix, iy, iw2, ih2 = ri['img_x'], ri['img_y'], ri['img_w'], ri['img_h']

            # 中心線（赤点線、画像内）
            if self._show_center:
                pen_c = QPen(QColor(255, 80, 80, 200)); pen_c.setWidth(1)
                pen_c.setStyle(Qt.PenStyle.DashLine); painter.setPen(pen_c)
                cx2 = ix + iw2 // 2; cy2 = iy + ih2 // 2
                painter.drawLine(cx2, iy, cx2, iy + ih2)
                painter.drawLine(ix, cy2, ix + iw2, cy2)

            # グリッド（青点線、画像内）
            if self._show_grid:
                pen_g = QPen(QColor(180, 180, 255, 120)); pen_g.setWidth(1)
                pen_g.setStyle(Qt.PenStyle.DotLine); painter.setPen(pen_g)
                step_disp = max(4, int(self._grid_step * ri.get('total_scale_w', 1.0) * sc))
                gx = ix + step_disp
                while gx < ix + iw2:
                    painter.drawLine(gx, iy, gx, iy + ih2); gx += step_disp
                gy = iy + step_disp
                while gy < iy + ih2:
                    painter.drawLine(ix, gy, ix + iw2, gy); gy += step_disp

            # リサイズハンドル（画像枠角・辺中央）
            hs = 7
            painter.setPen(QPen(QColor(0, 120, 255), 1))
            for _, hx, hy in self._get_handles():
                painter.fillRect(hx - hs // 2, hy - hs // 2, hs, hs, QColor(255, 255, 255, 220))
                painter.drawRect(hx - hs // 2, hy - hs // 2, hs, hs)

        painter.end()
        self._preview_lbl.setPixmap(pm)

    def _render_preview_image(self) -> Optional[Image.Image]:
        """現在の _work・スケール・オフセットを反映した出力画像を生成する。"""
        r = self._region
        cw, ch = r.canvas_w, r.canvas_h
        ml, mt, mr, mb = r.margin_left, r.margin_top, r.margin_right, r.margin_bottom
        avail_w = max(1, cw - ml - mr)
        avail_h = max(1, ch - mt - mb)

        cropped = self._work.copy()

        # 透明ボーダートリム
        bbox = cropped.getbbox()
        if bbox:
            cropped = cropped.crop(bbox)

        iw, ih = cropped.size

        # ベーススケール（fit/fill）にW/H独立スケールを掛ける
        if r.fit_mode == "Fill":
            base_scale = max(avail_w / iw, avail_h / ih) if iw and ih else 1
        else:
            base_scale = min(avail_w / iw, avail_h / ih) if iw and ih else 1
        total_w = base_scale * self._scale_w
        total_h = base_scale * self._scale_h
        nw, nh = max(1, int(iw * total_w)), max(1, int(ih * total_h))
        resized = cropped.resize((nw, nh), Image.LANCZOS)

        canvas = Image.new("RGBA", (cw, ch), (
            r.bg_color.red(), r.bg_color.green(), r.bg_color.blue(), r.bg_color.alpha()))

        # センタリング基準にオフセットを加算
        px = ml + (avail_w - nw) // 2 + self._prev_offset_x
        py = mt + (avail_h - nh) // 2 + self._prev_offset_y
        canvas.paste(resized, (px, py), resized if resized.mode == "RGBA" else None)

        # 表示スケールを計算してrender_infoを更新（_refresh_previewで使用）
        lw = max(self._preview_lbl.width() - 4, 200)
        lh = max(self._preview_lbl.height() - 4, 200)
        sc = min(lw / cw, lh / ch, 2.0)
        eff_sc = sc * self._disp_zoom
        # ズーム時のクロップ範囲
        zoom_w = int(cw * eff_sc); zoom_h = int(ch * eff_sc)
        crop_x = max(0, min(zoom_w - lw, zoom_w // 2 + self._disp_pan_x - lw // 2))
        crop_y = max(0, min(zoom_h - lh, zoom_h // 2 + self._disp_pan_y - lh // 2))
        if zoom_w <= lw: crop_x = -(lw - zoom_w) // 2  # center when smaller
        if zoom_h <= lh: crop_y = -(lh - zoom_h) // 2
        self._render_info = {
            'sc': sc,
            'eff_sc': eff_sc,
            'crop_x': crop_x, 'crop_y': crop_y,
            'lw': lw, 'lh': lh,
            'px': px, 'py': py,
            'nw': nw, 'nh': nh,
            'base_scale': base_scale,
            'total_scale_w': total_w, 'total_scale_h': total_h,
            'bbox': bbox,
            'iw': iw, 'ih': ih,
            # 画像のラベル座標
            'img_x': int(px * eff_sc) - crop_x,
            'img_y': int(py * eff_sc) - crop_y,
            'img_w': int(nw * eff_sc),
            'img_h': int(nh * eff_sc),
        }
        return canvas

    # ── スロット ──

    def _pick_color(self):
        """色ダイアログを開く。キャンセルで透過モードに戻す。"""
        col = QColorDialog.getColor(
            self._eraser_color if self._eraser_color else QColor(255, 255, 255),
            self, "消しゴム色を選択",
            QColorDialog.ColorDialogOption.ShowAlphaChannel
        )
        if col.isValid():
            self._eraser_color = col
            self._color_btn.setText("　　　　　")
            self._color_btn.setStyleSheet(
                f"background: rgba({col.red()},{col.green()},{col.blue()},{col.alpha()}); border:1px solid #888;"
            )
        else:
            self._eraser_color = None
            self._color_btn.setText("　透過　")
            self._color_btn.setStyleSheet("background: transparent; border:1px solid #888;")

    def _on_eyedrop_toggled(self, on: bool):
        self._eyedrop_mode = on
        self._update_brush_cursor()

    def _do_eyedrop(self, x: int, y: int):
        """画像から色をサンプリングしてスポイトモードを解除。"""
        iw, ih = self._work.size
        x = max(0, min(x, iw - 1))
        y = max(0, min(y, ih - 1))
        r, g, b, a = self._work.getpixel((x, y))
        self._eraser_color = QColor(r, g, b, a)
        self._color_btn.setText("　　　　　")
        self._color_btn.setStyleSheet(
            f"background: rgba({r},{g},{b},{a}); border:1px solid #888;"
        )
        self._eyedrop_btn.setChecked(False)

    def _do_erase(self, cx: int, cy: int):
        """ブラシ1点を消しゴムで塗る。履歴保存は stroke_begin で行うため、ここでは不要。"""
        import numpy as np

        r = self._eraser_size // 2
        iw, ih = self._work.size
        arr = np.array(self._work)

        # ブラシ範囲（矩形クリップ）
        x0, x1 = max(0, cx - r), min(iw, cx + r + 1)
        y0, y1 = max(0, cy - r), min(ih, cy + r + 1)
        if x0 >= x1 or y0 >= y1:
            return

        # 円形マスク生成
        ys, xs = np.ogrid[y0:y1, x0:x1]
        mask = (xs - cx) ** 2 + (ys - cy) ** 2 <= r * r

        if self._eraser_color is None:
            # 透過: alpha = 0
            arr[y0:y1, x0:x1, 3][mask] = 0
        else:
            ec = self._eraser_color
            arr[y0:y1, x0:x1, 0][mask] = ec.red()
            arr[y0:y1, x0:x1, 1][mask] = ec.green()
            arr[y0:y1, x0:x1, 2][mask] = ec.blue()
            arr[y0:y1, x0:x1, 3][mask] = ec.alpha()

        self._work = Image.fromarray(arr, "RGBA")
        self._refresh_preview()

    def _undo(self):
        if self._history:
            self._work = self._history.pop()
            self._refresh_preview()

    def keyPressEvent(self, e):
        step = 1
        k = e.key()
        if k == Qt.Key.Key_Left:
            self._prev_offset_x -= step; self._refresh_preview()
        elif k == Qt.Key.Key_Right:
            self._prev_offset_x += step; self._refresh_preview()
        elif k == Qt.Key.Key_Up:
            self._prev_offset_y -= step; self._refresh_preview()
        elif k == Qt.Key.Key_Down:
            self._prev_offset_y += step; self._refresh_preview()
        else:
            super().keyPressEvent(e)

    def resizeEvent(self, e):
        super().resizeEvent(e)
        QTimer.singleShot(0, self._refresh_preview)

    def get_result(self) -> 'Image.Image':
        """消しゴム編集後の生画像を返す。スケール・オフセットは region に別途保存する。"""
        return self._work

    def get_user_scale(self) -> float:
        return self._prev_illust_scale

    def get_user_offset(self) -> tuple:
        return (self._prev_offset_x, self._prev_offset_y)


# ──────────────────────────────────────────────
# Bottom thumbnail tray
# ──────────────────────────────────────────────

class ThumbnailTray(QWidget):
    item_selected = pyqtSignal(list)
    item_double_clicked = pyqtSignal(int)   # region id

    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(2, 2, 2, 2)
        header = QHBoxLayout()
        lbl = QLabel("分割プレビュー（ダブルクリックで編集）")
        lbl.setStyleSheet("font-weight:bold; font-size:12px;")
        header.addWidget(lbl)
        self._count_label = QLabel()
        header.addWidget(self._count_label)
        header.addStretch()
        self.btn_all = QPushButton("すべて選択")
        self.btn_none = QPushButton("選択解除")
        header.addWidget(self.btn_all)
        header.addWidget(self.btn_none)
        layout.addLayout(header)

        self.list_widget = QListWidget()
        self.list_widget.setViewMode(QListWidget.ViewMode.IconMode)
        self.list_widget.setIconSize(QSize(80, 80))
        self.list_widget.setMovement(QListWidget.Movement.Static)
        self.list_widget.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.list_widget.setFixedHeight(110)
        self.list_widget.itemSelectionChanged.connect(self._on_selection)
        self.list_widget.itemDoubleClicked.connect(
            lambda item: self.item_double_clicked.emit(item.data(Qt.ItemDataRole.UserRole))
        )
        layout.addWidget(self.list_widget)

        self.btn_all.clicked.connect(self.list_widget.selectAll)
        self.btn_none.clicked.connect(self.list_widget.clearSelection)

    def populate(self, regions: list[Region], source: Image.Image):
        self.list_widget.clear()
        THUMB = 80  # サムネイルの最大辺(px)
        for r in regions:
            # render_region でキャンバス適用済みの画像を取得
            rendered = render_region(source, r)  # canvas_w × canvas_h の RGBA
            cw, ch = rendered.size
            sc = min(THUMB / cw, THUMB / ch, 1.0)
            tw = max(1, int(cw * sc)); th = max(1, int(ch * sc))
            thumb_pil = rendered.resize((tw, th), Image.LANCZOS)
            pm = QPixmap.fromImage(pil_to_qimage(thumb_pil))

            # キャンバス外枠（白）と余白線（黄色点線）を QPainter で描画
            painter = QPainter(pm)
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, False)

            # 外枠
            pen_out = QPen(QColor(255, 255, 255, 160)); pen_out.setWidth(1)
            painter.setPen(pen_out)
            painter.drawRect(0, 0, tw - 1, th - 1)

            # 余白線
            ml = int(r.margin_left  * sc); mr_px = tw - int(r.margin_right  * sc) - 1
            mt = int(r.margin_top   * sc); mb_px = th - int(r.margin_bottom * sc) - 1
            pen_mg = QPen(QColor(255, 220, 0, 180)); pen_mg.setWidth(1)
            pen_mg.setStyle(Qt.PenStyle.DashLine)
            painter.setPen(pen_mg)
            if ml > 0 and ml < tw:
                painter.drawLine(ml, 0, ml, th)
            if mr_px > 0 and mr_px < tw:
                painter.drawLine(mr_px, 0, mr_px, th)
            if mt > 0 and mt < th:
                painter.drawLine(0, mt, tw, mt)
            if mb_px > 0 and mb_px < th:
                painter.drawLine(0, mb_px, tw, mb_px)

            painter.end()

            icon = QIcon(pm)
            item = QListWidgetItem(icon, r.name)
            item.setData(Qt.ItemDataRole.UserRole, r.id)
            item.setCheckState(Qt.CheckState.Checked if r.enabled else Qt.CheckState.Unchecked)
            self.list_widget.addItem(item)
        n = len(regions)
        self._count_label.setText(f"({n}枚)")

    def _on_selection(self):
        ids = [self.list_widget.item(i).data(Qt.ItemDataRole.UserRole)
               for i in range(self.list_widget.count())
               if self.list_widget.item(i).isSelected()]
        self.item_selected.emit(ids)

    def get_enabled_ids(self) -> list[int]:
        ids = []
        for i in range(self.list_widget.count()):
            item = self.list_widget.item(i)
            if item.checkState() == Qt.CheckState.Checked:
                ids.append(item.data(Qt.ItemDataRole.UserRole))
        return ids


# ──────────────────────────────────────────────
# Export worker
# ──────────────────────────────────────────────

class ExportWorker(QThread):
    progress = pyqtSignal(int, int, str)
    finished = pyqtSignal(int, list)

    def __init__(self, doc: Document, region_ids: list[int], out_settings: dict,
                 save_dir: str, name_base: str, start: int, digits: int,
                 fmt: str, quality: int,
                 conflict_mode: str = "overwrite", conflict_suffix: str = "_new",
                 region_nums: Optional[list[int]] = None):
        super().__init__()
        self.doc = doc
        self.region_ids = region_ids
        self.out_settings = out_settings
        self.save_dir = save_dir
        self.name_base = name_base
        self.start_num = start
        self.digits = digits
        self.fmt = fmt
        self.quality = quality
        self.conflict_mode = conflict_mode      # "overwrite" / "suffix" / "skip"
        self.conflict_suffix = conflict_suffix  # mode="suffix" のときファイル名に付加する文字列
        # region_nums: 各regionの実際のファイル番号（Noneなら start+i 連番）
        self.region_nums = region_nums
        self._errors = []

    def _make_fpath(self, num: str, ext: str) -> str:
        """競合解決ルールを適用してファイルパスを返す。skip の場合は None。"""
        fname = f"{self.name_base}{num}.{ext}"
        fpath = os.path.join(self.save_dir, fname)
        if not os.path.exists(fpath) or self.conflict_mode == "overwrite":
            return fpath
        if self.conflict_mode == "skip":
            return None
        # suffix モード: 付加文字を入れたパスを返す
        fname = f"{self.name_base}{num}{self.conflict_suffix}.{ext}"
        return os.path.join(self.save_dir, fname)

    def run(self):
        regions = [r for r in self.doc.regions if r.id in self.region_ids]
        total = len(regions)
        for i, region in enumerate(regions):
            try:
                if self.region_nums and i < len(self.region_nums):
                    idx = self.region_nums[i]
                else:
                    idx = self.start_num + i
                num = str(idx).zfill(self.digits)
                ext = "jpg" if "JPEG" in self.fmt else "png"
                fpath = self._make_fpath(num, ext)
                if fpath is None:
                    # skip モードで既存ファイル → 出力しない
                    self.progress.emit(i + 1, total, f"SKIP: {self.name_base}{num}.{ext}")
                    continue
                rendered = render_region(self.doc.source_pil, region)
                if "JPEG" in self.fmt:
                    bg = Image.new("RGB", rendered.size, (255, 255, 255))
                    bg.paste(rendered, mask=rendered.split()[3] if rendered.mode == "RGBA" else None)
                    bg.save(fpath, "JPEG", quality=self.quality)
                else:
                    rendered.save(fpath, "PNG")
                self.progress.emit(i + 1, total, os.path.basename(fpath))
            except Exception as e:
                self._errors.append(str(e))
                self.progress.emit(i + 1, total, f"ERROR: {e}")
        self.finished.emit(total - len(self._errors), self._errors)


def _compute_job_fpaths(job: dict) -> list[str]:
    """ジョブが出力するファイルパス一覧を返す（競合チェック用）。"""
    ext = "jpg" if "JPEG" in job['fmt'] else "png"
    return [
        os.path.join(job['save_dir'],
                     f"{job['base']}{str(job['start'] + i).zfill(job['digits'])}.{ext}")
        for i in range(len(job['region_ids']))
    ]


class ConflictDialog(QDialog):
    """既存ファイルとの競合を解決するダイアログ。"""

    def __init__(self, conflicts: list[str], parent=None):
        super().__init__(parent)
        self.setWindowTitle("ファイルが既に存在します")
        self.resize(480, 360)
        layout = QVBoxLayout(self)

        layout.addWidget(QLabel(f"{len(conflicts)} 件のファイルが既に存在します:"))
        lst = QListWidget(self)
        for p in conflicts:
            lst.addItem(os.path.basename(p))
        lst.setFixedHeight(140)
        layout.addWidget(lst)

        layout.addWidget(QLabel("既存ファイルの扱い:"))

        from PyQt6.QtWidgets import QRadioButton, QButtonGroup
        self._grp = QButtonGroup(self)
        self._rb_overwrite = QRadioButton("上書きする", self)
        self._rb_suffix    = QRadioButton("文字を付加して保存する", self)
        self._rb_skip      = QRadioButton("出力しない（スキップ）", self)
        self._rb_overwrite.setChecked(True)
        for rb in (self._rb_overwrite, self._rb_suffix, self._rb_skip):
            self._grp.addButton(rb)
            layout.addWidget(rb)

        suf_row = QHBoxLayout()
        suf_row.addWidget(QLabel("付加する文字:"))
        self._suf_edit = QLineEdit("_new", self)
        self._suf_edit.setFixedWidth(120)
        suf_row.addWidget(self._suf_edit)
        suf_row.addStretch()
        layout.addLayout(suf_row)

        self._rb_suffix.toggled.connect(self._suf_edit.setEnabled)
        self._suf_edit.setEnabled(False)

        btns = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel, self)
        btns.accepted.connect(self.accept)
        btns.rejected.connect(self.reject)
        layout.addWidget(btns)

    def result_mode(self) -> str:
        if self._rb_suffix.isChecked():
            return "suffix"
        if self._rb_skip.isChecked():
            return "skip"
        return "overwrite"

    def result_suffix(self) -> str:
        return self._suf_edit.text() or "_new"


# ──────────────────────────────────────────────
# Main Window
# ──────────────────────────────────────────────

class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Image Craft Studio")
        self.resize(1400, 900)
        self._doc: Optional[Document] = None
        self._history = CommandHistory()
        self._current_mode = "split"  # split / edit / anim / preview
        self._doc_tabs: list[DocumentTab] = []
        self._current_tab_idx: int = -1
        # 全タブ出力キュー
        self._export_queue: list[dict] = []
        self._export_total_ok: int = 0
        self._export_all_errs: list = []
        self._export_prog: Optional[QProgressDialog] = None
        self._anim_timer = QTimer()
        self._anim_timer.timeout.connect(self._anim_tick)
        self._anim_frame_idx = 0
        self._anim_playing = False

        self._build_menu()
        self._build_toolbar()
        self._build_left_toolbar()
        self._build_central()
        self._build_right_panel()
        self._build_bottom_tray()
        self._build_status()
        self._update_actions()
        self.setAcceptDrops(True)  # ウィンドウ全体への D&D

    # ── D&D on MainWindow (ウィンドウ外縁へのドロップも受け付ける) ──
    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dragMoveEvent(self, event):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dropEvent(self, event):
        urls = event.mimeData().urls()
        paths = [u.toLocalFile() for u in urls if u.isLocalFile()]
        if paths:
            self.open_files(paths)

    # ── Build UI ──

    def _build_menu(self):
        mb = self.menuBar()

        fm = mb.addMenu("ファイル(&F)")
        self._act_open = QAction("開く...", self, shortcut="Ctrl+O", triggered=self._open_dialog)
        self._act_save = QAction("保存", self, shortcut="Ctrl+S", triggered=self._save_all)
        self._act_saveas = QAction("別名保存...", self, shortcut="Ctrl+Shift+S", triggered=self._save_as)
        fm.addAction(self._act_open)
        fm.addSeparator()
        fm.addAction(self._act_save)
        fm.addAction(self._act_saveas)
        fm.addSeparator()
        fm.addAction(QAction("終了", self, shortcut="Ctrl+Q", triggered=self.close))

        em = mb.addMenu("編集(&E)")
        self._act_undo = QAction("元に戻す", self, shortcut="Ctrl+Z", triggered=self._undo)
        self._act_redo = QAction("やり直し", self, shortcut="Ctrl+Y", triggered=self._redo)
        em.addAction(self._act_undo)
        em.addAction(self._act_redo)

        vm = mb.addMenu("表示(&V)")
        self._act_fit = QAction("最適表示", self, shortcut="Ctrl+0", triggered=lambda: self._canvas.fit_in_view())
        vm.addAction(self._act_fit)
        vm.addAction(QAction("拡大", self, shortcut="Ctrl++", triggered=lambda: self._canvas.scale(1.2, 1.2)))
        vm.addAction(QAction("縮小", self, shortcut="Ctrl+-", triggered=lambda: self._canvas.scale(1/1.2, 1/1.2)))

        am = mb.addMenu("アニメーション(&A)")
        am.addAction(QAction("APNGとして保存...", self, triggered=self._save_apng))
        am.addSeparator()

        # フレーム操作
        am.addAction(QAction("現在のタブを読み込む", self, triggered=lambda: self._anim_load_source("current")))
        am.addAction(QAction("全タブを読み込む", self, triggered=lambda: self._anim_load_source("all")))
        am.addSeparator()
        am.addAction(QAction("全フレームを選択", self, triggered=lambda: self._anim_select_all(True)))
        am.addAction(QAction("全フレームを解除", self, triggered=lambda: self._anim_select_all(False)))
        am.addAction(QAction("フレームリストをクリア", self, triggered=self._anim_clear_frames))
        am.addSeparator()

        # 時間設定
        am.addAction(QAction("FPS を設定...", self, triggered=self._anim_set_fps_dialog))
        am.addAction(QAction("合計時間を設定...", self, triggered=self._anim_set_total_dialog))
        am.addAction(QAction("全フレームに時間を適用", self, triggered=self._anim_apply_duration))
        am.addSeparator()

        # ループ設定
        loop_menu = am.addMenu("ループ回数")
        for label, val in [("∞ (無限)", 0), ("1回", 1), ("2回", 2), ("3回", 3), ("4回", 4), ("5回", 5)]:
            loop_menu.addAction(QAction(label, self, triggered=lambda _, v=val: self._anim_set_loop(v)))
        am.addSeparator()

        # パネル表示
        am.addAction(QAction("APNGパネルを表示", self, triggered=self._show_anim_panel))

        sm = mb.addMenu("設定(&S)")
        sm.addAction(QAction("線の色・太さ設定...", self, triggered=self._open_line_settings))

        hm = mb.addMenu("ヘルプ(&H)")
        hm.addAction(QAction("キーボードショートカット / 操作ガイド...", self, triggered=self._open_help))
        hm.addAction(QAction("バージョン情報...", self, triggered=self._open_about))

    def _build_toolbar(self):
        tb = QToolBar("メイン", self)
        tb.setMovable(False)
        self.addToolBar(tb)

        tb.addAction(QAction("開く", self, triggered=self._open_dialog))
        tb.addAction(QAction("保存", self, triggered=self._save_all))
        tb.addAction(QAction("別名保存", self, triggered=self._save_as))
        tb.addSeparator()
        self._act_undo2 = QAction("← 元に戻す", self, triggered=self._undo)
        self._act_redo2 = QAction("やり直し →", self, triggered=self._redo)
        tb.addAction(self._act_undo2)
        tb.addAction(self._act_redo2)
        tb.addSeparator()
        tb.addAction(QAction("拡大", self, triggered=lambda: self._canvas.scale(1.2, 1.2)))
        tb.addAction(QAction("縮小", self, triggered=lambda: self._canvas.scale(1/1.2, 1/1.2)))
        tb.addAction(QAction("100%", self, triggered=lambda: self._canvas.set_zoom(1.0)))
        tb.addAction(QAction("最適表示", self, triggered=lambda: self._canvas.fit_in_view()))
        tb.addSeparator()

        # mode buttons
        for label, mode in [("分割設定", "split"), ("プレビュー", "preview")]:
            a = QAction(label, self)
            a.triggered.connect(lambda checked, m=mode: self._set_mode(m))
            tb.addAction(a)

    def _build_left_toolbar(self):
        tb = QToolBar("ツール", self)
        tb.setOrientation(Qt.Orientation.Vertical)
        tb.setMovable(False)
        self.addToolBar(Qt.ToolBarArea.LeftToolBarArea, tb)

        tools = [
            ("選択", "select"),
            ("手のひら", "pan"),
        ]
        self._tool_actions: dict[str, QAction] = {}
        for label, tool in tools:
            a = QAction(label, self)
            a.setCheckable(True)
            a.triggered.connect(lambda checked, t=tool: self._select_tool(t))
            tb.addAction(a)
            self._tool_actions[tool] = a
        self._tool_actions["select"].setChecked(True)

        # ── APNG専用ツール（APNGタブ選択時のみ表示）──
        tb.addSeparator()
        self._apng_tool_actions: list[QAction] = []

        def _apng_act(label: str, callback) -> QAction:
            a = QAction(label, self)
            a.triggered.connect(callback)
            a.setVisible(False)
            tb.addAction(a)
            self._apng_tool_actions.append(a)
            return a

        _apng_act("◀ 前", lambda: self._apng_preview and self._apng_preview.go_prev())
        _apng_act("次 ▶", lambda: self._apng_preview and self._apng_preview.go_next())
        _apng_act("▶ 再生", lambda: self._apng_preview and self._apng_preview.toggle_play())
        _apng_act("■ 停止", lambda: self._apng_preview and self._apng_preview.stop())
        _apng_act("＋追加", self._anim_add_frame_from_tray)
        _apng_act("更新", lambda: self._apng_preview and self._apng_preview.refresh())

    def _build_central(self):
        central = QWidget()
        self.setCentralWidget(central)
        layout = QVBoxLayout(central)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # ── 画像タブバー ──
        self._img_tab_bar = QTabBar(central)
        self._img_tab_bar.setTabsClosable(True)
        self._img_tab_bar.setMovable(True)
        self._img_tab_bar.setExpanding(False)
        self._img_tab_bar.currentChanged.connect(self._on_img_tab_changed)
        self._img_tab_bar.tabCloseRequested.connect(self._on_img_tab_close)
        self._img_tab_bar.tabMoved.connect(self._on_img_tab_moved)
        layout.addWidget(self._img_tab_bar)

        self._canvas = CanvasView(self)
        self._canvas._open_files_callback = self.open_files
        self._canvas.region_selected.connect(self._on_region_selected)
        self._canvas.split_lines_changed.connect(self._on_split_lines_changed)
        self._canvas.outer_margin_changed.connect(self._on_outer_margin_changed)
        self._canvas.overlap_changed.connect(self._on_overlap_changed)

        self._bottom_tray = ThumbnailTray(self)
        self._bottom_tray.item_selected.connect(self._on_tray_selected)
        self._bottom_tray.item_double_clicked.connect(self._open_eraser_dialog)

        # 通常ビュー（キャンバス＋サムネイルトレイ）
        normal_view = QWidget()
        nv = QVBoxLayout(normal_view); nv.setContentsMargins(0, 0, 0, 0); nv.setSpacing(0)
        splitter = QSplitter(Qt.Orientation.Vertical)
        splitter.addWidget(self._canvas)
        splitter.addWidget(self._bottom_tray)
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 1)
        self._central_splitter = splitter
        nv.addWidget(splitter)

        # APNGプレビュービュー（AnimPanelは後から設定）
        self._apng_preview: Optional[APNGPreviewWidget] = None  # _build_right_panel後に設定

        # スタックウィジェット
        self._central_stack = QStackedWidget()
        self._central_stack.addWidget(normal_view)   # index 0: 通常
        # APNGプレビューは _build_right_panel後に addWidget する（index 1）

        layout.addWidget(self._central_stack)
        QTimer.singleShot(150, self._init_splitter)

    def _build_right_panel(self):
        dock = QDockWidget("インスペクタ", self)
        dock.setAllowedAreas(Qt.DockWidgetArea.RightDockWidgetArea)
        dock.setFeatures(QDockWidget.DockWidgetFeature.NoDockWidgetFeatures)

        self._right_tabs = QTabWidget()
        self._right_tabs.setMinimumWidth(280)

        self._split_panel = SplitPanel()
        self._split_panel.split_requested.connect(self._do_split)
        self._split_panel.auto_split_requested.connect(self._do_auto_split)
        self._split_panel.batch_requested.connect(self._open_batch_dialog)
        self._split_panel.btn_export.clicked.connect(self._save_as)
        self._split_panel.overlap_x.valueChanged.connect(self._on_overlap_spin_changed)
        self._split_panel.overlap_y.valueChanged.connect(self._on_overlap_spin_changed)
        self._right_tabs.addTab(self._split_panel, "分割")

        self._mask_panel = MaskPanel()
        self._mask_panel.transparency_requested.connect(self._do_transparency)
        self._right_tabs.addTab(self._mask_panel, "透過")

        self._anim_panel = AnimPanel()
        self._anim_panel.apng_save_requested.connect(self._save_apng)
        self._right_tabs.addTab(self._anim_panel, "APNG")
        self._right_tabs.currentChanged.connect(self._on_right_tab_changed)

        # APNGプレビューウィジェット（スタックのindex 1）
        self._apng_preview = APNGPreviewWidget(self._anim_panel)
        self._central_stack.addWidget(self._apng_preview)
        self._anim_panel.frames_changed.connect(self._apng_preview.refresh)

        scroll = QScrollArea()
        scroll.setWidget(self._right_tabs)
        scroll.setWidgetResizable(True)
        scroll.setMinimumWidth(300)
        dock.setWidget(scroll)
        self.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, dock)

    def _build_bottom_tray(self):
        pass  # built in _build_central

    def _build_status(self):
        sb = QStatusBar()
        self.setStatusBar(sb)
        self._status_size = QLabel("サイズ: —")
        self._status_zoom = QLabel("倍率: —")
        self._status_sel = QLabel("選択数: 0")
        self._status_out = QLabel("出力: —")
        self._status_fmt = QLabel("形式: PNG")
        self._status_dest = QLabel("保存先: 元画像と同じ場所")
        for w in [self._status_size, self._status_zoom, self._status_sel,
                  self._status_out, self._status_fmt, self._status_dest]:
            sb.addWidget(w)
            sb.addWidget(_sep())

    # ── Actions ──

    def _update_actions(self):
        has_doc = self._doc is not None
        self._act_save.setEnabled(has_doc)
        self._act_saveas.setEnabled(has_doc)
        self._act_undo.setEnabled(self._history.can_undo())
        self._act_redo.setEnabled(self._history.can_redo())
        self._act_undo2.setEnabled(self._history.can_undo())
        self._act_redo2.setEnabled(self._history.can_redo())

    def _open_dialog(self):
        paths, _ = QFileDialog.getOpenFileNames(
            self, "画像を開く", "",
            "画像ファイル (*.png *.jpg *.jpeg *.gif *.bmp *.webp)")
        if paths:
            self.open_files(paths)

    def open_files(self, paths: list[str]):
        if not paths:
            return
        for path in paths:
            try:
                img = Image.open(path)
                doc = Document(path, img)
            except Exception as e:
                QMessageBox.critical(self, "エラー", f"画像を開けませんでした:\n{e}")
                continue
            tab = DocumentTab(doc)
            tab.split_settings = self._capture_split_settings()
            self._save_tab_state()
            self._doc_tabs.append(tab)
            new_idx = len(self._doc_tabs) - 1
            self._img_tab_bar.blockSignals(True)
            self._img_tab_bar.addTab(Path(path).name)
            self._img_tab_bar.setCurrentIndex(new_idx)
            self._img_tab_bar.blockSignals(False)
            self._activate_tab(new_idx)  # 直接呼ぶ（setCurrentIndex は変化なし時にシグナルを出さないため）

    def _save_all(self):
        if not self._doc:
            return
        self._save_as()

    def _save_as(self):
        if not self._doc:
            return
        enabled = self._bottom_tray.get_enabled_ids()
        if not enabled:
            enabled = [r.id for r in self._doc.regions if r.enabled]
        if not enabled:
            QMessageBox.information(self, "情報", "分割されたセルがありません。先に「分割線を更新」を押してください。")
            return
        self._show_export_dialog(enabled)

    def _show_export_dialog(self, region_ids: list[int]):
        dlg = QDialog(self)
        dlg.setWindowTitle("出力設定")
        layout = QVBoxLayout(dlg)
        form = QFormLayout()

        out_settings = self._split_panel.get_output_settings()

        dest_edit = QLineEdit(str(Path(self._doc.source_path).parent))
        btn_browse = QPushButton("参照...")
        btn_browse.clicked.connect(lambda: dest_edit.setText(
            QFileDialog.getExistingDirectory(dlg, "保存先フォルダ", dest_edit.text()) or dest_edit.text()))
        row = QHBoxLayout(); row.addWidget(dest_edit); row.addWidget(btn_browse)
        rw = QWidget(); rw.setLayout(row)
        form.addRow("保存先", rw)

        src_name = Path(self._doc.source_path).stem
        name_edit = QLineEdit(src_name)
        form.addRow("ファイル名(基本名)", name_edit)

        suffix_edit = QLineEdit("_part")
        form.addRow("後に追加", suffix_edit)

        num_only_chk = QCheckBox("連番のみ（基本名なし・元位置番号使用）")
        form.addRow("", num_only_chk)

        start_spin = QSpinBox(); start_spin.setRange(1, 9999); start_spin.setValue(1)
        form.addRow("連番開始番号", start_spin)

        def _toggle_num_only(checked):
            name_edit.setEnabled(not checked)
            suffix_edit.setEnabled(not checked)
        num_only_chk.toggled.connect(_toggle_num_only)
        start_spin.setEnabled(True)

        digits_spin = QSpinBox(); digits_spin.setRange(1, 6); digits_spin.setValue(2)
        form.addRow("桁数", digits_spin)

        fmt_combo = QComboBox()
        fmt_combo.addItems(["PNG (透過)", "JPEG"])
        form.addRow("形式", fmt_combo)

        quality_spin = QSpinBox(); quality_spin.setRange(1, 100); quality_spin.setValue(90)
        form.addRow("JPEG品質", quality_spin)

        # 全タブ出力オプション（複数タブ開いている時のみ表示）
        all_tabs_chk = QCheckBox("全タブを出力")
        if len(self._doc_tabs) > 1:
            form.addRow("", all_tabs_chk)

        layout.addLayout(form)
        btns = QHBoxLayout()
        btn_ok = QPushButton("出力")
        btn_cancel = QPushButton("キャンセル")
        btns.addWidget(btn_ok)
        btns.addWidget(btn_cancel)
        layout.addLayout(btns)
        btn_cancel.clicked.connect(dlg.reject)
        btn_ok.clicked.connect(dlg.accept)

        if dlg.exec() != QDialog.DialogCode.Accepted:
            return

        save_dir = dest_edit.text()
        if not os.path.isdir(save_dir):
            os.makedirs(save_dir, exist_ok=True)

        use_pos_nums = num_only_chk.isChecked()
        base_suffix = "" if use_pos_nums else name_edit.text() + suffix_edit.text()
        fmt = fmt_combo.currentText()
        quality = quality_spin.value()
        start = start_spin.value()
        digits = digits_spin.value()
        # 元位置番号：全regionリスト内の1-based連番
        all_ids = [r.id for r in self._doc.regions]
        def _make_region_nums(ids):
            if not use_pos_nums:
                return None
            base = start_spin.value()
            return [all_ids.index(rid) + base if rid in all_ids else base for rid in ids]

        def _apply_out_settings(doc: Document, ids: list[int]):
            for r in doc.regions:
                if r.id in ids:
                    r.canvas_w = out_settings["w"]
                    r.canvas_h = out_settings["h"]
                    r.margin_top    = out_settings.get("margin_top",    out_settings.get("margin", 24))
                    r.margin_bottom = out_settings.get("margin_bottom", out_settings.get("margin", 24))
                    r.margin_left   = out_settings.get("margin_left",   out_settings.get("margin", 24))
                    r.margin_right  = out_settings.get("margin_right",  out_settings.get("margin", 24))
                    r.fit_mode = out_settings["fit_mode"]
                    r.anchor   = out_settings.get("anchor", "center")
                    r.bg_color = out_settings["bg"]

        if all_tabs_chk.isChecked() and len(self._doc_tabs) > 1:
            # 全タブ出力: 左タブから順に連番を引き継ぎながらジョブを作成
            jobs = []
            running_start = start
            for tab in self._doc_tabs:
                tab_ids = [r.id for r in tab.doc.regions if r.enabled]
                if not tab_ids:
                    continue
                _apply_out_settings(tab.doc, tab_ids)
                jobs.append({
                    'doc': tab.doc, 'region_ids': tab_ids, 'out_settings': out_settings,
                    'save_dir': save_dir, 'base': base_suffix,
                    'start': running_start, 'digits': digits,
                    'fmt': fmt, 'quality': quality,
                    'tab_name': Path(tab.doc.source_path).name,
                })
                running_start += len(tab_ids)
            if not jobs:
                QMessageBox.information(self, "情報", "出力するセルがありません。")
                return
            # 競合チェック
            conflict_mode, conflict_suffix = self._resolve_conflicts(jobs)
            if conflict_mode is None:
                return  # キャンセル
            for j in jobs:
                j['conflict_mode'] = conflict_mode
                j['conflict_suffix'] = conflict_suffix
            total_regions = sum(len(j['region_ids']) for j in jobs)
            self._export_queue = jobs
            self._export_total_ok = 0
            self._export_all_errs = []
            self._export_prog = QProgressDialog("出力中...", "キャンセル", 0, total_regions, self)
            self._export_prog.setWindowModality(Qt.WindowModality.WindowModal)
            self._export_prog.show()
            self._start_next_export_job()
        else:
            # 現在タブのみ出力
            _apply_out_settings(self._doc, region_ids)
            single_job = {
                'doc': self._doc, 'region_ids': region_ids, 'out_settings': out_settings,
                'save_dir': save_dir, 'base': base_suffix,
                'start': start, 'digits': digits, 'fmt': fmt, 'quality': quality,
            }
            conflict_mode, conflict_suffix = self._resolve_conflicts([single_job])
            if conflict_mode is None:
                return  # キャンセル
            prog = QProgressDialog("出力中...", "キャンセル", 0, len(region_ids), self)
            prog.setWindowModality(Qt.WindowModality.WindowModal)
            self._worker = ExportWorker(
                self._doc, region_ids, out_settings,
                save_dir, base_suffix,
                start, digits, fmt, quality,
                conflict_mode, conflict_suffix,
                region_nums=_make_region_nums(region_ids)
            )
            self._worker.progress.connect(lambda done, total, name: (
                prog.setValue(done),
                prog.setLabelText(f"保存中: {name}")
            ))
            self._worker.finished.connect(lambda ok, errs: (
                prog.close(),
                QMessageBox.information(self, "完了", f"{ok}枚を保存しました。" +
                    (f"\nエラー {len(errs)}件" if errs else ""))
            ))
            prog.show()
            self._worker.start()

    def _resolve_conflicts(self, jobs: list[dict]) -> tuple:
        """全ジョブの出力パスを確認し、競合があればダイアログを出す。
        戻り値: (conflict_mode, conflict_suffix) または (None, None) でキャンセル。
        """
        conflicts = []
        for j in jobs:
            for fp in _compute_job_fpaths(j):
                if os.path.exists(fp):
                    conflicts.append(fp)
        if not conflicts:
            return ("overwrite", "")   # 競合なし → そのまま上書き（実際には上書き不要）
        dlg = ConflictDialog(conflicts, self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return (None, None)
        return (dlg.result_mode(), dlg.result_suffix())

    # ── タブ管理 ──

    def _capture_split_settings(self) -> dict:
        sp = self._split_panel
        return {
            'cols': sp.cols.value(), 'rows': sp.rows.value(),
            'overlap_x': sp.overlap_x.value(), 'overlap_y': sp.overlap_y.value(),
            'gap_x': sp.gap_x.value(), 'gap_y': sp.gap_y.value(),
            'outer_top': sp.outer_top.value(), 'outer_bottom': sp.outer_bottom.value(),
            'outer_left': sp.outer_left.value(), 'outer_right': sp.outer_right.value(),
        }

    def _restore_split_settings(self, settings: dict):
        if not settings:
            return
        sp = self._split_panel
        for attr, key in [
            ('cols', 'cols'), ('rows', 'rows'),
            ('overlap_x', 'overlap_x'), ('overlap_y', 'overlap_y'),
            ('gap_x', 'gap_x'), ('gap_y', 'gap_y'),
            ('outer_top', 'outer_top'), ('outer_bottom', 'outer_bottom'),
            ('outer_left', 'outer_left'), ('outer_right', 'outer_right'),
        ]:
            w = getattr(sp, attr)
            w.blockSignals(True)
            w.setValue(settings.get(key, w.value()))
            w.blockSignals(False)

    def _save_tab_state(self):
        idx = self._current_tab_idx
        if idx < 0 or idx >= len(self._doc_tabs):
            return
        tab = self._doc_tabs[idx]
        tab.history = self._history
        tab.vsegs = copy.deepcopy(self._canvas._vsegs)
        tab.hsegs = copy.deepcopy(self._canvas._hsegs)
        tab.vseg_pair = copy.deepcopy(self._canvas._vseg_pair)
        tab.hseg_pair = copy.deepcopy(self._canvas._hseg_pair)
        tab.outer_bounds = self._canvas._outer_bounds
        tab.overlap_x = self._canvas._overlap_x
        tab.overlap_y = self._canvas._overlap_y
        tab.split_settings = self._capture_split_settings()

    def _activate_tab(self, idx: int):
        tab = self._doc_tabs[idx]
        self._current_tab_idx = idx
        self._doc = tab.doc
        self._history = tab.history
        self._canvas.set_document(self._doc)
        if tab.vsegs:
            self._canvas.set_split_lines_2d(tab.vsegs, tab.hsegs, tab.outer_bounds)
            if tab.vseg_pair:
                self._canvas._vseg_pair = copy.deepcopy(tab.vseg_pair)
                self._canvas._hseg_pair = copy.deepcopy(tab.hseg_pair)
                self._canvas._rebuild_split_line_items()
            self._canvas.set_overlap(tab.overlap_x, tab.overlap_y)
        self._canvas.fit_in_view()
        self._restore_split_settings(tab.split_settings)
        self._bottom_tray.populate(self._doc.regions, self._doc.source_pil)
        self._update_status()
        self._update_actions()
        self.setWindowTitle(f"Image Craft Studio — {Path(self._doc.source_path).name}")
        self._anim_panel.set_tabs(self._doc_tabs)
        self._anim_panel._refresh_src_combo()

    def _on_img_tab_changed(self, new_idx: int):
        if new_idx < 0 or new_idx >= len(self._doc_tabs):
            return
        if self._current_tab_idx == new_idx:
            return
        self._save_tab_state()
        self._activate_tab(new_idx)

    def _on_img_tab_moved(self, from_idx: int, to_idx: int):
        tab = self._doc_tabs.pop(from_idx)
        self._doc_tabs.insert(to_idx, tab)
        self._current_tab_idx = self._img_tab_bar.currentIndex()

    def _on_img_tab_close(self, idx: int):
        self._save_tab_state()
        self._doc_tabs.pop(idx)
        self._img_tab_bar.blockSignals(True)
        self._img_tab_bar.removeTab(idx)
        self._img_tab_bar.blockSignals(False)
        if not self._doc_tabs:
            self._doc = None
            self._history = CommandHistory()
            self._current_tab_idx = -1
            self._canvas.set_document(None)
            self._bottom_tray.populate([], Image.new("RGBA", (1, 1)))
            self.setWindowTitle("Image Craft Studio")
            self._update_actions()
        else:
            new_idx = min(idx, len(self._doc_tabs) - 1)
            self._current_tab_idx = -1
            self._img_tab_bar.setCurrentIndex(new_idx)

    # ── 全タブ出力 ──

    def _start_next_export_job(self):
        if not self._export_queue:
            self._export_prog.close()
            QMessageBox.information(self, "完了",
                f"{self._export_total_ok}枚を保存しました。" +
                (f"\nエラー {len(self._export_all_errs)}件" if self._export_all_errs else ""))
            return
        job = self._export_queue.pop(0)
        w = ExportWorker(
            job['doc'], job['region_ids'], job['out_settings'],
            job['save_dir'], job['base'],
            job['start'], job['digits'], job['fmt'], job['quality'],
            job.get('conflict_mode', 'overwrite'), job.get('conflict_suffix', '_new')
        )
        self._worker = w
        tab_name = job['tab_name']
        prog_offset = self._export_total_ok  # このジョブ開始時点の累計完了数
        def _on_prog(done, total, name, _tab=tab_name, _off=prog_offset):
            self._export_prog.setValue(_off + done)
            self._export_prog.setLabelText(f"[{_tab}] {name}")
        w.progress.connect(_on_prog)
        w.finished.connect(self._on_export_job_done)
        w.start()

    def _on_export_job_done(self, ok: int, errs: list):
        self._export_total_ok += ok
        self._export_all_errs.extend(errs)
        self._start_next_export_job()

    def _output_settings_dialog(self):
        pass  # placeholder — use split panel for now

    def _set_mode(self, mode: str):
        self._current_mode = mode
        if mode == "anim":
            self._right_tabs.setCurrentIndex(2)
        elif mode == "split":
            self._right_tabs.setCurrentIndex(0)
        elif mode == "preview":
            self._right_tabs.setCurrentIndex(1)

    def _select_tool(self, tool: str):
        for t, a in self._tool_actions.items():
            a.setChecked(t == tool)
        self._canvas.set_tool(tool)

    # ── Split ──

    def _push_history(self):
        if not self._doc:
            return
        snap = [r.copy() for r in self._doc.regions]
        self._history.push(snap)
        self._update_actions()

    def _do_split(self, cols: int, rows: int, ovx: int, ovy: int, gx: int, gy: int):
        if not self._doc:
            QMessageBox.information(self, "情報", "先に画像を開いてください。")
            return
        self._push_history()
        img = self._doc.source_pil
        sw, sh = img.size

        # 外周余白
        ot, ob, ol, or_ = self._split_panel.get_outer_margins()
        eff_x  = ol
        eff_y  = ot
        eff_w  = sw - ol - or_
        eff_h  = sh - ot - ob
        if eff_w <= 0 or eff_h <= 0:
            return

        # 分割線位置を均等計算（ギャップ考慮）
        cell_w = (eff_w - gx * (cols - 1)) / cols
        cell_h = (eff_h - gy * (rows - 1)) / rows
        vlines = [int(eff_x + (i + 1) * cell_w + i * gx) for i in range(cols - 1)]
        hlines = [int(eff_y + (i + 1) * cell_h + i * gy) for i in range(rows - 1)]

        outer_bounds = (eff_x, eff_y, eff_x + eff_w, eff_y + eff_h)
        self._canvas.set_overlap(ovx, ovy)
        self._canvas.set_split_lines(vlines, hlines, outer_bounds)
        # set_split_lines 後に _vsegs/_hsegs が均等初期化されているので、そこから再計算
        self._compute_regions_from_segs(self._canvas._vsegs, self._canvas._hsegs,
                                        outer_bounds, cols, rows, ovx, ovy)

    def _compute_regions_from_segs(self, vsegs, hsegs, outer_bounds, cols, rows, ovx, ovy):
        """セグメント2D配列からセルを計算して Document.regions を更新する。
        vsegs[row][line_idx]: 各行バンドの垂直分割線X座標
        hsegs[col][line_idx]: 各列バンドの水平分割線Y座標
        per-band 外周オフセットと per-cell 重複を適用する。
        """
        sw, sh = self._doc.source_pil.size
        eff_x, eff_y, eff_x2, eff_y2 = outer_bounds
        out = self._split_panel.get_output_settings()
        cv = self._canvas

        self._doc.regions = []
        rid = 1
        for row in range(rows):
            row_vsegs = vsegs[row] if row < len(vsegs) else (vsegs[0] if vsegs else [])
            for col in range(cols):
                # X境界：per-band 外周オフセット考慮
                ol_r = eff_x + (cv._outer_left_offsets[row]  if row < len(cv._outer_left_offsets)  else 0)
                or_r = eff_x2 + (cv._outer_right_offsets[row] if row < len(cv._outer_right_offsets) else 0)
                ot_c = eff_y + (cv._outer_top_offsets[col]    if col < len(cv._outer_top_offsets)    else 0)
                ob_c = eff_y2 + (cv._outer_bottom_offsets[col] if col < len(cv._outer_bottom_offsets) else 0)

                # x1: ペア右側線があればそれがこのセルの左境界
                if col == 0:
                    x1 = int(ol_r)
                else:
                    pair_x = (cv._vseg_pair[row][col - 1]
                               if row < len(cv._vseg_pair) and col - 1 < len(cv._vseg_pair[row])
                               else None)
                    x1 = int(pair_x) if pair_x is not None else int(row_vsegs[col - 1])
                x2 = int(or_r) if col == cols - 1 else int(row_vsegs[col])
                col_hsegs = hsegs[col] if col < len(hsegs) else (hsegs[0] if hsegs else [])
                # y1: ペア右側線があればそれがこのセルの上境界
                if row == 0:
                    y1 = int(ot_c)
                else:
                    pair_y = (cv._hseg_pair[col][row - 1]
                               if col < len(cv._hseg_pair) and row - 1 < len(cv._hseg_pair[col])
                               else None)
                    y1 = int(pair_y) if pair_y is not None else int(col_hsegs[row - 1])
                y2 = int(ob_c) if row == rows - 1 else int(col_hsegs[row])

                # per-cell 重複：この行の垂直分割線 col-1 の重複量
                ov_left  = cv._cell_ovlp_v[row][col - 1] if col > 0 and row < len(cv._cell_ovlp_v) and col - 1 < len(cv._cell_ovlp_v[row]) else 0
                ov_right = cv._cell_ovlp_v[row][col]     if col < cols - 1 and row < len(cv._cell_ovlp_v) and col < len(cv._cell_ovlp_v[row]) else 0
                oh_top   = cv._cell_ovlp_h[col][row - 1] if row > 0 and col < len(cv._cell_ovlp_h) and row - 1 < len(cv._cell_ovlp_h[col]) else 0
                oh_bot   = cv._cell_ovlp_h[col][row]     if row < rows - 1 and col < len(cv._cell_ovlp_h) and row < len(cv._cell_ovlp_h[col]) else 0

                cx = x1 - ovx - ov_left
                cy = y1 - ovy - oh_top
                cw = (x2 - x1) + ovx * 2 + ov_left + ov_right
                ch = (y2 - y1) + ovy * 2 + oh_top + oh_bot
                cx = max(0, cx)
                cy = max(0, cy)
                cw = min(cw, sw - cx)
                ch = min(ch, sh - cy)
                if cw > 0 and ch > 0:
                    r = Region(rid, cx, cy, cw, ch)
                    r.name = f"{rid:02d}"
                    r.canvas_w = out["w"]
                    r.canvas_h = out["h"]
                    r.margin_top    = out.get("margin_top",    out.get("margin", 24))
                    r.margin_bottom = out.get("margin_bottom", out.get("margin", 24))
                    r.margin_left   = out.get("margin_left",   out.get("margin", 24))
                    r.margin_right  = out.get("margin_right",  out.get("margin", 24))
                    r.fit_mode = out["fit_mode"]
                    r.anchor   = out.get("anchor", "center")
                    r.bg_color = out["bg"]
                    self._doc.regions.append(r)
                rid += 1

        self._canvas.refresh_regions()
        self._bottom_tray.populate(self._doc.regions, self._doc.source_pil)
        self._update_status()

    def _on_split_lines_changed(self, vsegs: list, hsegs: list):
        """分割線・外周・重複ドラッグ完了 → セルを再計算する。"""
        if not self._doc:
            return
        cols = self._split_panel.cols.value()
        rows = self._split_panel.rows.value()
        # 未分割のとき（vsegs/hsegs の形状が合わない）はスキップ
        n_vlines = cols - 1
        n_hlines = rows - 1
        if len(vsegs) != rows:
            return
        if n_vlines > 0 and (not vsegs or len(vsegs[0]) != n_vlines):
            return
        if len(hsegs) != cols:
            return
        if n_hlines > 0 and (not hsegs or len(hsegs[0]) != n_hlines):
            return
        ovx  = self._split_panel.overlap_x.value()
        ovy  = self._split_panel.overlap_y.value()
        outer_bounds = self._canvas._outer_bounds
        self._push_history()
        self._compute_regions_from_segs(vsegs, hsegs, outer_bounds, cols, rows, ovx, ovy)

    def _on_outer_margin_changed(self, top: int, bottom: int, left: int, right: int):
        """外周余白ハンドルドラッグ → スピンボックスをリアルタイム更新"""
        p = self._split_panel
        for sb, val in [(p.outer_top, top), (p.outer_bottom, bottom),
                        (p.outer_left, left), (p.outer_right, right)]:
            sb.blockSignals(True)
            sb.setValue(val)
            sb.blockSignals(False)

    def _do_auto_split(self, cols: int, rows: int, tolerance: int):
        """🔍 自動分割 — 画像の余白を検出して分割線を設定する。"""
        if not self._doc:
            QMessageBox.information(self, "情報", "先に画像を開いてください。")
            return
        self._push_history()
        img = self._doc.source_pil
        sw, sh = img.size
        sp = self._split_panel
        ot, ob, ol, or_ = sp.get_outer_margins()
        ovx = sp.overlap_x.value()
        ovy = sp.overlap_y.value()

        eff_x  = ol
        eff_y  = ot
        eff_x2 = sw - or_
        eff_y2 = sh - ob
        if eff_x2 <= eff_x or eff_y2 <= eff_y:
            QMessageBox.warning(self, "警告", "外周余白が画像サイズを超えています。")
            return

        region_img = img.crop((eff_x, eff_y, eff_x2, eff_y2))
        # 各セルのイラスト境界を個別検出 → 行・列ごとに異なる2D分割線
        bg_color = sp.get_auto_bg_color()
        raw_vsegs, raw_hsegs = auto_detect_splits(region_img, cols, rows, bg_color=bg_color, tolerance=tolerance)
        # 座標系を eff_x/eff_y オフセットで元画像座標に変換
        vsegs = [[eff_x + x for x in row] for row in raw_vsegs]
        hsegs = [[eff_y + y for y in col] for col in raw_hsegs]

        outer_bounds = (eff_x, eff_y, eff_x2, eff_y2)
        self._canvas.set_overlap(ovx, ovy)
        self._canvas.set_split_lines_2d(vsegs, hsegs, outer_bounds)
        sp.cols.blockSignals(True); sp.cols.setValue(cols); sp.cols.blockSignals(False)
        sp.rows.blockSignals(True); sp.rows.setValue(rows); sp.rows.blockSignals(False)
        self._compute_regions_from_segs(vsegs, hsegs, outer_bounds, cols, rows, ovx, ovy)

    def _open_batch_dialog(self):
        """バッチ処理ダイアログを開く。"""
        sp = self._split_panel
        ot, ob, ol, or_ = sp.get_outer_margins()
        params = {
            "cols": sp.cols.value(), "rows": sp.rows.value(),
            "ovx": sp.overlap_x.value(), "ovy": sp.overlap_y.value(),
            "gx": sp.gap_x.value(), "gy": sp.gap_y.value(),
            "ot": ot, "ob": ob, "ol": ol, "or_": or_,
        }
        dlg = BatchDialog(params, sp.get_output_settings(), self)
        dlg.exec()

    def _on_overlap_spin_changed(self):
        """重複スピンボックス変更 → 分割線リセットなしで regions をリアルタイム更新"""
        if not self._doc or not self._canvas._vsegs:
            return
        ovx = self._split_panel.overlap_x.value()
        ovy = self._split_panel.overlap_y.value()
        self._canvas.set_overlap(ovx, ovy)
        self._canvas._update_overlap_handles()
        vsegs = self._canvas._vsegs
        hsegs = self._canvas._hsegs
        outer_bounds = self._canvas._outer_bounds
        rows = len(vsegs)
        cols = len(hsegs)
        if rows == 0 or cols == 0:
            return
        n_vlines = len(vsegs[0]) if vsegs[0] else 0
        n_hlines = len(hsegs[0]) if hsegs[0] else 0
        self._compute_regions_from_segs(
            vsegs, hsegs, outer_bounds,
            n_vlines + 1, n_hlines + 1,
            ovx, ovy
        )

    def _open_eraser_dialog(self, region_id: int):
        """サムネイルのダブルクリックで消しゴムダイアログを開く。"""
        if not self._doc:
            return
        region = next((r for r in self._doc.regions if r.id == region_id), None)
        if not region:
            return
        dlg = EraserDialog(region, self._doc.source_pil, self)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            self._push_history()
            region._erased_pil = dlg.get_result()
            region.user_scale = dlg.get_user_scale()
            ox, oy = dlg.get_user_offset()
            region.user_offset_x = ox
            region.user_offset_y = oy
            self._canvas.refresh_regions()
            self._bottom_tray.populate(self._doc.regions, self._doc.source_pil)

    def _on_overlap_changed(self, ovx: int, ovy: int):
        """重複ハンドルドラッグ → スピンボックスをリアルタイム更新"""
        p = self._split_panel
        for sb, val in [(p.overlap_x, ovx), (p.overlap_y, ovy)]:
            sb.blockSignals(True)
            sb.setValue(val)
            sb.blockSignals(False)

    def closeEvent(self, event):
        self._split_panel.save_settings()
        super().closeEvent(event)

    def _init_splitter(self):
        h = self._central_splitter.height()
        if h > 0:
            tray_h = 155   # ヘッダー + サムネイルリスト
            self._central_splitter.setSizes([max(200, h - tray_h), tray_h])

    # ── Transparency / Mask ──

    def _do_transparency(self, color: QColor, tolerance: int, contiguous: bool):
        if not self._doc:
            return
        sel = self._canvas.get_selected_ids()
        targets = [r for r in self._doc.regions if r.id in sel] if sel else self._doc.regions[:1]
        if not targets:
            return
        self._push_history()
        for region in targets:
            cr = region.crop
            cropped = self._doc.source_pil.crop((cr.x(), cr.y(), cr.x() + cr.width(), cr.y() + cr.height()))
            # use center as seed if contiguous
            seed_x = cropped.width // 2
            seed_y = cropped.height // 2
            result = apply_color_transparency(cropped, color, tolerance, contiguous, seed_x, seed_y)
            region._mask_pil = result

        self._canvas.refresh_bg()
        self._update_status()

    # ── Undo / Redo ──

    def _undo(self):
        snap = self._history.undo()
        if snap is not None and self._doc:
            self._doc.regions = snap
            self._canvas.refresh_regions()
            self._bottom_tray.populate(self._doc.regions, self._doc.source_pil)
        self._update_actions()

    def _redo(self):
        snap = self._history.redo()
        if snap is not None and self._doc:
            self._doc.regions = snap
            self._canvas.refresh_regions()
            self._bottom_tray.populate(self._doc.regions, self._doc.source_pil)
        self._update_actions()

    # ── Animation ──

    def _on_anim_frame_selected(self, idx: int):
        pass  # 新AnimPanelではフレームプレビューをダイアログで行う

    def _add_selected_to_anim(self):
        pass  # 新AnimPanelでは「読み込む」ボタンで行う

    def _build_anim_thumbs(self) -> dict:
        return {}

    def _save_apng(self):
        frames = self._anim_panel.get_enabled_frames()
        if not frames:
            # フレームが未設定なら現在タブの全リージョンを自動追加
            if self._doc and self._doc.regions:
                self._anim_panel.set_tabs(self._doc_tabs)
                self._anim_panel._load_from_source()
                frames = self._anim_panel.get_enabled_frames()
            if not frames:
                QMessageBox.information(self, "情報",
                    "フレームがありません。APNGパネルの「読み込む」でフレームを追加してください。")
                return

        frames_pil, durations, offsets = [], [], []
        for f in frames:
            doc = f.doc or self._doc
            if not doc:
                continue
            region = next((r for r in doc.regions if r.id == f.region_id), None)
            if not region:
                continue
            fi = render_region(doc.source_pil, region)
            fsw = getattr(f, 'scale_w', 1.0); fsh = getattr(f, 'scale_h', 1.0)
            if fsw != 1.0 or fsh != 1.0:
                fi = fi.resize((max(1, int(fi.width * fsw)), max(1, int(fi.height * fsh))), Image.LANCZOS)
            frames_pil.append(fi)
            durations.append(f.duration_ms)
            offsets.append((f.offset_x, f.offset_y))

        if not frames_pil:
            QMessageBox.information(self, "情報", "有効なフレームがありません。")
            return

        default_path = str(Path(self._doc.source_path).with_suffix(".png")) if self._doc else "animation.png"
        path, _ = QFileDialog.getSaveFileName(self, "APNGとして保存", default_path, "PNG (*.png)")
        if not path:
            return

        loop = self._anim_panel.get_loop()
        canvas_size = self._anim_panel.get_canvas_size()
        data = make_apng(frames_pil, durations, loop, offsets, canvas_size)
        with open(path, "wb") as f:
            f.write(data)
        QMessageBox.information(self, "保存完了", f"APNGを保存しました:\n{path}")

    # ── Animation menu helpers ──

    def _anim_add_frame_from_tray(self):
        """現在選択されているトレイのリージョンをAPNGフレームとして追加する。"""
        if not self._doc:
            return
        selected_ids = self._canvas.selected_region_ids() if hasattr(self._canvas, 'selected_region_ids') else []
        if not selected_ids:
            # 選択がなければ全リージョンを追加
            regions = [r for r in self._doc.regions if r.enabled]
        else:
            regions = [r for r in self._doc.regions if r.id in selected_ids and r.enabled]
        for r in regions:
            f = AnimFrame(r.id, 100, doc=self._doc)
            self._anim_panel._frames.append(f)
            cr = r.crop
            if r._erased_pil:
                crop = r._erased_pil.copy()
            else:
                crop = self._doc.source_pil.crop(
                    (cr.x(), cr.y(), cr.x() + cr.width(), cr.y() + cr.height()))
            crop.thumbnail((48, 48))
            self._anim_panel._thumbs[id(f)] = QIcon(QPixmap.fromImage(pil_to_qimage(crop)))
        self._anim_panel._refresh_list()
        self._anim_panel._update_time_info()
        if self._apng_preview:
            self._apng_preview.refresh()

    def _anim_load_source(self, mode: str):
        """メニューからフレームを読み込む。mode='current'|'all'"""
        panel = self._anim_panel
        if mode == "current":
            panel._src_combo.setCurrentIndex(0)  # 現在のタブ
        else:
            panel._src_combo.setCurrentIndex(1)  # 全タブ
        panel._load_from_source()
        self._show_anim_panel()

    def _anim_select_all(self, checked: bool):
        panel = self._anim_panel
        state = Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked
        for i in range(panel.frame_list.count()):
            panel.frame_list.item(i).setCheckState(state)

    def _anim_clear_frames(self):
        self._anim_panel._frames.clear()
        self._anim_panel._thumbs.clear()
        self._anim_panel._refresh_list()
        self._anim_panel._update_time_info()

    def _anim_set_fps_dialog(self):
        panel = self._anim_panel
        val, ok = QInputDialog.getDouble(self, "FPS設定", "FPS (フレーム/秒):",
                                          panel._fps_spin.value(), 0.1, 120, 1)
        if ok:
            panel._rb_fps.setChecked(True)
            panel._fps_spin.setValue(val)
            panel._apply_duration()

    def _anim_set_total_dialog(self):
        panel = self._anim_panel
        val, ok = QInputDialog.getDouble(self, "合計時間設定", "合計時間 (秒):",
                                          panel._total_spin.value(), 0.1, 3600, 1)
        if ok:
            panel._rb_total.setChecked(True)
            panel._total_spin.setValue(val)
            panel._apply_duration()

    def _anim_apply_duration(self):
        self._anim_panel._apply_duration()

    def _anim_set_loop(self, loop_val: int):
        panel = self._anim_panel
        if loop_val == 0:
            panel.loop_combo.setCurrentIndex(0)
        else:
            panel.loop_combo.setCurrentIndex(loop_val)  # 1回→index 1, 2回→index 2, ...
        panel._update_time_info()

    def _show_anim_panel(self):
        """APNGタブに切り替える"""
        for i in range(self._right_tabs.count()):
            if self._right_tabs.tabText(i) == "APNG":
                self._right_tabs.setCurrentIndex(i)
                break

    # ── 設定ダイアログ ──

    def _open_line_settings(self):
        dlg = QDialog(self)
        dlg.setWindowTitle("線の色・太さ設定")
        dlg.resize(400, 280)
        layout = QVBoxLayout(dlg)
        form = QFormLayout()

        def _color_btn(key_color, key_alpha):
            c = QColor(LINE_SETTINGS[key_color])
            c.setAlpha(LINE_SETTINGS[key_alpha])
            btn = QPushButton()
            btn.setFixedWidth(80)
            def _update_style():
                cc = QColor(LINE_SETTINGS[key_color])
                cc.setAlpha(LINE_SETTINGS[key_alpha])
                btn.setStyleSheet(f"background:rgba({cc.red()},{cc.green()},{cc.blue()},{cc.alpha()});border:1px solid #888;")
            _update_style()
            def _pick():
                col = QColorDialog.getColor(QColor(LINE_SETTINGS[key_color]), dlg, "色を選択",
                                            QColorDialog.ColorDialogOption.ShowAlphaChannel)
                if col.isValid():
                    LINE_SETTINGS[key_color] = col.name()
                    LINE_SETTINGS[key_alpha] = col.alpha()
                    _update_style()
            btn.clicked.connect(_pick)
            return btn

        def _width_spin(key_width):
            sp = QSpinBox(); sp.setRange(1, 8); sp.setValue(LINE_SETTINGS[key_width])
            sp.valueChanged.connect(lambda v: LINE_SETTINGS.update({key_width: v}))
            return sp

        row1 = QHBoxLayout(); row1.addWidget(_color_btn('split_color', 'split_alpha')); row1.addWidget(_width_spin('split_width')); row1.addWidget(QLabel("px")); row1.addStretch()
        w1 = QWidget(); w1.setLayout(row1)
        row2 = QHBoxLayout(); row2.addWidget(_color_btn('margin_color', 'margin_alpha')); row2.addWidget(_width_spin('margin_width')); row2.addWidget(QLabel("px")); row2.addStretch()
        w2 = QWidget(); w2.setLayout(row2)
        row3 = QHBoxLayout(); row3.addWidget(_color_btn('overlap_color', 'overlap_alpha')); row3.addWidget(_width_spin('overlap_width')); row3.addWidget(QLabel("px")); row3.addStretch()
        w3 = QWidget(); w3.setLayout(row3)

        form.addRow("分割線（色・太さ）:", w1)
        form.addRow("余白線（色・太さ）:", w2)
        form.addRow("重複線（色・太さ）:", w3)
        layout.addLayout(form)
        layout.addStretch()

        btns = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        btns.accepted.connect(dlg.accept)
        btns.rejected.connect(dlg.reject)
        layout.addWidget(btns)

        if dlg.exec() == QDialog.DialogCode.Accepted:
            if self._canvas:
                self._canvas._rebuild_split_line_items()

    # ── ヘルプ ──

    def _open_help(self):
        dlg = QDialog(self)
        dlg.setWindowTitle("操作ガイド / キーボードショートカット")
        dlg.resize(620, 560)
        layout = QVBoxLayout(dlg)
        text = QLabel()
        text.setWordWrap(True)
        text.setTextFormat(Qt.TextFormat.RichText)
        text.setText("""
<h2>Image Craft Studio 操作ガイド</h2>

<h3>■ 分割キャンバス</h3>
<table cellspacing="4">
<tr><td><b>ドラッグ</b></td><td>分割線・余白線を移動</td></tr>
<tr><td><b>Ctrl + ドラッグ（余白線）</b></td><td>列/行バンドごとに余白線を個別移動</td></tr>
<tr><td><b>Alt + ドラッグ（分割線）</b></td><td>セル単位の重複幅を調整（非直線分割）<br>
  ※ 各行バンドの垂直線・各列バンドの水平線は独立移動可能。<br>
  　 これにより直線では切れない場所を階段状に分割できます。</td></tr>
<tr><td><b>Ctrl + スクロール</b></td><td>キャンバス拡大縮小</td></tr>
<tr><td><b>右クリック + ドラッグ</b></td><td>キャンバスパン（移動）</td></tr>
</table>

<h3>■ 消しゴム編集ダイアログ（ダブルクリックで開く）</h3>
<table cellspacing="4">
<tr><td><b>✋ 移動モード</b></td><td>ドラッグで画像を移動</td></tr>
<tr><td><b>✏ 消しゴムモード</b></td><td>ドラッグで塗り消し（ブラシサイズ可変）</td></tr>
<tr><td><b>スポイト</b></td><td>クリックで色をサンプリング</td></tr>
<tr><td><b>矢印キー</b></td><td>1px単位で画像位置を移動</td></tr>
<tr><td><b>スクロール</b></td><td>画像スケール 1%単位で変更</td></tr>
<tr><td><b>Ctrl + スクロール</b></td><td>プレビュー表示を拡大縮小（ズーム）</td></tr>
<tr><td><b>リサイズハンドル</b></td><td>画像枠の8点をドラッグでスケール変更<br>
  角＝斜め方向、辺中央＝縦または横方向<br>
  「縦横比固定」チェック時は比率維持</td></tr>
</table>

<h3>■ APNGフレーム位置・スケール調整（フレームリストでダブルクリック）</h3>
<table cellspacing="4">
<tr><td><b>ドラッグ</b></td><td>フレーム位置を移動</td></tr>
<tr><td><b>矢印キー</b></td><td>1px単位で位置移動</td></tr>
<tr><td><b>リサイズハンドル</b></td><td>フレーム画像をスケール変更</td></tr>
</table>

<h3>■ 出力ファイル名</h3>
<ul>
<li>「連番のみ」チェック時：分割グリッド内の元位置番号でファイル名を付与<br>
  例：01・05・07 のみチェック → ファイル名も 01, 05, 07 になる</li>
<li>通常モード：基本名 + 連番（開始番号から）</li>
</ul>

<h3>■ 線の色・太さ変更</h3>
<p>メニュー「設定」→「線の色・太さ設定」から分割線・余白線・重複線の色と太さを変更できます。</p>
""")
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(text)
        layout.addWidget(scroll)
        btns = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok)
        btns.accepted.connect(dlg.accept)
        layout.addWidget(btns)
        dlg.exec()

    def _open_about(self):
        QMessageBox.information(self, "バージョン情報",
            f"Image Craft Studio\nVersion {APP_VERSION}\n\n"
            "AI生成画像の仕上げ・分割・規格化・APNG制作ツール\n"
            "Python + PyQt6 + Pillow")

    def _on_right_tab_changed(self, idx: int):
        """右パネルのタブ切り替えでメインエリアを切り替える。"""
        tab_text = self._right_tabs.tabText(idx)
        if tab_text == "APNG" and self._apng_preview is not None:
            self._apng_preview.refresh()
            self._central_stack.setCurrentIndex(1)
            self._update_left_toolbar_for_apng(True)
        else:
            self._central_stack.setCurrentIndex(0)
            self._update_left_toolbar_for_apng(False)

    def _update_left_toolbar_for_apng(self, apng_mode: bool):
        """左ツールバーのAPNG専用ボタンの表示/非表示を切り替える。"""
        for act in self._apng_tool_actions:
            act.setVisible(apng_mode)
        for act in self._tool_actions.values():
            act.setVisible(not apng_mode)

    def _anim_tick(self):
        if not self._doc or not self._doc.anim_frames:
            return
        frame = self._doc.anim_frames[self._anim_frame_idx % len(self._doc.anim_frames)]
        self._anim_frame_idx = (self._anim_frame_idx + 1) % len(self._doc.anim_frames)
        region = next((r for r in self._doc.regions if r.id == frame.region_id), None)
        if region:
            rendered = render_region(self._doc.source_pil, region)
            self._anim_panel.set_preview(rendered)
        self._anim_timer.setInterval(frame.duration_ms)

    # ── Selection sync ──

    def _on_region_selected(self, ids: list[int]):
        lw = self._bottom_tray.list_widget
        lw.blockSignals(True)
        lw.clearSelection()
        for i in range(lw.count()):
            item = lw.item(i)
            if item.data(Qt.ItemDataRole.UserRole) in ids:
                item.setSelected(True)
        lw.blockSignals(False)
        self._status_sel.setText(f"選択数: {len(ids)}")

    def _on_tray_selected(self, ids: list[int]):
        self._canvas.set_selected_regions(ids)
        self._status_sel.setText(f"選択数: {len(ids)}")

    # ── Status ──

    def _update_status(self):
        if not self._doc:
            return
        img = self._doc.source_pil
        self._status_size.setText(f"ソース: {img.width}×{img.height} px")
        out = self._split_panel.get_output_settings()
        mt = out.get('margin_top', 0); mb = out.get('margin_bottom', 0)
        ml = out.get('margin_left', 0); mr = out.get('margin_right', 0)
        self._status_out.setText(f"出力: {out['w']}×{out['h']} px (余白: {mt}/{mb}/{ml}/{mr})")
        self._status_fmt.setText(f"形式: {out['fmt']}")
        n_sel = len(self._canvas.get_selected_ids())
        self._status_sel.setText(f"選択数: {n_sel} / {len(self._doc.regions)}")
        src = Path(self._doc.source_path)
        self._status_dest.setText(f"保存先: 元画像と同じ場所  ファイル名: {src.stem}_part")


# ──────────────────────────────────────────────
# Auto-split detection
# ──────────────────────────────────────────────

def _find_gap_centers(is_gap: 'np.ndarray', n_splits: int, total: int) -> list[int]:
    """
    bool 配列 is_gap から「ギャップ帯」の中心座標を n_splits 本返す。
    画像端に接するギャップ帯（外周余白）は候補から除外。
    候補過多 → 等分理想位置に最近傍で選択。
    候補不足 → 等分フォールバック。
    """
    all_gaps: list[tuple[int, int]] = []
    i = 0
    while i < total:
        if is_gap[i]:
            j = i
            while j < total and is_gap[j]:
                j += 1
            all_gaps.append((i, j))
            i = j
        else:
            i += 1

    # 端に接するギャップ（画像外周余白）を除外して内側のみ残す
    interior = [(s, e) for s, e in all_gaps if s > 0 and e < total]
    gaps = interior if interior else all_gaps  # 全部端なら仕方なく全部使う

    centers = [(s + e) // 2 for s, e in gaps]

    if len(centers) == n_splits:
        return centers
    if len(centers) < n_splits:
        step = total // (n_splits + 1)
        return [step * (k + 1) for k in range(n_splits)]

    # 等分理想位置に最近傍の n_splits 本を選ぶ
    ideal = [(k + 1) * total / (n_splits + 1) for k in range(n_splits)]
    chosen: list[int] = []
    used: set[int] = set()
    for target in ideal:
        best = min(
            (idx for idx in range(len(centers)) if idx not in used),
            key=lambda idx: abs(centers[idx] - target),
            default=None,
        )
        if best is not None:
            chosen.append(centers[best])
            used.add(best)
    return sorted(chosen)


def auto_detect_splits(pil_img: 'Image.Image', n_cols: int, n_rows: int,
                       bg_color: tuple = None, tolerance: int = 20
                       ) -> tuple[list[list[int]], list[list[int]]]:
    """
    透過 PNG: alpha が全行/全列でゼロの列・行をギャップ帯として検出。
    不透明画像: 背景色が全行/全列で一致する列・行をギャップ帯として検出。

    ギャップ帯の中心でセル境界を確定 → 各セル内コンテンツの bbox を個別検出
    → 隣接 bbox 間の中点を最終分割線にする（行ごと・列ごとに独立）。

    Returns
    -------
    vsegs[row][col_line]  : 垂直分割線 X 座標（行ごとに独立）
    hsegs[col][row_line]  : 水平分割線 Y 座標（列ごとに独立）
    """
    import numpy as np
    rgba = pil_img.convert("RGBA")
    arr = np.array(rgba)
    H, W = arr.shape[:2]
    alpha = arr[:, :, 3]

    has_alpha = (alpha < 32).mean() > 0.02

    if has_alpha:
        # 透過画像：列全体・行全体で alpha の最大値が低い = ギャップ
        # （交点まで透過という条件 ≒ その列/行に不透明ピクセルが全く無い）
        col_max_a = alpha.max(axis=0)          # (W,)  各列の最大 alpha
        row_max_a = alpha.max(axis=1)          # (H,)  各行の最大 alpha
        # 完全透過でなくてもスタンプ間の余白は薄い → tolerance で調整
        alpha_thr = min(32, max(1, tolerance))
        col_is_gap = col_max_a < alpha_thr     # (W,) bool
        row_is_gap = row_max_a < alpha_thr     # (H,) bool
        content = alpha >= alpha_thr
    else:
        if bg_color is None:
            corners = [arr[0, 0, :3], arr[0, W-1, :3],
                       arr[H-1, 0, :3], arr[H-1, W-1, :3]]
            bg_color = tuple(np.array(corners).mean(axis=0).astype(int).tolist())
        diff = np.abs(arr[:, :, :3].astype(np.int32)
                      - np.array(bg_color[:3], dtype=np.int32))
        is_bg = diff.max(axis=2) <= tolerance
        col_is_gap = is_bg.all(axis=0)         # 列全体が背景色
        row_is_gap = is_bg.all(axis=1)
        content = ~is_bg

    # ギャップ帯の中心座標（セル境界）を取得
    vc = _find_gap_centers(col_is_gap, n_cols - 1, W)  # x 境界
    hc = _find_gap_centers(row_is_gap, n_rows - 1, H)  # y 境界

    x_bounds = [0] + vc + [W]    # n_cols + 1 個
    y_bounds = [0] + hc + [H]    # n_rows + 1 個

    # 各セルのコンテンツ bbox を検出
    bboxes = [[None] * n_cols for _ in range(n_rows)]
    for r in range(n_rows):
        for c in range(n_cols):
            cy1, cy2 = y_bounds[r], y_bounds[r + 1]
            cx1, cx2 = x_bounds[c], x_bounds[c + 1]
            ys, xs = np.where(content[cy1:cy2, cx1:cx2])
            if len(xs):
                bboxes[r][c] = (cx1 + int(xs.min()), cy1 + int(ys.min()),
                                cx1 + int(xs.max()), cy1 + int(ys.max()))

    # 垂直分割線：各行で隣接セル bbox 間の中点
    vsegs: list[list[int]] = []
    for r in range(n_rows):
        row_v: list[int] = []
        for c in range(n_cols - 1):
            lb, rb = bboxes[r][c], bboxes[r][c + 1]
            if lb and rb:
                x = (lb[2] + rb[0]) // 2
            elif lb:
                x = lb[2]
            elif rb:
                x = rb[0]
            else:
                x = vc[c] if c < len(vc) else x_bounds[c + 1]
            # セル境界でクランプ
            row_v.append(max(x_bounds[c], min(x_bounds[c + 1], x)))
        vsegs.append(row_v)

    # 水平分割線：各列で隣接セル bbox 間の中点
    hsegs: list[list[int]] = []
    for c in range(n_cols):
        col_h: list[int] = []
        for r in range(n_rows - 1):
            tb, bb = bboxes[r][c], bboxes[r + 1][c]
            if tb and bb:
                y = (tb[3] + bb[1]) // 2
            elif tb:
                y = tb[3]
            elif bb:
                y = bb[1]
            else:
                y = hc[r] if r < len(hc) else y_bounds[r + 1]
            col_h.append(max(y_bounds[r], min(y_bounds[r + 1], y)))
        hsegs.append(col_h)

    return vsegs, hsegs


# ──────────────────────────────────────────────
# Batch worker
# ──────────────────────────────────────────────

class BatchWorker(QThread):
    progress = pyqtSignal(int, int, str)   # done, total, current_file
    finished = pyqtSignal(int, list)       # ok_count, error_list

    def __init__(self, file_paths: list[str], split_params: dict,
                 out_settings: dict, out_dir: str, use_auto: bool,
                 bg_tolerance: int, fmt: str, quality: int):
        super().__init__()
        self.file_paths = file_paths
        self.split_params = split_params      # cols, rows, ovx, ovy, gx, gy, ot, ob, ol, or_
        self.out_settings = out_settings
        self.out_dir = out_dir
        self.use_auto = use_auto
        self.bg_tolerance = bg_tolerance
        self.fmt = fmt
        self.quality = quality
        self._errors: list[str] = []

    def run(self):
        total = len(self.file_paths)
        ok = 0
        sp = self.split_params
        cols, rows = sp["cols"], sp["rows"]
        ovx, ovy   = sp["ovx"], sp["ovy"]
        gx,  gy    = sp["gx"], sp["gy"]
        ot, ob, ol, or_ = sp["ot"], sp["ob"], sp["ol"], sp["or_"]

        for i, fpath in enumerate(self.file_paths):
            fname = Path(fpath).stem
            self.progress.emit(i, total, fname)
            try:
                img = Image.open(fpath).convert("RGBA")
                sw, sh = img.size

                eff_x  = ol
                eff_y  = ot
                eff_x2 = sw - or_
                eff_y2 = sh - ob
                eff_w  = eff_x2 - eff_x
                eff_h  = eff_y2 - eff_y
                if eff_w <= 0 or eff_h <= 0:
                    raise ValueError("外周余白が画像サイズを超えています")

                if self.use_auto:
                    region_img = img.crop((eff_x, eff_y, eff_x2, eff_y2))
                    raw_vsegs, raw_hsegs = auto_detect_splits(
                        region_img, cols, rows, tolerance=self.bg_tolerance
                    )
                    vsegs = [[eff_x + x for x in r] for r in raw_vsegs]
                    hsegs = [[eff_y + y for y in c] for c in raw_hsegs]
                else:
                    cell_w = (eff_w - gx * (cols - 1)) / cols
                    cell_h = (eff_h - gy * (rows - 1)) / rows
                    flat_v = [int(eff_x + (k + 1) * cell_w + k * gx) for k in range(cols - 1)]
                    flat_h = [int(eff_y + (k + 1) * cell_h + k * gy) for k in range(rows - 1)]
                    vsegs = [flat_v for _ in range(rows)]
                    hsegs = [flat_h for _ in range(cols)]

                # 各セルを切り出して保存
                out = self.out_settings
                cell_no = 1
                for row in range(rows):
                    row_v = vsegs[row] if row < len(vsegs) else vsegs[0] if vsegs else []
                    for col in range(cols):
                        col_h = hsegs[col] if col < len(hsegs) else hsegs[0] if hsegs else []
                        x1 = eff_x if col == 0 else row_v[col - 1]
                        x2 = eff_x2 if col == cols - 1 else row_v[col]
                        y1 = eff_y if row == 0 else col_h[row - 1]
                        y2 = eff_y2 if row == rows - 1 else col_h[row]
                        cx = max(0, x1 - ovx)
                        cy = max(0, y1 - ovy)
                        cw = min((x2 - x1) + ovx * 2, sw - cx)
                        ch = min((y2 - y1) + ovy * 2, sh - cy)
                        if cw <= 0 or ch <= 0:
                            cell_no += 1
                            continue

                        r = Region(cell_no, cx, cy, cw, ch)
                        r.canvas_w = out["w"]; r.canvas_h = out["h"]
                        r.margin_top    = out.get("margin_top", 24)
                        r.margin_bottom = out.get("margin_bottom", 24)
                        r.margin_left   = out.get("margin_left", 24)
                        r.margin_right  = out.get("margin_right", 24)
                        r.fit_mode = out["fit_mode"]
                        r.anchor   = out.get("anchor", "center")
                        r.bg_color = out["bg"]

                        rendered = render_region(img, r)
                        ext = "jpg" if "JPEG" in self.fmt else "png"
                        num = f"{cell_no:02d}"
                        out_name = f"{fname}_{num}.{ext}"
                        out_path = os.path.join(self.out_dir, out_name)
                        if "JPEG" in self.fmt:
                            bg = Image.new("RGB", rendered.size, (255, 255, 255))
                            bg.paste(rendered, mask=rendered.split()[3] if rendered.mode == "RGBA" else None)
                            bg.save(out_path, "JPEG", quality=self.quality)
                        else:
                            rendered.save(out_path, "PNG")
                        cell_no += 1
                ok += 1
            except Exception as e:
                self._errors.append(f"{fname}: {e}")

        self.progress.emit(total, total, "完了")
        self.finished.emit(ok, self._errors)


# ──────────────────────────────────────────────
# Batch dialog
# ──────────────────────────────────────────────

class BatchDialog(QDialog):
    def __init__(self, split_params: dict, out_settings: dict, parent=None):
        super().__init__(parent)
        self.setWindowTitle("バッチ処理")
        self.resize(560, 480)
        self._split_params = split_params
        self._out_settings = out_settings
        self._worker = None
        self._setup_ui()

    def _setup_ui(self):
        layout = QVBoxLayout(self)

        # ── ファイルリスト ──
        file_grp = QGroupBox("処理する画像ファイル")
        file_lay = QVBoxLayout(file_grp)
        btn_row = QHBoxLayout()
        self._add_btn = QPushButton("ファイルを追加")
        self._clear_btn = QPushButton("リストをクリア")
        btn_row.addWidget(self._add_btn)
        btn_row.addWidget(self._clear_btn)
        btn_row.addStretch()
        file_lay.addLayout(btn_row)
        self._file_list = QListWidget()
        self._file_list.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self._file_list.setFixedHeight(150)
        file_lay.addWidget(self._file_list)
        layout.addWidget(file_grp)

        # ── 出力先 ──
        out_grp = QGroupBox("出力先")
        out_lay = QHBoxLayout(out_grp)
        self._out_dir_edit = QLineEdit()
        self._out_dir_btn = QPushButton("参照")
        out_lay.addWidget(self._out_dir_edit, 1)
        out_lay.addWidget(self._out_dir_btn)
        layout.addWidget(out_grp)

        # ── 分割オプション ──
        opt_grp = QGroupBox("分割オプション")
        opt_form = QFormLayout(opt_grp)
        self._auto_chk = QCheckBox("自動分割（余白を検出）")
        opt_form.addRow(self._auto_chk)
        self._tolerance_spin = QSpinBox()
        self._tolerance_spin.setRange(1, 128)
        self._tolerance_spin.setValue(20)
        opt_form.addRow("背景色許容誤差", self._tolerance_spin)
        sp = self._split_params
        info = (f"グリッド: {sp['cols']}列 × {sp['rows']}行　"
                f"重複: X={sp['ovx']} Y={sp['ovy']}　"
                f"ギャップ: X={sp['gx']} Y={sp['gy']}")
        opt_form.addRow("現在の設定:", QLabel(info))

        self._fmt_combo = QComboBox()
        self._fmt_combo.addItems(["PNG (透過)", "JPEG"])
        opt_form.addRow("出力形式", self._fmt_combo)
        self._quality_spin = QSpinBox()
        self._quality_spin.setRange(1, 100)
        self._quality_spin.setValue(90)
        opt_form.addRow("JPEG品質", self._quality_spin)
        layout.addWidget(opt_grp)

        # ── 進捗バー ──
        self._progress = QProgressDialog("", "キャンセル", 0, 100, self)
        self._progress.setWindowModality(Qt.WindowModality.WindowModal)
        self._progress.setAutoClose(False)
        self._progress.reset()

        # ── ボタン ──
        btn_row2 = QHBoxLayout()
        self._run_btn = QPushButton("▶ 実行")
        self._run_btn.setStyleSheet("font-weight:bold; padding:6px; background:#2F6FEB; color:white;")
        self._close_btn = QPushButton("閉じる")
        btn_row2.addWidget(self._run_btn)
        btn_row2.addWidget(self._close_btn)
        layout.addLayout(btn_row2)

        # シグナル接続
        self._add_btn.clicked.connect(self._add_files)
        self._clear_btn.clicked.connect(self._file_list.clear)
        self._out_dir_btn.clicked.connect(self._browse_out_dir)
        self._run_btn.clicked.connect(self._run)
        self._close_btn.clicked.connect(self.reject)
        self._auto_chk.toggled.connect(lambda on: self._tolerance_spin.setEnabled(on))
        self._tolerance_spin.setEnabled(False)

    def _add_files(self):
        paths, _ = QFileDialog.getOpenFileNames(
            self, "画像を追加", "",
            "画像ファイル (*.png *.jpg *.jpeg *.webp *.bmp *.tiff *.tif)"
        )
        for p in paths:
            if not any(self._file_list.item(i).text() == p
                       for i in range(self._file_list.count())):
                self._file_list.addItem(p)

    def _browse_out_dir(self):
        d = QFileDialog.getExistingDirectory(self, "出力フォルダを選択")
        if d:
            self._out_dir_edit.setText(d)

    def _run(self):
        files = [self._file_list.item(i).text() for i in range(self._file_list.count())]
        if not files:
            QMessageBox.warning(self, "警告", "ファイルを追加してください。")
            return
        out_dir = self._out_dir_edit.text().strip()
        if not out_dir:
            QMessageBox.warning(self, "警告", "出力先フォルダを指定してください。")
            return
        os.makedirs(out_dir, exist_ok=True)

        self._run_btn.setEnabled(False)
        prog = self._progress
        prog.setMaximum(len(files))
        prog.setValue(0)
        prog.setLabelText("処理中...")
        prog.show()

        self._worker = BatchWorker(
            files, self._split_params, self._out_settings,
            out_dir,
            self._auto_chk.isChecked(),
            self._tolerance_spin.value(),
            self._fmt_combo.currentText(),
            self._quality_spin.value()
        )
        self._worker.progress.connect(lambda done, total, name: (
            prog.setValue(done),
            prog.setLabelText(f"処理中: {name}")
        ))
        self._worker.finished.connect(self._on_finished)
        prog.canceled.connect(self._worker.terminate)
        self._worker.start()

    def _on_finished(self, ok: int, errors: list):
        self._progress.hide()
        self._run_btn.setEnabled(True)
        msg = f"{ok}ファイルを処理しました。"
        if errors:
            msg += f"\nエラー {len(errors)}件:\n" + "\n".join(errors[:5])
        QMessageBox.information(self, "完了", msg)


def _sep():
    sep = QFrame()
    sep.setFrameShape(QFrame.Shape.VLine)
    sep.setFrameShadow(QFrame.Shadow.Sunken)
    return sep


# ──────────────────────────────────────────────
# Entry point
# ──────────────────────────────────────────────

def main():
    import traceback
    _base = Path(sys.executable).parent if getattr(sys, "frozen", False) else Path(__file__).parent
    _log = open(_base / "crash_log.txt", "w", buffering=1)
    def _excepthook(tp, val, tb):
        traceback.print_exception(tp, val, tb, file=_log)
        _log.flush()
        traceback.print_exception(tp, val, tb)
    sys.excepthook = _excepthook

    app = QApplication(sys.argv)
    app.setApplicationName("Image Craft Studio")
    app.setOrganizationName("ICS")

    # Dark style
    app.setStyle("Fusion")
    palette = app.palette()
    from PyQt6.QtGui import QPalette
    dark = QColor(30, 30, 30)
    mid = QColor(45, 45, 48)
    light = QColor(60, 63, 65)
    text = QColor(220, 220, 220)
    highlight = QColor(0x2F, 0x6F, 0xEB)
    palette.setColor(QPalette.ColorRole.Window, mid)
    palette.setColor(QPalette.ColorRole.WindowText, text)
    palette.setColor(QPalette.ColorRole.Base, dark)
    palette.setColor(QPalette.ColorRole.AlternateBase, mid)
    palette.setColor(QPalette.ColorRole.Text, text)
    palette.setColor(QPalette.ColorRole.Button, light)
    palette.setColor(QPalette.ColorRole.ButtonText, text)
    palette.setColor(QPalette.ColorRole.Highlight, highlight)
    palette.setColor(QPalette.ColorRole.HighlightedText, QColor(255, 255, 255))
    app.setPalette(palette)

    win = MainWindow()
    win.show()

    # Open files from command line
    if len(sys.argv) > 1:
        win.open_files(sys.argv[1:])

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
