"""Spreadsheet-like auto filter for a QTableWidget: a filter button in every column header opens a list of the
column's values with check boxes, a search field and sorting. Clicking the header text sorts as usual.

Cells may carry FILTER_ROLE (value shown in the filter list, e.g. the day of a date) and SORT_ROLE (sort key,
see SortItem); without them the cell text is used."""
from PySide6.QtCore import QPoint, QRect, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import (QDialogButtonBox, QFrame, QHeaderView, QLineEdit, QListWidget, QListWidgetItem,
                               QPushButton, QStyle, QStyleOptionHeader, QTableWidgetItem, QVBoxLayout)

from .i18n import tr

SORT_ROLE = Qt.UserRole + 1
SEARCH_ROLE = Qt.UserRole + 2             # extra text a search should find
FILTER_ROLE = Qt.UserRole + 3
ACTIVE = '#3a7bd5'


class SortItem(QTableWidgetItem):
    """Table cell that sorts by a key (SORT_ROLE: time, number) instead of its text; empty cells first."""

    def __lt__(self, other):
        a, b = self.data(SORT_ROLE), other.data(SORT_ROLE)
        if a is None or b is None:
            return a is None and b is not None
        return a < b


def filter_value(item):
    if item is None:
        return ''
    v = item.data(FILTER_ROLE)
    return item.text() if v is None else v


class FilterHeader(QHeaderView):
    """Horizontal header with a filter button at the right edge of every section."""
    filter_clicked = Signal(int)
    BUTTON = 14

    def __init__(self, parent=None):
        super().__init__(Qt.Horizontal, parent)
        self.active = set()                   # columns with a filter
        self.setSectionsClickable(True)
        self.setHighlightSections(False)

    def button_rect(self, logical):
        x = self.sectionViewportPosition(logical) + self.sectionSize(logical) - self.BUTTON - 3
        return QRect(x, (self.height() - self.BUTTON) // 2, self.BUTTON, self.BUTTON)

    def sectionSizeFromContents(self, logical):
        s = super().sectionSizeFromContents(logical)
        s.setWidth(s.width() + self.BUTTON + 4)
        return s

    def paintSection(self, painter, rect, logical):
        # text and sort arrow left of the button: draw the section without the button strip, then the strip
        strip = QRect(rect.right() - self.BUTTON - 5, rect.top(), self.BUTTON + 6, rect.height())
        painter.save()
        super().paintSection(painter, rect.adjusted(0, 0, -strip.width(), 0), logical)
        painter.restore()
        opt = QStyleOptionHeader()
        self.initStyleOption(opt)
        opt.rect = strip
        opt.section = logical
        painter.save()
        self.style().drawControl(QStyle.CE_HeaderSection, opt, painter, self)
        painter.restore()
        r = QRectF(self.button_rect(logical)).adjusted(2, 3, -2, -3)
        on = logical in self.active
        color = QColor(ACTIVE) if on else self.palette().text().color()
        if not on:
            color.setAlpha(110)
        path = QPainterPath()                 # funnel
        path.moveTo(r.left(), r.top())
        path.lineTo(r.right(), r.top())
        path.lineTo(r.center().x() + 1.5, r.center().y() + 1)
        path.lineTo(r.center().x() + 1.5, r.bottom())
        path.lineTo(r.center().x() - 1.5, r.bottom() - 1.5)
        path.lineTo(r.center().x() - 1.5, r.center().y() + 1)
        path.closeSubpath()
        painter.save()
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setPen(QPen(color, 1))
        painter.setBrush(color if on else Qt.NoBrush)
        painter.drawPath(path)
        painter.restore()

    def mousePressEvent(self, e):
        logical = self.logicalIndexAt(e.position().toPoint())
        if logical >= 0 and self.button_rect(logical).adjusted(-2, -4, 2, 4).contains(e.position().toPoint()):
            self.filter_clicked.emit(logical)
            return
        super().mousePressEvent(e)

    desc_first = frozenset()                  # columns sorted descending on the first click (counts, dates)

    def mouseReleaseEvent(self, e):
        logical = self.logicalIndexAt(e.position().toPoint())
        new = logical != self.sortIndicatorSection()
        super().mouseReleaseEvent(e)
        if new and logical in self.desc_first and self.sortIndicatorSection() == logical:
            self.setSortIndicator(logical, Qt.DescendingOrder)


class FilterPopup(QFrame):
    """Sort buttons, search field and the check list of one column's values."""

    def __init__(self, table, col, values, allowed, apply):
        super().__init__(table, Qt.Popup)
        self.setFrameShape(QFrame.StyledPanel)
        self.apply = apply
        lay = QVBoxLayout(self)
        for text, order in ((tr('Sort ascending'), Qt.AscendingOrder), (tr('Sort descending'), Qt.DescendingOrder)):
            b = QPushButton(text)
            b.setFlat(True)
            b.setStyleSheet('text-align: left; padding: 3px 6px;')
            b.clicked.connect(lambda _=False, o=order: (table.sortByColumn(col, o), self.close()))
            lay.addWidget(b)
        self.search = QLineEdit()
        self.search.setPlaceholderText(tr('Search …'))
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(self._search)
        lay.addWidget(self.search)
        self.list = QListWidget()
        self.all = QListWidgetItem(tr('(Select all)'))
        self.all.setFlags(self.all.flags() | Qt.ItemIsUserCheckable)
        self.list.addItem(self.all)
        for v in values:
            it = QListWidgetItem(v if v != '' else tr('(empty)'))
            it.setData(Qt.UserRole, v)
            it.setFlags(it.flags() | Qt.ItemIsUserCheckable)
            it.setCheckState(Qt.Checked if allowed is None or v in allowed else Qt.Unchecked)
            self.list.addItem(it)
        self._sync_all()
        self.list.itemChanged.connect(self._changed)
        lay.addWidget(self.list)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.accepted.connect(self._ok)
        bb.rejected.connect(self.close)
        lay.addWidget(bb)
        self.resize(260, 380)

    def _items(self, visible_only=False):
        return [self.list.item(i) for i in range(1, self.list.count())
                if not (visible_only and self.list.item(i).isHidden())]

    def _sync_all(self):
        states = {it.checkState() for it in self._items(True)}
        self.list.blockSignals(True)
        self.all.setCheckState(Qt.Checked if states == {Qt.Checked} else
                               Qt.Unchecked if states <= {Qt.Unchecked} else Qt.PartiallyChecked)
        self.list.blockSignals(False)

    def _changed(self, item):
        self.list.blockSignals(True)
        if item is self.all:                  # (select all) applies to the values the search shows
            state = Qt.Unchecked if item.checkState() == Qt.Unchecked else Qt.Checked
            for it in self._items(True):
                it.setCheckState(state)
        self.list.blockSignals(False)
        self._sync_all()

    def _search(self, text):
        words = text.lower().split()
        for it in self._items():
            it.setHidden(not all(w in it.text().lower() for w in words))
        self._sync_all()

    def _ok(self):
        items = self._items()
        if self.search.text().strip():        # like a spreadsheet: searching keeps only the values found
            checked = {it.data(Qt.UserRole) for it in items if not it.isHidden() and it.checkState() == Qt.Checked}
        else:
            checked = {it.data(Qt.UserRole) for it in items if it.checkState() == Qt.Checked}
        self.apply(None if len(checked) == len(items) else checked)
        self.close()


class AutoFilter:
    """Adds sorting and per-column filters to a QTableWidget. Call apply() after the table was refilled."""

    def __init__(self, table):
        self.table = table
        self.filters = {}                     # column -> set of allowed filter values
        self.extra = None                     # optional row -> bool, e.g. a search field
        self.header = FilterHeader(table)
        table.setHorizontalHeader(self.header)
        self.header.setSectionResizeMode(QHeaderView.ResizeToContents)
        self.header.setStretchLastSection(True)
        self.header.filter_clicked.connect(self.open)
        table.setSortingEnabled(True)

    def open(self, col):
        t = self.table
        values = sorted({filter_value(t.item(r, col)) for r in range(t.rowCount())}, key=self._order(col))
        popup = FilterPopup(t, col, values, self.filters.get(col), lambda allowed: self.set(col, allowed))
        r = self.header.button_rect(col)
        pos = self.header.mapToGlobal(QPoint(r.right() - popup.width(), self.header.height()))
        popup.move(max(pos.x(), self.header.mapToGlobal(QPoint(0, 0)).x()), pos.y())
        popup.show()
        popup.search.setFocus()

    def _order(self, col):
        """Values in the list in the column's sort order (by the sort key of the first cell having that value)."""
        keys = {}
        t = self.table
        for r in range(t.rowCount()):
            it = t.item(r, col)
            v = filter_value(it)
            if v not in keys and it is not None:
                keys[v] = it.data(SORT_ROLE)
        return lambda v: (keys.get(v) is None, keys.get(v) if keys.get(v) is not None else v)

    def set(self, col, allowed):
        if allowed is None:
            self.filters.pop(col, None)
        else:
            self.filters[col] = allowed
        self.apply()

    def clear(self):
        self.filters.clear()
        self.apply()

    def apply(self):
        t = self.table
        self.header.active = set(self.filters)
        self.header.viewport().update()
        for r in range(t.rowCount()):
            t.setRowHidden(r, any(filter_value(t.item(r, c)) not in allowed for c, allowed in self.filters.items())
                           or (self.extra is not None and not self.extra(r)))
