"""The dark theme."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import QApplication

ACCENT = "#e5484d"
CHECK = (Path(__file__).parent / "check.svg").as_posix()  # the tick in a ticked checkbox

STYLESHEET = f"""
QWidget {{ font-size: 10pt; }}
QLabel#sectionTitle {{ font-size: 12pt; font-weight: 600; }}
QLabel#muted {{ color: #9a9aa3; }}
QLabel#error {{ color: #ff8a8a; }}
QListWidget {{ background: #1f1f24; border: 1px solid #2c2c33; border-radius: 10px; padding: 6px; }}
QListWidget::item {{ padding: 8px; border-radius: 8px; margin: 2px; }}
QListWidget::item:selected {{ background: #3a2224; border: 1px solid {ACCENT}; }}
QListWidget::item:hover:!selected {{ background: #26262c; }}
QLineEdit, QComboBox {{ background: #16161a; border: 1px solid #33333b; border-radius: 6px; padding: 6px 8px; }}
QLineEdit:focus, QComboBox:focus {{ border-color: {ACCENT}; }}
QComboBox QLineEdit {{ border: none; padding: 0; }}
QPushButton {{ background: #2a2a31; border: 1px solid #3a3a43; border-radius: 6px; padding: 6px 14px; }}
QPushButton:hover {{ background: #33333b; }}
QPushButton:disabled {{ color: #66666e; background: #222227; }}
QPushButton#primary {{ background: {ACCENT}; border: none; color: white; font-weight: 600; padding: 9px 22px; }}
QPushButton#primary:hover {{ background: #ef5f63; }}
QPushButton#primary:disabled {{ background: #5a2a2c; color: #b9a0a1; }}
QCheckBox::indicator {{ width: 14px; height: 14px; border: 1px solid #6e6e78; border-radius: 4px; background: #16161a; }}
QCheckBox::indicator:hover {{ border-color: #9a9aa3; }}
QCheckBox::indicator:checked {{ background: {ACCENT}; border-color: {ACCENT}; image: url("{CHECK}"); }}
QPushButton#link {{ background: transparent; border: none; padding: 2px 4px; color: #9a9aa3; }}
QPushButton#link:hover {{ color: #e8e8ec; }}
QFrame#updateBanner {{ background: #2e1c1e; border: 1px solid #5a2a2c; border-radius: 8px; }}
QFrame#updateBanner QPushButton#link {{ color: #f2c4c6; }}
QFrame#updateBanner QPushButton#link:hover {{ color: #ffffff; }}
QProgressBar {{ background: #16161a; border: 1px solid #33333b; border-radius: 6px; height: 14px; text-align: center; }}
QProgressBar::chunk {{ background: {ACCENT}; border-radius: 5px; }}
QPlainTextEdit {{ background: #121215; border: 1px solid #2c2c33; border-radius: 8px; font-family: Consolas, Menlo, monospace; font-size: 9pt; }}
"""


def apply_dark_theme(app: QApplication) -> None:
    app.setStyle("Fusion")
    palette = QPalette()
    for role, color in {
        QPalette.Window: "#17171b",
        QPalette.WindowText: "#e8e8ec",
        QPalette.Base: "#16161a",
        QPalette.AlternateBase: "#1f1f24",
        QPalette.Text: "#e8e8ec",
        QPalette.Button: "#2a2a31",
        QPalette.ButtonText: "#e8e8ec",
        QPalette.Highlight: ACCENT,
        QPalette.HighlightedText: "#ffffff",
        QPalette.ToolTipBase: "#2a2a31",
        QPalette.ToolTipText: "#e8e8ec",
        QPalette.PlaceholderText: "#6e6e78",
    }.items():
        palette.setColor(role, QColor(color))
    app.setPalette(palette)
    app.setStyleSheet(STYLESHEET)
