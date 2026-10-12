"""Session report: one HTML file (charts as embedded SVG) and optionally a PDF of one session - rating, battery,
charger, main data and software versions, the chart of the whole run, the details with their explanations (the
hover texts of the app) and a chart for each charge / discharge phase.

The values and texts come from the app's own widgets (ResultPanel, CurvePlot), built unseen in the report's language,
so the report always says what the app shows."""
import html
import os
import platform
import re
from datetime import datetime

from PySide6.QtCore import QBuffer, QIODevice, QMarginsF, QRect, QRectF, QSize, Qt
from PySide6.QtGui import (QAbstractTextDocumentLayout, QColor, QFont, QImage, QPageLayout, QPageSize, QPainter,
                           QPalette, QPdfWriter, QTextDocument)
from PySide6.QtSvg import QSvgGenerator
from PySide6.QtWidgets import QApplication, QFormLayout

from . import i18n
from .i18n import language, tr, tr_data
from .version import SOURCE, full_version

WATERMARK = 'fashberg/battbench'
CHART_W, CHART_H = 1100, 520
PDF_SCALE = 2                         # charts in the PDF: pixels per chart pixel

CSS = """
body { font-family: 'Segoe UI', Arial, sans-serif; color: #1d1d1d; margin: 24px auto; max-width: 1140px; }
h1 { font-size: 22px; margin: 0 0 4px; }
h2 { font-size: 17px; margin: 26px 0 8px; border-bottom: 1px solid #ccc; padding-bottom: 3px; }
h3 { font-size: 14px; margin: 14px 0 4px; }
.sub { color: #666; margin: 0 0 14px; }
table.kv, table.list { border-collapse: collapse; }
table.kv td { padding: 3px 14px 3px 0; vertical-align: top; }
table.kv td.k { color: #555; white-space: nowrap; }
table.list th, table.list td { border: 1px solid #ccc; padding: 3px 10px; text-align: left; }
table.list th { background: #f0f0f0; }
table.grade { width: 100%; border-collapse: collapse; }
table.grade td { color: white; font-size: 14pt; font-weight: bold; padding: 8px 14px; border-radius: 6px; }
.note { font-weight: bold; margin: 8px 0; }
.tip { color: #333; margin: 0 0 10px; }
.chart svg { width: 100%; height: auto; }
.help table { border-collapse: collapse; margin: 4px 0 10px; }
.help td, .help th { border: 1px solid #ccc; padding: 3px 10px; }
.help p { margin: 0 0 8px; }
.foot { color: #888; font-size: 12px; margin-top: 30px; }
a { color: #2f6fd6; }
"""


def _esc(text):
    return html.escape(str(text)) if text is not None else ''


def _kv(rows):
    return '<table class="kv">' + ''.join(
        f'<tr><td class="k">{_esc(k)}</td><td>{v}</td></tr>' for k, v in rows if v not in (None, '')) + '</table>'


def _plot(A):
    """The app's chart, unseen and with every value shown (the user may have hidden some in the app)."""
    plot = A.CurvePlot()
    plot.setAttribute(Qt.WA_DontShowOnScreen, True)
    plot.shown = [True] * len(plot.SERIES)
    plot._build_legend()
    plot.resize(CHART_W, CHART_H)
    plot.show()
    return plot


def _chart(plot, rows, phases, title, phase=None):
    """(svg text, QImage for the PDF) of the whole run or of one phase (start time)."""
    plot.phase_sel = phase
    plot.set_data(rows, phases, title)
    plot.title_label.setText(_esc(title), color='#222', size='11pt')
    plot.reset_btn.hide()
    for _ in range(3):                    # one normal paint first: pyqtgraph updates its view transforms there
        QApplication.processEvents()
        plot.grab()
    buf = QBuffer()
    buf.open(QIODevice.WriteOnly)
    gen = QSvgGenerator()
    gen.setOutputDevice(buf)
    gen.setSize(QSize(CHART_W, CHART_H))
    gen.setViewBox(QRect(0, 0, CHART_W, CHART_H))
    gen.setResolution(plot.logicalDpiX())     # axes are QPictures at screen resolution: same dpi, same size
    gen.setTitle(title)
    _paint(plot, QPainter(gen))
    svg = bytes(buf.data()).decode('utf-8')
    svg = re.sub(r'^<\?xml[^>]*>\s*', '', svg)
    svg = re.sub(r'font-family="([^"]*)"', r'font-family="\1, Segoe UI, Arial, sans-serif"', svg)
    svg = _join_lines(svg)
    img = QImage(CHART_W * PDF_SCALE, CHART_H * PDF_SCALE, QImage.Format_RGB32)
    img.setDevicePixelRatio(PDF_SCALE)    # not painter.scale(): the legend's texts would stay at half size
    img.fill(QColor('white'))
    _paint(plot, QPainter(img))
    return svg, img


POLYLINE = re.compile(r'<polyline ([^>]*?)points="([^"]*)"\s*/>\s*')


def _join_lines(svg):
    """pyqtgraph draws a curve as thousands of two-point lines; consecutive ones that continue each other (same
    attributes, the next starts where the last ended) become one polyline - a fraction of the size, same picture."""
    out, run = [], None                   # run: [attributes, points]
    pos = 0
    for m in POLYLINE.finditer(svg):
        attrs, pts = m.group(1), m.group(2).split()
        if run and m.start() == pos and attrs == run[0] and pts and run[1][-1] == pts[0]:
            run[1] += pts[1:]
        else:
            if run:
                out.append(f'<polyline {run[0]}points="{" ".join(run[1])}"/>')
            out.append(svg[pos:m.start()])
            run = [attrs, pts]
        pos = m.end()
    if run:
        out.append(f'<polyline {run[0]}points="{" ".join(run[1])}"/>')
    out.append(svg[pos:])
    return ''.join(out)


def _paint(plot, p):
    """The chart and the watermark top left into painter p (which is ended)."""
    p.setRenderHint(QPainter.Antialiasing)
    p.fillRect(QRectF(0, 0, CHART_W, CHART_H), QColor('white'))
    # the scene, not the widget: QWidget.render into another painter misplaces pyqtgraph's view boxes
    plot.scene().render(p, QRectF(0, 0, CHART_W, CHART_H), QRectF(plot.viewport().rect()))
    f = QFont('Segoe UI')
    f.setPixelSize(12)
    p.setFont(f)
    p.setPen(QColor('#9a9a9a'))
    p.drawText(8, 16, WATERMARK)
    p.end()


def _details(A, panel):
    """(label, value, explanation html) of the result panel's fields, in its order."""
    forms = panel.findChildren(QFormLayout)
    tips = panel.field_tips()
    out = []
    for k, w in panel.f.items():
        label = next((form.labelForField(w) for form in forms if form.labelForField(w) is not None), None)
        name = label.text().rstrip(':') if label is not None else k
        out.append((name, w.text(), tips.get(k, '')))
    return out


def build(db, d, battery_id, dev_name, lang, with_phases=True, esp=None):
    """The report of session d (dict like DB.session_dict() with 'phases') in lang. esp: the charger is read
    through a BattBench-ESP - its firmware and address ('' = not known), None = not.
    Returns a Report for write_html() / write_pdf()."""
    from . import app as A                # the app's widgets and formatting (imports this module lazily)
    with language(lang):
        return _build(A, db, d, battery_id, dev_name, with_phases, esp)


def _build(A, db, d, battery_id, dev_name, with_phases, esp):
    panel = A.ResultPanel(db)
    panel.dev_name = dev_name
    panel.show_session(d, battery_id)
    b = db.battery(battery_id, deleted=True) if battery_id is not None else None
    m = db.model(b['model_id']) if b and b['model_id'] else None
    dev = next((x for x in db.devices() if x['id'] == d.get('dev')), {})
    slot = tr('Slot {}').format(d['slot'] + 1)
    label = d.get('label')
    if label is None and d.get('id') is not None:
        row = db.con.execute('SELECT label FROM sessions WHERE id=?', (d['id'],)).fetchone()
        label = row[0] if row else ''
    battery = f"#{b['id']} {b['name']}" if b else tr('– no battery assigned –')
    head = ' · '.join(x for x in (battery if b else '', dev_name(d.get('dev')), slot, A.fmt_t(d['start'])) if x)

    color = re.search(r'background:\s*(#[0-9a-fA-F]{3,6})', panel.grade.styleSheet())
    color = color.group(1) if color else '#888888'
    rating = (f'<table class="grade" width="100%"><tr><td bgcolor="{color}" style="background:{color}">'
              f'{_esc(panel.grade.text())}</td></tr></table>' +             # a table: the PDF knows no div colours
              (f'<p class="note">{_esc(panel.note.text())}</p>' if panel.note.text() else ''))

    overview = _kv([
        (tr('Battery:'), _esc(battery) + (f"<br><i>{_esc(b['description'])}</i>" if b and b['description'] else '')),
        (tr('Model:'), _esc(A.model_text(m['maker'], m['name'], m['capacity'], m['type'])) if m else ''),
        (tr('Nominal capacity:'), f"{d['nominal']} mAh" if d.get('nominal') else ''),
        (tr('Charger') + ':', _esc(dev_name(d.get('dev'))) +
         (f" ({_esc(dev.get('model'))})" if dev.get('model') else '')),
        (tr('Charger version:'), _esc(dev.get('version'))),
        (tr('Slot') + ':', str(d['slot'] + 1)),
        (tr('Task') + ':', _esc(tr_data(d['task']))),
        (tr('Status') + ':', _esc(panel.f['status'].text())),
        (tr('Time') + ':', _esc(panel.f['time'].text())),
        (tr('Note:'), _esc(label)),
    ])
    software = _kv([
        ('BattBench', _esc(full_version())),
        ('BattBench-ESP', _esc(esp or tr('connected through it')) if esp is not None else ''),
        (tr('Operating system'), _esc(platform.platform())),
    ])

    rows = db.samples(d['dev'], d['slot'], d['start'], d['end'] + 1)
    phases = list(d.get('phases') or [])
    plot = _plot(A)
    plot.show_curve = not A.is_measuring(d)
    charts = [(tr('Whole run'), *_chart(plot, rows, phases, head))]
    if with_phases and len(phases) > 1:
        for i, (kind, a, e, mah) in enumerate(phases):
            title = tr('Phase {}: {}').format(i + 1, tr_data(kind)) + \
                f' · {datetime.fromtimestamp(a):%H:%M} · {A.fmt_dur(e - a)} · {mah} mAh'
            charts.append((title, *_chart(plot, rows, phases, head + ' · ' + title, a)))
    plot.close()
    plot.deleteLater()

    details = _details(A, panel)
    detail_table = '<table class="list"><tr><th>' + tr('Value') + '</th><th>' + tr('Result') + '</th></tr>' + ''.join(
        f'<tr><td>{_esc(n)}</td><td>{_esc(v) or "–"}</td></tr>' for n, v, _t in details) + '</table>'
    phase_table = ('<table class="list"><tr>' + ''.join(
        f'<th>{_esc(h)}</th>' for h in (tr('Phase'), tr('Start'), tr('Duration'), 'mAh')) + '</tr>' + ''.join(
        f'<tr><td><span style="color:{A.PHASE_COLORS.get(kind, "#000")}">{_esc(tr_data(kind))}</span></td>'
        f'<td>{datetime.fromtimestamp(a):%H:%M}</td><td>{A.fmt_dur(e - a)}</td><td>{mah}</td></tr>'
        for kind, a, e, mah in phases) + '</table>') if phases else ''
    panel.deleteLater()

    def h2(text):
        return '<h2>' + _esc(text) + '</h2>'

    created = tr('Created {} with BattBench {}').format(datetime.now().strftime('%Y-%m-%d %H:%M'), full_version())
    # (html, chart index or None): a block stays together on one PDF page, so a heading never ends a page
    blocks = [('<h1>' + _esc(tr('BattBench session report')) + f'</h1><p class="sub">{_esc(head)}</p>', None),
              (h2(tr('Rating')) + rating, None),
              (h2(tr('Overview')) + overview, None),
              (h2(tr('Software')) + software, None),
              (h2(tr('Chart')), 0),
              (h2(tr('Details')) + detail_table, None)]
    if phase_table:
        blocks.append((h2(tr('Phases')) + phase_table, None))
    for i in range(1, len(charts)):
        blocks.append(((h2(tr('Charge and discharge phases')) if i == 1 else '') +
                       f'<h3>{_esc(charts[i][0])}</h3>', i))
    tips = [(n, t) for n, _v, t in details if t]
    breaks = {len(blocks)} if tips else set()          # the explanations start a new PDF page
    for j, (n, t) in enumerate(tips):
        blocks.append(((h2(tr('Explanations')) if j == 0 else '') + f'<h3>{_esc(n)}</h3><div class="tip">{t}</div>',
                       None))
    for j, part in enumerate(_help_section(tr('Rating'))):     # how the rating is worked out, from the user guide
        blocks.append(((h2(tr('Rating details')) if j == 0 else '') + part, None))
    blocks.append((f'<p class="foot">{_esc(created)} · <a href="{SOURCE}">{_esc(SOURCE)}</a></p>', None))
    return Report(tr('BattBench session report') + ' · ' + head, blocks, charts, breaks)


def _help_section(title):
    """The user guide's section "### title" in the current language (English if not translated), as html parts
    (paragraphs; a paragraph ending in ':' stays with what follows, e.g. its table)."""
    folder = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'help')
    for lang, head in ((i18n.current, title), ('en', 'Rating')):
        path = os.path.join(folder, f'help_{lang}.md')
        if not os.path.exists(path):
            continue
        with open(path, encoding='utf-8') as f:
            m = re.search(rf'^### {re.escape(head)}\n(.*?)(?=^#{{1,3}} |\Z)', f.read(), re.M | re.S)
        if m:
            break
    else:
        return []
    parts = []
    for chunk in (c.strip() for c in m.group(1).split('\n\n')):
        if not chunk:
            continue
        if parts and parts[-1].endswith(':'):
            parts[-1] += '\n\n' + chunk
        else:
            parts.append(chunk)
    return [_markdown(p) for p in parts]


def _markdown(text):
    doc = QTextDocument()
    doc.setMarkdown(text)
    body = re.search(r'<body[^>]*>(.*)</body>', doc.toHtml(), re.S)
    return '<div class="help">' + (body.group(1) if body else _esc(text)) + '</div>'


class Report:
    """A built report: title, blocks (html, chart index), charts (title, svg, QImage) and the indexes of the blocks
    that start a new page in the PDF."""

    def __init__(self, title, blocks, charts, breaks=()):
        self.title, self.blocks, self.charts, self.breaks = title, blocks, charts, set(breaks)


def write_html(path, rep):
    body = '\n'.join(text + (f'<div class="chart">{rep.charts[i][1]}</div>' if i is not None else '')
                     for text, i in rep.blocks)
    with open(path, 'w', encoding='utf-8') as f:
        f.write('<!doctype html>\n<html><head><meta charset="utf-8">'
                '<meta name="viewport" content="width=device-width, initial-scale=1">'
                f'<title>{_esc(rep.title)}</title><style>{CSS}</style></head><body>\n{body}\n</body></html>\n')


PDF_MARGIN_MM = 12                    # all round; DIN A4 portrait
PDF_DPI = 96                          # painter units = CSS pixels: the same sizes as the HTML page
PDF_GAP = 10                          # px below a chart
# px above a block starting like this (not at the top of a page)
PDF_SPACE_BEFORE = {'<h2>': 26, '<h3>': 14, '<div class="help">': 8, '<p class="foot">': 18}
# blocks are set one by one: the space above comes from PDF_SPACE_BEFORE, these give the room below the headings
PDF_CSS = """
h1 { margin: 0 0 6px; }
h2 { margin: 0 0 16px; padding-bottom: 4px; }
h3 { margin: 10px 0 6px; }
.sub { margin: 0 0 6px; }
table { margin-top: 10px; }
"""


def write_pdf(path, rep):
    """DIN A4 portrait, set block by block: a block that does not fit on the rest of a page starts the next one
    (headings stay with what follows them); charts span the width between the margins."""
    w = QPdfWriter(path)
    w.setPageSize(QPageSize(QPageSize.A4))
    w.setPageOrientation(QPageLayout.Portrait)
    w.setPageMargins(QMarginsF(0, 0, 0, 0))
    w.setResolution(PDF_DPI)
    w.setTitle(rep.title)
    w.setCreator('BattBench')
    m = PDF_MARGIN_MM / 25.4 * PDF_DPI
    page = QRectF(0, 0, w.width(), w.height())
    body = page.adjusted(m, m, -m, -m - 16)                 # room for the page number
    p = QPainter(w)
    p.setRenderHint(QPainter.Antialiasing)
    p.setRenderHint(QPainter.SmoothPixmapTransform)
    paper = QPalette()                                      # black on white, whatever the app's theme
    for role, color in ((QPalette.Text, '#1d1d1d'), (QPalette.WindowText, '#1d1d1d'), (QPalette.Base, 'white'),
                        (QPalette.Window, 'white'), (QPalette.Link, '#2f6fd6')):
        paper.setColor(role, QColor(color))
    p.fillRect(page, QColor('white'))                       # some viewers show a page without a fill grey
    state = {'y': body.top(), 'page': 1}

    def footer():
        f = QFont('Segoe UI')
        f.setPixelSize(10)
        p.setFont(f)
        p.setPen(QColor('#888888'))
        p.drawText(QRectF(body.left(), body.bottom() + 4, body.width(), 14), Qt.AlignRight | Qt.AlignVCenter,
                   str(state['page']))

    def new_page():
        footer()
        w.newPage()
        p.fillRect(page, QColor('white'))
        state['y'], state['page'] = body.top(), state['page'] + 1

    for i, (text, chart) in enumerate(rep.blocks):
        doc = _pdf_doc(text, body.width(), w)
        h = doc.size().height()
        ch = body.width() * CHART_H / CHART_W if chart is not None else 0
        gap = next((g for tag, g in PDF_SPACE_BEFORE.items() if text.startswith(tag)), 0)
        if state['y'] > body.top() and (i in rep.breaks or state['y'] + gap + h + ch > body.bottom()):
            new_page()
        if state['y'] > body.top():                          # room above a heading, not at the top of a page
            state['y'] += gap
        top = 0                                              # a block higher than a page: in slices
        while top < h:
            part = min(h - top, body.bottom() - state['y'])
            p.save()
            p.translate(body.left(), state['y'] - top)
            ctx = QAbstractTextDocumentLayout.PaintContext()
            ctx.palette = paper                              # not the app's palette: dark mode gave white text
            ctx.clip = QRectF(0, top, body.width(), part)
            p.setClipRect(ctx.clip)
            doc.documentLayout().draw(p, ctx)
            p.restore()
            state['y'] += part
            top += part
            if top < h:
                new_page()
        if chart is not None:
            if state['y'] + ch > body.bottom():
                new_page()
            p.drawImage(QRectF(body.left(), state['y'], body.width(), ch), rep.charts[chart][2])
            state['y'] += ch + PDF_GAP
    footer()
    p.end()


def _pdf_doc(text, width, device):
    doc = QTextDocument()
    doc.documentLayout().setPaintDevice(device)
    f = QFont('Segoe UI')
    f.setPixelSize(13)
    doc.setDefaultFont(f)
    doc.setDocumentMargin(0)
    doc.setDefaultStyleSheet(CSS + PDF_CSS)
    doc.setHtml(text)
    doc.setTextWidth(width)
    return doc
