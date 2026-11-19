# -*- coding: utf-8 -*-
"""
Make It Fit - a LibreOffice Writer extension.

  Format > Make It Fit...
      Asks for a number of pages and a standard, then re-flows the document to fit by changing
      the space between paragraphs, the margins and the line spacing - as little as possible,
      and never beyond the limits of the chosen standard:

        Normal printing   margins down to 0.5 inch, paragraph spacing down to 0, line spacing
                          down to single.
        APA (7th ed.)     1 inch margins, double-spaced text.  Only the space between paragraphs
                          (which APA says should be none) is squeezed.
        Chicago/Turabian  1 inch margins, double-spaced text, single-spaced block quotations,
                          footnotes and bibliography entries.  Only the space between
                          paragraphs is squeezed.

      If the text cannot fit in the requested number of pages within the standard, it is fitted to
      the smallest number of pages that works (say 6 instead of 5) and the result window says so.
      Format > Undo Make It Fit puts everything back.

  Format > Undo Make It Fit
      Puts the spacing and margins back as they were before the first Make It Fit.  (LibreOffice's
      own Undo cannot reverse style changes, so the original values are kept inside the document.)

  Insert > Suppress Numbering on First Page
      Leaves the title page without a page number and starts counting at 1 on the next page.
      Choose it again to undo.  A page number is added at the top right if the document has none.
"""
import json
import os

import uno
import unohelper
from com.sun.star.beans import PropertyValue
from com.sun.star.frame import XDispatch, XDispatchProvider
from com.sun.star.lang import XInitialization, XServiceInfo

INCH = 2540                      # 1/100 mm
FIRST_PAGE_STYLE = u"First Page"

TEXT = {
    "title_fit": u"Make It Fit",
    "pages": u"Number of pages (at most):",
    "standard": u"Fit within the limits of:",
    "std_print": u"Normal printing (margins down to 0.5 inch)",
    "std_apa": u"APA, 7th edition (1 inch margins, double spacing)",
    "std_chicago": u"Chicago / Turabian (1 inch margins, double spacing)",
    "std_note": u"The text is adjusted as little as possible: first the space between paragraphs, "
                u"then the margins, then the line spacing (normal printing only).  If it cannot "
                u"fit within the standard, it goes onto an extra page instead.  "
                u"Format > Undo Make It Fit puts everything back.",
    "ok": u"Fit",
    "cancel": u"Cancel",
    "open_writer": u"Open a Writer document first.",
    "already": u"The document is already %(pages)d page(s), so it fits within %(target)d.  Nothing was changed.",
    "fitted": u"Fitted to %(pages)d page(s) (%(standard)s).",
    "short": u"It cannot fit on %(target)d page(s) within %(standard)s, so it was fitted to %(pages)d.",
    "spacing": u"Space between paragraphs: %(v)d%% of what it was",
    "margins": u"Margins: %(v)s inch",
    "lines": u"Line spacing: %(v)d%% (was %(w)d%%)",
    "none_changed": u"No other changes were needed.",
    "nofirst_on": u"The title page now has no page number, and page 1 is the next page.",
    "nofirst_off": u"Numbering on the first page is back to normal.",
    "nofirst_added": u"The document had no page number, so one was added at the top right.",
    "wrong": u"Something went wrong:",
    "restored": u"Spacing and margins are back as they were before Make It Fit.",
    "nothing_to_undo": u"There is nothing to undo: Make It Fit has not changed this document.",
    "para_mismatch": u"Paragraphs were added or removed since then, so only styles and margins were restored.",
    "title_undo": u"Undo Make It Fit",
    "undo_hint": u"Format > Undo Make It Fit puts everything back.",
}
STANDARD_NAMES = {"print": u"normal printing", "apa": u"APA", "chicago": u"Chicago/Turabian"}
SINGLE_SPACED_STYLES = (u"Quotations", u"Footnote", u"Endnote", u"Bibliography 1", u"Bibliography Heading",
                        u"Block Quotation", u"Block Quote")


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _msg(ctx, frame, title, text, error=False):
    try:
        tk = ctx.ServiceManager.createInstanceWithContext("com.sun.star.awt.Toolkit", ctx)
        from com.sun.star.awt.MessageBoxType import ERRORBOX, INFOBOX
        box = tk.createMessageBox(frame.ContainerWindow if frame is not None else None,
                                  ERRORBOX if error else INFOBOX, 1, title, text)
        box.execute()
    except Exception:
        pass


def _settings_file(ctx):
    ps = ctx.ServiceManager.createInstanceWithContext("com.sun.star.util.PathSubstitution", ctx)
    return uno.fileUrlToSystemPath(ps.substituteVariables(u"$(user)/makeitfit.json", True))


def _load_settings(ctx):
    try:
        with open(_settings_file(ctx), encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save_settings(ctx, data):
    try:
        with open(_settings_file(ctx), "w", encoding="utf-8") as f:
            json.dump(data, f)
    except Exception:
        pass


def _is_direct(obj, prop):
    try:
        return obj.getPropertyState(prop).value == "DIRECT_VALUE"
    except Exception:
        return False


def _paragraphs(text):
    """Body paragraphs in order (tables are left alone)."""
    en = text.createEnumeration()
    while en.hasMoreElements():
        el = en.nextElement()
        try:
            if el.supportsService("com.sun.star.text.Paragraph"):
                yield el
        except Exception:
            continue


def _style_name(h):
    """Name of the paragraph style a holder is (a style) or uses (a paragraph)."""
    try:
        if h.supportsService("com.sun.star.style.ParagraphStyle"):
            return h.Name
    except Exception:
        pass
    try:
        return h.ParaStyleName
    except Exception:
        return u""


def _is_style(h):
    try:
        return h.supportsService("com.sun.star.style.ParagraphStyle")
    except Exception:
        return False


def page_count(doc):
    vc = doc.CurrentController.getViewCursor()
    vc.jumpToLastPage()
    return vc.getPage()


def _line_spacing(mode, height):
    ls = uno.createUnoStruct("com.sun.star.style.LineSpacing")
    ls.Mode = mode
    ls.Height = height
    return ls


# ---------------------------------------------------------------------------
# remembering the original values inside the document
# ---------------------------------------------------------------------------

SNAP_PROP = u"MakeItFitOriginal"


def _store_snapshot(doc, snap):
    """Keeps the very first original: a second Make It Fit does not overwrite it."""
    props = doc.DocumentProperties.UserDefinedProperties
    info = props.getPropertySetInfo()
    if not info.hasPropertyByName(SNAP_PROP):
        props.addProperty(SNAP_PROP, 128, json.dumps(snap))          # 128 = removable
    elif not props.getPropertyValue(SNAP_PROP):
        props.setPropertyValue(SNAP_PROP, json.dumps(snap))


def _load_snapshot(doc):
    props = doc.DocumentProperties.UserDefinedProperties
    if not props.getPropertySetInfo().hasPropertyByName(SNAP_PROP):
        return None
    try:
        return json.loads(props.getPropertyValue(SNAP_PROP) or "null")
    except Exception:
        return None


def _put_back(holder, rec):
    for prop, v in rec.items():
        if prop == "i":
            continue
        if prop == "ParaLineSpacing":
            holder.ParaLineSpacing = _line_spacing(v[0], v[1])
        else:
            setattr(holder, prop, v)


def restore_original(doc):
    snap = _load_snapshot(doc)
    if not snap:
        return TEXT["nothing_to_undo"]
    styles = doc.StyleFamilies.getByName("ParagraphStyles")
    for name, rec in snap["styles"].items():
        if styles.hasByName(name):
            _put_back(styles.getByName(name), rec)
    pstyles = doc.StyleFamilies.getByName("PageStyles")
    for name, rec in snap["pages"].items():
        if pstyles.hasByName(name):
            st = pstyles.getByName(name)
            for p, v in rec.items():
                st.setPropertyValue(p, v)
    paras = list(_paragraphs(doc.Text))
    text = TEXT["restored"]
    if len(paras) == snap.get("n"):
        for rec in snap["paras"]:
            _put_back(paras[rec["i"]], rec)
    else:
        text += u"\n\n" + TEXT["para_mismatch"]
    props = doc.DocumentProperties.UserDefinedProperties
    try:
        props.removeProperty(SNAP_PROP)
    except Exception:
        props.setPropertyValue(SNAP_PROP, u"")
    return text


# ---------------------------------------------------------------------------
# Make It Fit
# ---------------------------------------------------------------------------

class Fitter(object):
    """Holds the original values and applies a 'squeeze' u from 0 (as it was) to 3 (the limit):
         0..1  space between paragraphs  ->  0
         1..2  margins                   ->  the standard's lower limit
         2..3  line spacing              ->  single (normal printing only)"""

    def __init__(self, doc, standard):
        self.doc = doc
        self.standard = standard
        self.margin_limit = INCH if standard in ("apa", "chicago") else INCH // 2
        self.line_floor = 100 if standard == "print" else None
        self.spacing = []            # (object, property, original)
        self.lines = []              # (object, original height)
        self.pages = []              # (page style, {prop: original})
        self.snapshot = None
        self.applied = {}
        self._collect()

    def _special(self, name):
        return self.standard != "print" and name in SINGLE_SPACED_STYLES

    def _collect(self):
        doc = self.doc
        styles = doc.StyleFamilies.getByName("ParagraphStyles")
        holders = [styles.getByName(n) for n in styles.getElementNames()]
        for para in _paragraphs(doc.Text):
            holders.append(para)
        for h in holders:
            name = _style_name(h)
            for prop in ("ParaTopMargin", "ParaBottomMargin"):
                if _is_direct(h, prop):
                    self.spacing.append((h, prop, getattr(h, prop)))
            if self.line_floor is not None and not self._special(name) and _is_direct(h, "ParaLineSpacing"):
                ls = h.ParaLineSpacing
                if ls.Mode == 0 and ls.Height > self.line_floor:
                    self.lines.append((h, ls.Height))
        pstyles = doc.StyleFamilies.getByName("PageStyles")
        for n in pstyles.getElementNames():
            st = pstyles.getByName(n)
            self.pages.append((st, dict((p, getattr(st, p)) for p in
                                        ("LeftMargin", "RightMargin", "TopMargin", "BottomMargin"))))
        self.holders = holders
        self.snapshot = self._take_snapshot(styles)

    def _take_snapshot(self, styles):
        """Every value Make It Fit may change, so that it can be put back later."""
        def grab(h):
            d = {}
            for prop in ("ParaTopMargin", "ParaBottomMargin"):
                if _is_direct(h, prop):
                    d[prop] = getattr(h, prop)
            if _is_direct(h, "ParaLineSpacing"):
                ls = h.ParaLineSpacing
                d["ParaLineSpacing"] = [ls.Mode, ls.Height]
            return d
        snap = {"styles": {}, "pages": {}, "paras": [], "n": 0}
        for n in styles.getElementNames():
            d = grab(styles.getByName(n))
            if d:
                snap["styles"][n] = d
        for st, orig in self.pages:
            snap["pages"][st.Name] = dict(orig)
        paras = [h for h in self.holders if not _is_style(h)]
        snap["n"] = len(paras)
        for i, p in enumerate(paras):
            d = grab(p)
            if d:
                d["i"] = i
                snap["paras"].append(d)
        return snap

    def baseline(self):
        """Things the standards require anyway (only used when the document has to be squeezed)."""
        if self.standard == "print":
            return
        for st, orig in self.pages:
            for p in orig:
                if orig[p] < self.margin_limit:
                    orig[p] = self.margin_limit         # the standard's own margin
        for h in self.holders:
            if self._special(_style_name(h)) and _is_direct(h, "ParaLineSpacing"):
                ls = h.ParaLineSpacing
                if ls.Mode == 0 and ls.Height > 100:
                    h.ParaLineSpacing = _line_spacing(0, 100)

    def apply(self, u):
        a = min(max(u, 0.0), 1.0)
        b = min(max(u - 1.0, 0.0), 1.0)
        c = min(max(u - 2.0, 0.0), 1.0)
        for i, (obj, prop, orig) in enumerate(self.spacing):
            v = int(round(orig * (1.0 - a)))
            key = ("s", i)
            if self.applied.get(key) != v:
                setattr(obj, prop, v)
                self.applied[key] = v
        for st, orig in self.pages:
            for p, o in orig.items():
                v = int(round(o + (self.margin_limit - o) * b)) if o > self.margin_limit else o
                key = ("p", id(st), p)
                if getattr(st, p) != v:
                    setattr(st, p, v)
        for i, (obj, orig) in enumerate(self.lines):
            v = int(round(orig - (orig - self.line_floor) * c))
            key = ("l", i)
            if self.applied.get(key) != v:
                obj.ParaLineSpacing = _line_spacing(0, v)
                self.applied[key] = v

    def describe(self, u):
        out = []
        a = min(max(u, 0.0), 1.0)
        b = min(max(u - 1.0, 0.0), 1.0)
        c = min(max(u - 2.0, 0.0), 1.0)
        if self.spacing and a > 0:
            out.append(TEXT["spacing"] % {"v": int(round((1 - a) * 100))})
        if b > 0 and self.pages:
            st, orig = self.pages[0]
            for st, orig in self.pages:
                if orig["LeftMargin"] > self.margin_limit:
                    v = orig["LeftMargin"] + (self.margin_limit - orig["LeftMargin"]) * b
                    out.append(TEXT["margins"] % {"v": ("%.2f" % (v / float(INCH)))})
                    break
        if c > 0 and self.lines:
            was = max(o for _, o in self.lines)
            out.append(TEXT["lines"] % {"v": int(round(was - (was - self.line_floor) * c)), "w": was})
        return out


def make_it_fit(ctx, frame, doc, target, standard):
    """Returns the text to show the person."""
    ctrl = doc.CurrentController
    vc = ctrl.getViewCursor()
    saved = vc.getStart()
    n0 = page_count(doc)
    try:
        if n0 <= target:
            return TEXT["already"] % {"pages": n0, "target": target}
        undo = doc.getUndoManager()
        undo.enterUndoContext(TEXT["title_fit"])
        try:
            fit = Fitter(doc, standard)
            _store_snapshot(doc, fit.snapshot)
            fit.baseline()
            fit.apply(0.0)
            n_base = page_count(doc)
            fit.apply(3.0)
            n_max = page_count(doc)
            goal = max(target, n_max)
            if n_base <= goal:
                best = 0.0
            else:
                lo, hi = 0.0, 3.0
                for _ in range(11):
                    mid = (lo + hi) / 2.0
                    fit.apply(mid)
                    if page_count(doc) <= goal:
                        hi = mid
                    else:
                        lo = mid
                best = hi
            fit.apply(best)
            final = page_count(doc)
            if final > goal:                    # safety: fall back to the strongest squeeze
                best = 3.0
                fit.apply(best)
                final = page_count(doc)
        finally:
            undo.leaveUndoContext()
        name = STANDARD_NAMES[standard]
        lines = [(TEXT["fitted"] if final <= target else TEXT["short"]) %
                 {"pages": final, "target": target, "standard": name}]
        lines.append(u"")
        lines.extend(fit.describe(best) or [TEXT["none_changed"]])
        lines.extend([u"", TEXT["undo_hint"]])
        return u"\n".join(lines)
    finally:
        try:
            vc.gotoRange(saved, False)
        except Exception:
            pass


def show_fit_dialog(ctx, frame, current_pages):
    """Returns (pages, standard) or None."""
    saved = _load_settings(ctx)
    std = saved.get("standard", "print")
    smgr = ctx.ServiceManager
    dm = smgr.createInstanceWithContext("com.sun.star.awt.UnoControlDialogModel", ctx)
    dm.PositionX, dm.PositionY, dm.Width, dm.Height = 100, 80, 270, 172
    dm.Title = TEXT["title_fit"]

    def add(kind, name, x, y, w, h, **props):
        m = dm.createInstance("com.sun.star.awt.UnoControl%sModel" % kind)
        m.Name = name
        m.PositionX, m.PositionY, m.Width, m.Height = x, y, w, h
        for k, v in props.items():
            setattr(m, k, v)
        dm.insertByName(name, m)
        return m
    add("FixedText", "l1", 8, 10, 110, 10, Label=TEXT["pages"])
    add("NumericField", "pages", 122, 8, 40, 12, Value=float(saved.get("pages", current_pages) or current_pages),
        ValueMin=1.0, ValueMax=999.0, DecimalAccuracy=0, Spin=True, TabIndex=0)
    add("FixedText", "l2", 8, 30, 254, 10, Label=TEXT["standard"])
    for i, key in enumerate(("print", "apa", "chicago")):
        add("RadioButton", "rb_" + key, 14, 44 + i * 14, 248, 10, Label=TEXT["std_" + key],
            State=1 if std == key else 0, TabIndex=1 + i)
    add("FixedText", "note", 8, 90, 254, 52, Label=TEXT["std_note"], MultiLine=True)
    add("Button", "ok", 70, 150, 60, 14, Label=TEXT["ok"], PushButtonType=1, DefaultButton=True, TabIndex=4)
    add("Button", "cancel", 140, 150, 60, 14, Label=TEXT["cancel"], PushButtonType=2, TabIndex=5)
    dialog = smgr.createInstanceWithContext("com.sun.star.awt.UnoControlDialog", ctx)
    dialog.setModel(dm)
    toolkit = smgr.createInstanceWithContext("com.sun.star.awt.Toolkit", ctx)
    dialog.createPeer(toolkit, frame.ContainerWindow if frame is not None else None)
    result = dialog.execute()
    pages = int(dm.getByName("pages").Value)
    chosen = "print"
    for key in ("apa", "chicago"):
        if dm.getByName("rb_" + key).State == 1:
            chosen = key
    dialog.dispose()
    if result != 1:
        return None
    _save_settings(ctx, {"standard": chosen, "pages": pages})
    return max(1, pages), chosen


# ---------------------------------------------------------------------------
# Suppress Numbering on First Page
# ---------------------------------------------------------------------------

def _page_number_fields(style):
    """The page number fields in a page style's header and footer."""
    found = []
    for kind in ("Header", "Footer"):
        try:
            if not style.getPropertyValue(kind + "IsOn"):
                continue
            for para in _paragraphs(style.getPropertyValue(kind + "Text")):
                pe = para.createEnumeration()
                while pe.hasMoreElements():
                    po = pe.nextElement()
                    if po.TextPortionType == "TextField" and \
                            po.TextField.supportsService("com.sun.star.text.TextField.PageNumber"):
                        found.append(po.TextField)
        except Exception:
            continue
    return found


def _add_page_number(doc, style):
    style.HeaderIsOn = True
    txt = style.HeaderText
    txt.setString(u"")
    cur = txt.createTextCursor()
    cur.ParaAdjust = uno.Enum("com.sun.star.style.ParagraphAdjust", "RIGHT")
    field = doc.createInstance("com.sun.star.text.TextField.PageNumber")
    field.NumberingType = 4
    field.SubType = uno.Enum("com.sun.star.text.PageNumberType", "CURRENT")
    txt.insertTextContent(cur, field, False)


def toggle_first_page_number(doc):
    """The title page gets no number and the next page is page 1 (done with the page number field's
    own offset, so no blank pages are inserted).  Returns the text to show the person."""
    first = None
    for first in _paragraphs(doc.Text):
        break
    if first is None:
        return TEXT["open_writer"]
    pstyles = doc.StyleFamilies.getByName("PageStyles")
    undo = doc.getUndoManager()
    undo.enterUndoContext(u"Suppress Numbering on First Page")
    try:
        if first.PageDescName == FIRST_PAGE_STYLE:
            follow = u"Standard"
            if pstyles.hasByName(FIRST_PAGE_STYLE):
                follow = pstyles.getByName(FIRST_PAGE_STYLE).FollowStyle or follow
            first.PageDescName = u""
            if pstyles.hasByName(follow):
                for f in _page_number_fields(pstyles.getByName(follow)):
                    f.Offset = 0
            return TEXT["nofirst_off"]
        follow = first.PageStyleName
        if not pstyles.hasByName(follow):
            follow = u"Standard"
        base = pstyles.getByName(follow)
        if not pstyles.hasByName(FIRST_PAGE_STYLE):
            pstyles.insertByName(FIRST_PAGE_STYLE, doc.createInstance("com.sun.star.style.PageStyle"))
        fp = pstyles.getByName(FIRST_PAGE_STYLE)
        for p in ("Width", "Height", "IsLandscape", "LeftMargin", "RightMargin", "TopMargin", "BottomMargin"):
            try:
                setattr(fp, p, getattr(base, p))
            except Exception:
                pass
        fp.HeaderIsOn = False
        fp.FooterIsOn = False
        fp.FollowStyle = follow
        fields = _page_number_fields(base)
        added = not fields
        if added:
            _add_page_number(doc, base)
            fields = _page_number_fields(base)
        for f in fields:
            f.Offset = -1
        first.PageDescName = FIRST_PAGE_STYLE
    finally:
        undo.leaveUndoContext()
    text = TEXT["nofirst_on"]
    if added:
        text += u"\n\n" + TEXT["nofirst_added"]
    return text


# ---------------------------------------------------------------------------
# commands
# ---------------------------------------------------------------------------

def run_command(ctx, frame, command):
    model = frame.Controller.Model if frame is not None else None
    title = TEXT["title_fit"]
    if model is None or not model.supportsService("com.sun.star.text.TextDocument"):
        _msg(ctx, frame, title, TEXT["open_writer"], True)
        return
    try:
        if command == "Fit":
            picked = show_fit_dialog(ctx, frame, page_count(model))
            if picked is None:
                return
            _msg(ctx, frame, title, make_it_fit(ctx, frame, model, picked[0], picked[1]))
        elif command == "Restore":
            _msg(ctx, frame, TEXT["title_undo"], restore_original(model))
        elif command == "NoFirstNumber":
            _msg(ctx, frame, u"Suppress Numbering on First Page", toggle_first_page_number(model))
    except Exception as e:
        import traceback
        _msg(ctx, frame, title, u"%s\n%s\n\n%s" % (TEXT["wrong"], e, traceback.format_exc()[-500:]), True)


class MakeItFitHandler(unohelper.Base, XServiceInfo, XDispatchProvider, XDispatch, XInitialization):
    IMPL_NAME = "org.makeitfit.Handler"
    SERVICES = ("com.sun.star.frame.ProtocolHandler",)
    PROTOCOL = "org.makeitfit.cmd:"

    def __init__(self, ctx):
        self.ctx = ctx
        self.frame = None

    def initialize(self, args):
        if args:
            self.frame = args[0]

    def getImplementationName(self):
        return self.IMPL_NAME

    def supportsService(self, name):
        return name in self.SERVICES

    def getSupportedServiceNames(self):
        return self.SERVICES

    def queryDispatch(self, url, target, flags):
        return self if url.Protocol == self.PROTOCOL else None

    def queryDispatches(self, requests):
        return tuple(self.queryDispatch(r.FeatureURL, r.FrameName, r.SearchFlags) for r in requests)

    def dispatch(self, url, args):
        if url.Path in ("Fit", "Restore", "NoFirstNumber"):
            run_command(self.ctx, self.frame, url.Path)

    def addStatusListener(self, listener, url):
        pass

    def removeStatusListener(self, listener, url):
        pass


g_ImplementationHelper = unohelper.ImplementationHelper()
g_ImplementationHelper.addImplementation(MakeItFitHandler, MakeItFitHandler.IMPL_NAME, MakeItFitHandler.SERVICES)
