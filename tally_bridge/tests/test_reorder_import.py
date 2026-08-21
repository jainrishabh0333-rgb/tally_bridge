"""
Tests for the Reorder Report parser.

The parser is pure (XML string in, dicts out), so it is exercised without a
site. Expected numbers come from the real Reorder Report (Panty group,
2026-08-20), including Tally's '(-)7.50' rendering of a negative.

The live shape is now known (see TestRealReportShape): a FLAT field stream
with no row wrapper, SERNO marking each row, and truncated tag names —
SRPENDINGORD, SROREORDLBL, SRODEFSURP. The other shapes are kept because the
parser must stay tolerant: this report is custom TDL and its tags can change
without warning.
"""

import unittest

from tally_bridge.reorder_import import _number, _field_for, parse_reorder_xml

HEADER_STYLE = """<ENVELOPE><BODY><DATA>
 <ROW><ITEM>1326 CL S-XL-(Doz)</ITEM><GROUP>Panty</GROUP><SIZE>34</SIZE>
  <INSTOCK>13</INSTOCK><STITCHING>179</STITCHING><PENDINGORDER>18</PENDINGORDER>
  <REORDERLEVEL>75</REORDERLEVEL><DEFICITSURPLUS>99</DEFICITSURPLUS></ROW>
 <ROW><ITEM>2002 S-XL-(Doz)</ITEM><GROUP>Panty</GROUP><SIZE>36</SIZE>
  <INSTOCK>6</INSTOCK><UNPACKQTY>57.25</UNPACKQTY><STITCHING>176</STITCHING>
  <PENDINGORDER>75</PENDINGORDER><REORDERLEVEL>240</REORDERLEVEL>
  <DEFICITSURPLUS>(-)75.75</DEFICITSURPLUS></ROW>
</DATA></BODY></ENVELOPE>"""

VARIABLE_STYLE = """<ENVELOPE><BODY><DATA><COLLECTION>
 <LINE><RO_ArticleNameN>1326 CL S-XL-(Doz)</RO_ArticleNameN>
  <RO_ItemGrpName>Panty</RO_ItemGrpName>
  <RO_SizeColorSizeName>36</RO_SizeColorSizeName>
  <RO_InStock>3</RO_InStock><RO_Stitching>223.50</RO_Stitching>
  <RO_PendingOrder>22</RO_PendingOrder><RO_ReorderLevel>75</RO_ReorderLevel>
  <RO_DeficitSurplus>129.50</RO_DeficitSurplus></LINE>
 <LINE><RO_ArticleNameN>AK INNER 3XL-(Doz)</RO_ArticleNameN>
  <RO_ItemGrpName>Panty</RO_ItemGrpName>
  <RO_SizeColorSizeName>42</RO_SizeColorSizeName>
  <RO_InStock>7</RO_InStock><RO_UnpackQty>15</RO_UnpackQty>
  <RO_ReorderLevel>7.50</RO_ReorderLevel>
  <RO_DeficitSurplus>14.50</RO_DeficitSurplus></LINE>
</COLLECTION></DATA></BODY></ENVELOPE>"""


class TestNumberParsing(unittest.TestCase):
    def test_tally_negative_rendering(self):
        """
        '(-)7.50' must read as NEGATIVE.

        This is the highest-consequence line in the parser: strip the
        punctuation naively and a deficit of 7.50 becomes a surplus of 7.50,
        turning "cut more" into "cut nothing".
        """
        self.assertEqual(_number("(-)7.50"), -7.5)
        self.assertEqual(_number("(-)75"), -75.0)

    def test_plain_and_formatted(self):
        self.assertEqual(_number("99"), 99.0)
        self.assertEqual(_number("1,234.50"), 1234.5)
        self.assertEqual(_number("-57.25"), -57.25)
        self.assertEqual(_number("7.50 Doz"), 7.5)
        self.assertEqual(_number(""), 0.0)
        self.assertEqual(_number("--"), 0.0)


class TestFieldMapping(unittest.TestCase):
    def test_group_beats_item(self):
        """
        ITEMGRPNAME holds the GROUP, and contains the substring "item".

        If rule order regresses, every row's group is read as its item name
        and the whole import silently collapses onto a handful of fake items.
        """
        self.assertEqual(_field_for("RO_ItemGrpName"), "stock_group")
        self.assertEqual(_field_for("ITEMGRPNAME"), "stock_group")

    def test_article_variants(self):
        self.assertEqual(_field_for("RO_ArticleNameN"), "item_name")
        self.assertEqual(_field_for("ITEM"), "item_name")
        self.assertEqual(_field_for("StockItemName"), "item_name")

    def test_size_inside_colour_name(self):
        self.assertEqual(_field_for("RO_SizeColorSizeName"), "size")

    def test_reorder_level_not_pending(self):
        self.assertEqual(_field_for("RO_ReorderLevel"), "reorder_level")
        self.assertEqual(_field_for("PENDINGORDER"), "pending_order")

    def test_unknown_tag_ignored(self):
        self.assertIsNone(_field_for("SNO"))
        self.assertIsNone(_field_for("Narration"))


class TestParseReorderXml(unittest.TestCase):
    def test_header_style(self):
        rows = parse_reorder_xml(HEADER_STYLE)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["item_name"], "1326 CL S-XL-(Doz)")
        self.assertEqual(rows[0]["stock_group"], "Panty")
        self.assertEqual(rows[0]["reorder_level"], 75.0)
        self.assertEqual(rows[1]["deficit"], -75.75)

    def test_variable_style(self):
        """Tag names taken from TDL variables, not column headers."""
        rows = parse_reorder_xml(VARIABLE_STYLE)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["size"], "36")
        self.assertEqual(rows[0]["stitching"], 223.5)
        self.assertEqual(rows[1]["unpack_qty"], 15.0)

    def test_reproduces_report_arithmetic(self):
        """
        Parsed columns must satisfy the report's own formula:
            deficit = in stock + unpack + stitching - pending - level
        """
        for payload in (HEADER_STYLE, VARIABLE_STYLE):
            for row in parse_reorder_xml(payload):
                calc = (row.get("in_stock", 0) + row.get("unpack_qty", 0)
                        + row.get("stitching", 0) - row.get("pending_order", 0)
                        - row.get("reorder_level", 0))
                self.assertAlmostEqual(calc, row.get("deficit", 0), places=2,
                                       msg=f"formula broke on {row}")

    def test_empty_and_junk(self):
        self.assertEqual(parse_reorder_xml(""), [])
        self.assertEqual(parse_reorder_xml("   "), [])

    def test_rows_without_item_are_dropped(self):
        """Header/total bands carry numbers but no item, and are not rows."""
        payload = """<E><D><R><SNO>1</SNO><REORDERLEVEL>10</REORDERLEVEL></R>
                     <R><SNO>2</SNO><REORDERLEVEL>20</REORDERLEVEL></R></D></E>"""
        self.assertEqual(parse_reorder_xml(payload), [])


REAL_FLAT = """<ENVELOPE> <SERNO>1</SERNO> <SROITEMNAME>1228 INNER 3XL-(Doz)</SROITEMNAME>
 <SROITEMGRP>Panty</SROITEMGRP> <SROSIZE>42</SROSIZE> <SROINSTOCK></SROINSTOCK>
 <SRUNPACKQTY></SRUNPACKQTY> <SROSTITCHING></SROSTITCHING> <SRPENDINGORD></SRPENDINGORD>
 <SROREORDLBL>7.50</SROREORDLBL> <SRODEFSURP>-7.50</SRODEFSURP>
 <SERNO>2</SERNO> <SROITEMNAME>1326 CL S-XL-(Doz)</SROITEMNAME> <SROITEMGRP>Panty</SROITEMGRP>
 <SROSIZE>34</SROSIZE> <SROINSTOCK>13</SROINSTOCK> <SRUNPACKQTY></SRUNPACKQTY>
 <SROSTITCHING>179</SROSTITCHING> <SRPENDINGORD>18</SRPENDINGORD>
 <SROREORDLBL>75</SROREORDLBL> <SRODEFSURP>99</SRODEFSURP>
 <SERNO>3</SERNO> <SROITEMNAME>2002 S-XL-(Doz)</SROITEMNAME> <SROITEMGRP>Panty</SROITEMGRP>
 <SROSIZE>36</SROSIZE> <SROINSTOCK>6</SROINSTOCK> <SRUNPACKQTY>57.25</SRUNPACKQTY>
 <SROSTITCHING>176</SROSTITCHING> <SRPENDINGORD>75</SRPENDINGORD>
 <SROREORDLBL>240</SROREORDLBL> <SRODEFSURP>-75.75</SRODEFSURP>
</ENVELOPE>"""


class TestRealReportShape(unittest.TestCase):
    """
    The shape the live report actually sends: a FLAT field stream with no row
    wrapper, rows delimited by SERNO. Transcribed from a real Panty export
    (2026-08-21).

    Read as nested, this yields exactly as many rows, each holding only an
    item name — which is what shipped 475 rows of zeroes the first time. Hence
    the field-count scoring in parse_reorder_xml.
    """

    def test_row_count_and_sizes(self):
        rows = parse_reorder_xml(REAL_FLAT)
        self.assertEqual(len(rows), 3)
        self.assertEqual([r["size"] for r in rows], ["42", "34", "36"])

    def test_truncated_tag_names(self):
        """SRPENDINGORD, SROREORDLBL and SRODEFSURP are all truncated."""
        rows = parse_reorder_xml(REAL_FLAT)
        self.assertEqual(rows[1]["pending_order"], 18.0)
        self.assertEqual(rows[1]["reorder_level"], 75.0)
        self.assertEqual(rows[2]["deficit"], -75.75)

    def test_blank_columns_do_not_shift_fields(self):
        """Empty tags must not slide the next value into the wrong column."""
        rows = parse_reorder_xml(REAL_FLAT)
        self.assertEqual(rows[0]["reorder_level"], 7.5)
        self.assertEqual(rows[0]["deficit"], -7.5)
        self.assertNotIn("in_stock", rows[0])

    def test_formula_holds(self):
        for row in parse_reorder_xml(REAL_FLAT):
            calc = (row.get("in_stock", 0) + row.get("unpack_qty", 0)
                    + row.get("stitching", 0) - row.get("pending_order", 0)
                    - row.get("reorder_level", 0))
            self.assertAlmostEqual(calc, row["deficit"], places=2)
