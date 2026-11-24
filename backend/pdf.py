"""A small PDF writer (standard library only) for the readable backup report.

It lays out text on A4 landscape pages using the PDF's built-in fonts, so no fonts or
libraries need installing. Tables use Courier, whose letters are all the same width,
so columns line up by padding.
"""

import zlib

PAGE_W, PAGE_H = 842, 595          # A4 landscape, in points
MARGIN = 36
BODY_SIZE = 8.5
LINE = BODY_SIZE * 1.45
COURIER_W = 0.6                    # every Courier letter is 0.6 × the font size wide

# Characters the built-in fonts can't show are swapped for close plain ones
_PLAIN = {"—": "-", "–": "-", "’": "'", "‘": "'", "“": '"', "”": '"', "•": "*", "…": "...",
          "→": "->", "·": "-", "✅": "", "⚠": "!", "️": ""}


def _text(s):
    s = "".join(_PLAIN.get(ch, ch) for ch in str(s))
    s = s.encode("latin-1", "replace").decode("latin-1")
    return s.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


class Document:
    def __init__(self, title):
        self.title = title
        self.pages = []            # each page is a list of drawing commands
        self.y = 0
        self._new_page()

    def _new_page(self):
        self.pages.append([])
        self.y = PAGE_H - MARGIN

    def _room(self, height):
        if self.y - height < MARGIN + 14:
            self._new_page()

    def _put(self, x, text, font="F3", size=BODY_SIZE, rgb=(0, 0, 0)):
        self.pages[-1].append("%.3f %.3f %.3f rg BT /%s %.1f Tf %.1f %.1f Td (%s) Tj ET"
                              % (rgb[0], rgb[1], rgb[2], font, size, x, self.y, _text(text)))

    def heading(self, text, size=15):
        self._room(size * 2)
        self.y -= size
        self._put(MARGIN, text, "F2", size, (0.05, 0.35, 0.12))
        self.y -= size * 0.6

    def text(self, text, size=9, bold=False, grey=False):
        self._room(size * 1.5)
        self.y -= size * 1.4
        self._put(MARGIN, text, "F2" if bold else "F1", size, (0.4, 0.4, 0.4) if grey else (0, 0, 0))

    def space(self, points=6):
        self.y -= points

    def rule(self):
        self._room(6)
        self.y -= 4
        self.pages[-1].append("0.75 0.75 0.75 RG 0.5 w %d %.1f m %d %.1f l S"
                              % (MARGIN, self.y, PAGE_W - MARGIN, self.y))
        self.y -= 2

    def table(self, columns, rows, totals=None):
        """columns: [(title, width_in_letters, "l" or "r")]; rows: lists of strings."""
        def line(cells, bold=False, shade=False):
            parts = []
            for (title, width, align), cell in zip(columns, cells):
                cell = str(cell)
                if len(cell) > width:
                    cell = cell[:width - 1] + "~"
                parts.append(cell.rjust(width) if align == "r" else cell.ljust(width))
            self._room(LINE)
            self.y -= LINE
            if shade:
                self.pages[-1].append("0.93 0.96 0.93 rg %d %.1f %d %.1f re f"
                                      % (MARGIN - 2, self.y - 2.5, PAGE_W - 2 * MARGIN + 4, LINE))
            self._put(MARGIN, "  ".join(parts), "F4" if bold else "F3")

        header = [c[0] for c in columns]
        line(header, bold=True, shade=True)
        for i, row in enumerate(rows):
            if self.y - LINE < MARGIN + 14:          # repeat the header on a new page
                self._new_page()
                line(header, bold=True, shade=True)
            line(row)
        if totals:
            line(totals, bold=True, shade=True)

    def render(self):
        """The finished file, as bytes."""
        objects = []

        def add(body):
            objects.append(body)
            return len(objects)

        fonts = {name: add("<< /Type /Font /Subtype /Type1 /BaseFont /%s /Encoding /WinAnsiEncoding >>" % base)
                 for name, base in (("F1", "Helvetica"), ("F2", "Helvetica-Bold"),
                                    ("F3", "Courier"), ("F4", "Courier-Bold"))}
        font_res = " ".join("/%s %d 0 R" % (k, v) for k, v in fonts.items())
        pages_id = len(objects) + 2 * len(self.pages) + 1   # comes right after each page's two objects
        page_ids = []
        total = len(self.pages)
        for n, commands in enumerate(self.pages, 1):
            footer = ("0.45 0.45 0.45 rg BT /F1 7.5 Tf %d %d Td (%s) Tj ET"
                      % (MARGIN, MARGIN - 18, _text("%s  -  page %d of %d" % (self.title, n, total))))
            stream = zlib.compress("\n".join(commands + [footer]).encode("latin-1"))
            content = add(b"<< /Length %d /Filter /FlateDecode >>\nstream\n" % len(stream) + stream + b"\nendstream")
            page_ids.append(add("<< /Type /Page /Parent %d 0 R /MediaBox [0 0 %d %d] /Contents %d 0 R "
                                "/Resources << /Font << %s >> >> >>" % (pages_id, PAGE_W, PAGE_H, content, font_res)))
        assert add("<< /Type /Pages /Kids [%s] /Count %d >>"
                   % (" ".join("%d 0 R" % p for p in page_ids), len(page_ids))) == pages_id
        catalog = add("<< /Type /Catalog /Pages %d 0 R >>" % pages_id)
        info = add("<< /Title (%s) /Producer (PetroKenya) >>" % _text(self.title))

        out = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
        offsets = []
        for i, body in enumerate(objects, 1):
            offsets.append(len(out))
            out += b"%d 0 obj\n" % i
            out += body if isinstance(body, bytes) else body.encode("latin-1")
            out += b"\nendobj\n"
        xref = len(out)
        out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
        for off in offsets:
            out += b"%010d 00000 n \n" % off
        out += b"trailer\n<< /Size %d /Root %d 0 R /Info %d 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (
            len(objects) + 1, catalog, info, xref)
        return bytes(out)
