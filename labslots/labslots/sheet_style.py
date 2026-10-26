"""Look of the Google Sheet, in the website's "class routine" style (DESIGN.md).

style_requests() returns one spreadsheets.batchUpdate request list that formats
every tab: routine-blue header rows, group-coloured routine cells, day dividers,
today's columns, zebra rows, widths, date formats, warnings on the tabs only the
system writes. It replaces the sync's earlier conditional formats and bandings,
so it can run again whenever groups, colours or row counts change.
"""

from __future__ import annotations

FONT, MONO = "Noto Sans", "Roboto Mono"
BLUE, BLUE_DEEP, BLUE_TINT = "#1d4e89", "#163d6c", "#e6eef8"
INK, INK_2, INK_3 = "#15202b", "#47525e", "#66717d"
PANEL, ZEBRA, RULE_SOFT, NIGHT = "#eef2f6", "#f6f8fb", "#c6cfd9", "#f3f6fa"
RED, RED_TINT = "#b42318", "#fdecea"
AMBER, AMBER_TINT, AMBER_HI = "#7a4f00", "#fff3d6", "#f2b705"
GREEN, GREEN_TINT = "#1e6b3b", "#e5f3ea"
SYSTEM_DESC = "labslots: written by the slot system"


def rgb(hex_: str) -> dict:
    h = hex_.lstrip("#")
    return {"red": int(h[0:2], 16) / 255, "green": int(h[2:4], 16) / 255, "blue": int(h[4:6], 16) / 255}


def _range(sid, r0=0, r1=None, c0=0, c1=None) -> dict:
    out = {"sheetId": sid, "startRowIndex": r0, "startColumnIndex": c0}
    if r1 is not None:
        out["endRowIndex"] = r1
    if c1 is not None:
        out["endColumnIndex"] = c1
    return out


def _cell(rng, fmt: dict, fields: str) -> dict:
    return {"repeatCell": {"range": rng, "cell": {"userEnteredFormat": fmt}, "fields": f"userEnteredFormat({fields})"}}


def _text(color=INK, bold=False, size=10, font=FONT) -> dict:
    return {"foregroundColorStyle": {"rgbColor": rgb(color)}, "bold": bold, "fontSize": size, "fontFamily": font}


def _widths(sid, widths) -> list[dict]:
    return [{"updateDimensionProperties": {"range": {"sheetId": sid, "dimension": "COLUMNS", "startIndex": i,
                                                     "endIndex": i + 1},
                                           "properties": {"pixelSize": w}, "fields": "pixelSize"}}
            for i, w in enumerate(widths)]


def _height(sid, r0, r1, px) -> dict:
    return {"updateDimensionProperties": {"range": {"sheetId": sid, "dimension": "ROWS", "startIndex": r0, "endIndex": r1},
                                          "properties": {"pixelSize": px}, "fields": "pixelSize"}}


def _props(sid, tab_color, index, frozen_cols=0) -> dict:
    return {"updateSheetProperties": {"properties": {
        "sheetId": sid, "index": index, "tabColorStyle": {"rgbColor": rgb(tab_color)},
        "gridProperties": {"frozenRowCount": 1, "frozenColumnCount": frozen_cols, "hideGridlines": True}},
        "fields": "index,tabColorStyle,gridProperties.frozenRowCount,gridProperties.frozenColumnCount,"
                  "gridProperties.hideGridlines"}}


def _table(sid, ncols, nrows, widths, wrap=False, header=BLUE) -> list[dict]:
    """A list tab: header row, body font, zebra rows over the rows in use, column widths."""
    last = max(nrows, 2) + 1
    return [
        _cell(_range(sid, 1, 1000, 0, ncols), {
            "textFormat": _text(), "verticalAlignment": "MIDDLE",
            "wrapStrategy": "WRAP" if wrap else "CLIP",
            "padding": {"top": 4, "bottom": 4, "left": 8, "right": 8}},
            "textFormat,verticalAlignment,wrapStrategy,padding"),
        _cell(_range(sid, 0, 1, 0, ncols), {
            "backgroundColorStyle": {"rgbColor": rgb(header)}, "textFormat": _text("#ffffff", True),
            "verticalAlignment": "MIDDLE", "wrapStrategy": "CLIP",
            "padding": {"top": 6, "bottom": 6, "left": 8, "right": 8}},
            "backgroundColorStyle,textFormat,verticalAlignment,wrapStrategy,padding"),
        _height(sid, 0, 1, 34),
        {"addBanding": {"bandedRange": {"range": _range(sid, 0, last, 0, ncols), "rowProperties": {
            "headerColorStyle": {"rgbColor": rgb(header)}, "firstBandColorStyle": {"rgbColor": rgb("#ffffff")},
            "secondBandColorStyle": {"rgbColor": rgb(ZEBRA)}}}}},
        {"updateBorders": {"range": _range(sid, 0, last, 0, ncols),
                           "bottom": {"style": "SOLID_MEDIUM", "colorStyle": {"rgbColor": rgb(INK)}},
                           "innerHorizontal": {"style": "SOLID", "colorStyle": {"rgbColor": rgb(RULE_SOFT)}}}},
    ] + _widths(sid, widths)


def _cf(sid, rng, condition: dict, fmt: dict, index: int = 0) -> dict:
    return {"addConditionalFormatRule": {"index": index, "rule": {
        "ranges": [rng], "booleanRule": {"condition": condition, "format": fmt}}}}


def _fill(bg, fg=INK, bold=False) -> dict:
    return {"backgroundColorStyle": {"rgbColor": rgb(bg)},
            "textFormat": {"foregroundColorStyle": {"rgbColor": rgb(fg)}, "bold": bold}}


def style_requests(meta: dict, rows: dict[str, list[list]], groups: list[dict], tabs: dict) -> list[dict]:
    """meta: spreadsheets.get with conditionalFormats/bandedRanges/protectedRanges.
    rows: current values per tab. groups: [{slug, short, color}] in Groups-tab order.
    tabs: the tab names {"readme", "groups", "pcs", "terms", "admins", "problems", "enrollments", "routine": [..]}."""
    reqs: list[dict] = []
    ids = {s["properties"]["title"]: s["properties"]["sheetId"] for s in meta.get("sheets", [])}
    # clear what an earlier run added, so this is repeatable
    for s in meta.get("sheets", []):
        sid = s["properties"]["sheetId"]
        for i in reversed(range(len(s.get("conditionalFormats", [])))):
            reqs.append({"deleteConditionalFormatRule": {"sheetId": sid, "index": i}})
        for b in s.get("bandedRanges", []):
            reqs.append({"deleteBanding": {"bandedRangeId": b["bandedRangeId"]}})
    protected = {s["properties"]["title"] for s in meta.get("sheets", [])
                 for p in s.get("protectedRanges", []) if p.get("description") == SYSTEM_DESC}

    order = [tabs["readme"]] + tabs["routine"] + [tabs[k] for k in ("groups", "pcs", "terms", "admins",
                                                                     "problems", "enrollments")]
    index = {t: i for i, t in enumerate(t for t in order if t in ids)}

    def n(tab):
        return len(rows.get(tab, []))

    # the first, plain build tinted whole header rows: clear everything right of each tab's columns
    widths = {tabs["groups"]: 6, tabs["pcs"]: 5, tabs["terms"]: 3, tabs["admins"]: 1, tabs["problems"]: 5,
              tabs["enrollments"]: 11, **{t: 15 for t in tabs["routine"]}}
    for t, ncols in widths.items():
        if t in ids:
            reqs.append(_cell(_range(ids[t], 0, 1000, ncols, 40), {"backgroundColorStyle": {"rgbColor": rgb("#ffffff")},
                                                                  "textFormat": _text()},
                              "backgroundColorStyle,textFormat"))

    # Read me
    if tabs["readme"] in ids:
        sid = ids[tabs["readme"]]
        reqs += [_props(sid, INK_2, index[tabs["readme"]]),
                 _cell(_range(sid, 0, 200, 0, 1), {"textFormat": _text(INK_2, size=11), "wrapStrategy": "WRAP",
                                                   "verticalAlignment": "MIDDLE",
                                                   "padding": {"top": 3, "bottom": 3, "left": 12, "right": 12}},
                       "textFormat,wrapStrategy,verticalAlignment,padding"),
                 _cell(_range(sid, 0, 1, 0, 1), {"textFormat": _text(BLUE, True, 18),
                                                 "padding": {"top": 14, "bottom": 10, "left": 12, "right": 12}},
                       "textFormat,padding"),
                 _height(sid, 0, 1, 52)] + _widths(sid, [900])
        reqs.append({"updateSheetProperties": {"properties": {"sheetId": sid, "gridProperties": {"frozenRowCount": 0}},
                                               "fields": "gridProperties.frozenRowCount"}})

    # Routine tabs
    for tab in tabs["routine"]:
        if tab not in ids:
            continue
        sid, last = ids[tab], max(n(tab), 2)
        reqs += [
            _props(sid, BLUE, index[tab], frozen_cols=1),
            _cell(_range(sid, 1, 1000, 1, 15), {
                "textFormat": _text(INK, True, 10, MONO), "horizontalAlignment": "CENTER",
                "verticalAlignment": "MIDDLE"}, "textFormat,horizontalAlignment,verticalAlignment"),
            _cell(_range(sid, last, 1000, 0, 1), {"backgroundColorStyle": {"rgbColor": rgb("#ffffff")}},
                  "backgroundColorStyle"),
            _cell(_range(sid, 1, last, 0, 1), {
                "textFormat": _text(INK, True, 10), "backgroundColorStyle": {"rgbColor": rgb(PANEL)},
                "verticalAlignment": "MIDDLE", "padding": {"left": 10, "right": 6}},
                "textFormat,backgroundColorStyle,verticalAlignment,padding"),
            _cell(_range(sid, 0, 1, 0, 15), {
                "backgroundColorStyle": {"rgbColor": rgb(BLUE)}, "textFormat": _text("#ffffff", True, 10),
                "horizontalAlignment": "CENTER", "verticalAlignment": "MIDDLE", "wrapStrategy": "WRAP"},
                "backgroundColorStyle,textFormat,horizontalAlignment,verticalAlignment,wrapStrategy"),
            _height(sid, 0, 1, 40), _height(sid, 1, last, 36),
        ]
        reqs += _widths(sid, [130] + [86] * 14)
        for k in range(7):  # night halves a shade darker, so day and night read apart
            reqs.append(_cell(_range(sid, 1, last, 2 + 2 * k, 3 + 2 * k),
                              {"backgroundColorStyle": {"rgbColor": rgb(NIGHT)}}, "backgroundColorStyle"))
        reqs.append({"updateBorders": {
            "range": _range(sid, 0, last, 0, 15),
            "top": {"style": "SOLID_MEDIUM", "colorStyle": {"rgbColor": rgb(INK)}},
            "bottom": {"style": "SOLID_MEDIUM", "colorStyle": {"rgbColor": rgb(INK)}},
            "left": {"style": "SOLID_MEDIUM", "colorStyle": {"rgbColor": rgb(INK)}},
            "right": {"style": "SOLID_MEDIUM", "colorStyle": {"rgbColor": rgb(INK)}},
            "innerHorizontal": {"style": "SOLID", "colorStyle": {"rgbColor": rgb(RULE_SOFT)}},
            "innerVertical": {"style": "SOLID", "colorStyle": {"rgbColor": rgb(RULE_SOFT)}}}})
        for k in range(8):  # a strong rule before each day and after the PC column
            reqs.append({"updateBorders": {"range": _range(sid, 0, last, 1 + 2 * k, 2 + 2 * k) if k < 7 else
                                           _range(sid, 0, last, 14, 15),
                                           ("left" if k < 7 else "right"): {
                                               "style": "SOLID_MEDIUM", "colorStyle": {"rgbColor": rgb(INK)}}}})
        rule_i = 0
        for g in groups:  # each group's code in its highlighter colour, as on the website
            if not g.get("short"):
                continue
            reqs.append(_cf(sid, _range(sid, 1, 1000, 1, 15), {"type": "TEXT_EQ", "values": [
                {"userEnteredValue": g["short"]}]}, _fill(g["color"], INK, True), rule_i))
            rule_i += 1
        today = "=INT((COLUMN()-2)/2)+1=WEEKDAY(TODAY(),2)"
        reqs.append(_cf(sid, _range(sid, 0, 1, 1, 15), {"type": "CUSTOM_FORMULA", "values": [
            {"userEnteredValue": today}]}, _fill(AMBER_HI, INK, True), rule_i))
        reqs.append(_cf(sid, _range(sid, 1, last, 1, 15), {"type": "CUSTOM_FORMULA", "values": [
            {"userEnteredValue": today}]}, {"backgroundColorStyle": {"rgbColor": rgb(BLUE_TINT)}}, rule_i + 1))

    # Groups
    t = tabs["groups"]
    if t in ids:
        sid = ids[t]
        reqs += [_props(sid, INK_2, index[t])] + _table(sid, 6, n(t), [150, 230, 90, 90, 270, 360], wrap=True)
        reqs.append(_cell(_range(sid, 1, 1000, 0, 1), {"textFormat": _text(INK, True, 10, MONO)}, "textFormat"))
        reqs.append(_cell(_range(sid, 1, 1000, 2, 4), {"textFormat": _text(INK, True, 10, MONO),
                                                       "horizontalAlignment": "CENTER"},
                          "textFormat,horizontalAlignment"))
        for i, g in enumerate(groups, start=1):  # the short code and colour cells wear the group's colour
            if g.get("color"):
                reqs.append(_cell(_range(sid, i, i + 1, 2, 4), {"backgroundColorStyle": {"rgbColor": rgb(g["color"])}},
                                  "backgroundColorStyle"))

    # PCs
    t = tabs["pcs"]
    if t in ids:
        sid = ids[t]
        reqs += [_props(sid, INK_2, index[t])] + _table(sid, 5, n(t), [130, 150, 150, 320, 100])
        reqs.append(_cell(_range(sid, 1, 1000, 0, 1), {"textFormat": _text(INK, True)}, "textFormat"))
        reqs.append(_cell(_range(sid, 1, 1000, 1, 2), {"textFormat": _text(INK_2, False, 10, MONO)}, "textFormat"))
        reqs.append(_cell(_range(sid, 1, 1000, 2, 3), {"horizontalAlignment": "CENTER"}, "horizontalAlignment"))
        reqs.append(_cell(_range(sid, 1, 1000, 4, 5), {"textFormat": _text(INK_2, False, 10, MONO)}, "textFormat"))
        reqs.append(_cf(sid, _range(sid, 1, 1000, 2, 3), {"type": "TEXT_EQ", "values": [{"userEnteredValue": "Yes"}]},
                        _fill(GREEN_TINT, GREEN, True)))
        reqs.append(_cf(sid, _range(sid, 1, 1000, 2, 3), {"type": "TEXT_EQ", "values": [{"userEnteredValue": "No"}]},
                        {"textFormat": {"foregroundColorStyle": {"rgbColor": rgb(INK_3)}}}, 1))

    # Terms
    t = tabs["terms"]
    if t in ids:
        sid = ids[t]
        reqs += [_props(sid, INK_2, index[t])] + _table(sid, 3, n(t), [220, 140, 140])
        reqs.append(_cell(_range(sid, 1, 1000, 0, 1), {"textFormat": _text(INK, True)}, "textFormat"))
        reqs.append(_cell(_range(sid, 1, 1000, 1, 3), {"numberFormat": {"type": "DATE", "pattern": "d mmm yyyy"}},
                          "numberFormat"))

    # Admins
    t = tabs["admins"]
    if t in ids:
        sid = ids[t]
        reqs += [_props(sid, INK_2, index[t])] + _table(sid, 1, n(t), [340])

    # Problems (system)
    t = tabs["problems"]
    if t in ids:
        sid = ids[t]
        reqs += [_props(sid, RED, index[t])] + _table(sid, 5, n(t), [300, 170, 360, 300, 150], wrap=True, header=RED)
        reqs.append(_cf(sid, _range(sid, 1, 2, 0, 5), {"type": "CUSTOM_FORMULA", "values": [
            {"userEnteredValue": '=LEFT($A$2,11)="No problems"'}]}, _fill(GREEN_TINT, GREEN, True)))
        reqs.append(_cf(sid, _range(sid, 1, 1000, 2, 3), {"type": "NOT_BLANK"},
                        {"textFormat": {"foregroundColorStyle": {"rgbColor": rgb(RED)}, "bold": True}}, 1))

    # Enrollments (system)
    t = tabs["enrollments"]
    if t in ids:
        sid = ids[t]
        reqs += [_props(sid, RULE_SOFT, index[t])] + _table(sid, 11, n(t), [44, 140, 100, 190, 280, 170, 150, 320,
                                                                             330, 210, 230], wrap=True)
        reqs.append(_cell(_range(sid, 1, 1000, 2, 3), {"horizontalAlignment": "CENTER",
                                                       "textFormat": _text(INK, True)},
                          "horizontalAlignment,textFormat"))
        for i, (word, bg, fg) in enumerate((("pending", AMBER_TINT, AMBER), ("approved", GREEN_TINT, GREEN),
                                           ("rejected", RED_TINT, RED), ("withdrawn", PANEL, INK_3))):
            reqs.append(_cf(sid, _range(sid, 1, 1000, 2, 3), {"type": "TEXT_EQ", "values": [{"userEnteredValue": word}]},
                            _fill(bg, fg, True), i))

    _clamp(reqs, {s["properties"]["sheetId"]: s["properties"].get("gridProperties", {}) for s in meta.get("sheets", [])})

    # tabs only the system writes: warn before anyone edits them
    for key in ("readme", "problems", "enrollments"):
        t = tabs[key]
        if t in ids and t not in protected:
            reqs.append({"addProtectedRange": {"protectedRange": {
                "range": {"sheetId": ids[t]}, "description": SYSTEM_DESC, "warningOnly": True}}})
    return reqs


def _clamp(node, dims: dict) -> None:
    """Keep every range inside its tab's grid (Google rejects ranges past the last row or column)."""
    if isinstance(node, list):
        for x in node:
            _clamp(x, dims)
    elif isinstance(node, dict):
        g = dims.get(node.get("sheetId")) if "sheetId" in node else None
        if g:
            for key, lim in (("endRowIndex", "rowCount"), ("endColumnIndex", "columnCount"),
                             ("startRowIndex", "rowCount"), ("startColumnIndex", "columnCount")):
                if key in node and lim in g:
                    node[key] = min(node[key], g[lim])
        for v in node.values():
            if isinstance(v, (dict, list)):
                _clamp(v, dims)
