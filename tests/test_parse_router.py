from app.parse_quality import assess_result
from app.parse_router import fuse_results, plan_page, plan_routes


def block(kind, text, bbox, identity="b", engine="pymupdf"):
    return {"block_id": identity, "page": 1, "type": kind, "content_text": text,
            "bbox": bbox, "engine": engine, "order": 0, "selected": True, "quality_status": "pass"}


def page(blocks, text="Normal native text", **extra):
    return {"page": 1, "geometry": {"width": 600, "height": 800}, "blocks": blocks,
            "native_text": text, "image_count": 0, "drawing_count": 0, **extra}


def test_short_title_does_not_trigger_ocr():
    result = plan_page(page([block("heading", "Title", [50, 40, 220, 65])], text="Title"))
    assert result["route"] == "native"


def test_known_ordinary_scan_uses_tesseract_and_unknown_scan_uses_mineru():
    ordinary = page([block("paragraph", "", [40, 40, 550, 720])], text="", image_count=1)
    unknown = page([block("figure", "", [0, 0, 600, 800])], text="", image_count=1)
    assert plan_page(ordinary)["route"] == "tesseract"
    assert plan_page(unknown)["route"] == "mineru_page"


def test_dense_vector_graphics_do_not_depend_on_image_xrefs():
    result = plan_page(page([block("paragraph", "Some text", [30, 30, 550, 60])], drawing_count=35))
    assert result["route"] == "mineru_page"
    assert "dense_vector_graphics" in result["reason_codes"]


def test_region_fusion_keeps_native_prose_and_replaces_table_once():
    paragraph = block("paragraph", "Introduction", [40, 40, 550, 80], "intro")
    table = block("table", "damaged table", [40, 300, 550, 450], "table")
    source = page([paragraph, table])
    source["route_plan"] = plan_page(source)
    converted = [block("paragraph", "Introduction", [40, 40, 550, 80], "m-intro", "mineru"),
                 {**block("table", "Year | Value\n2025 | -1.25", [40, 300, 550, 450], "m-table", "mineru"), "html": "<table></table>"}]
    blocks, route = fuse_results(source, converted)
    selected = [b for b in blocks if b["selected"]]
    assert route == "hybrid"
    assert [b["block_id"] for b in selected] == ["intro", "m-table"]
    assert source["blocks"][1]["selected"]  # 融合不改写输入证据。


def test_ambiguous_region_upgrades_to_whole_page_ownership():
    source = page([block("paragraph", "Native introduction", [40, 30, 550, 100], "intro"),
                   block("formula", "bad formula", [40, 110, 550, 150], "formula")])
    source["route_plan"] = {"route": "mineru_region"}
    converted = [block("formula", "x=y", [40, 40, 550, 150], "combined", "mineru")]
    blocks, route = fuse_results(source, converted)
    assert route == "mineru"
    assert [b["block_id"] for b in blocks if b["selected"]] == ["combined"]


def test_different_locations_with_same_numbers_are_not_deduplicated():
    source = page([])
    source["route_plan"] = {"route": "mineru_page"}
    converted = [block("paragraph", "100", [40, 40, 80, 60], "one", "mineru"),
                 block("paragraph", "100", [40, 100, 80, 120], "two", "mineru")]
    blocks, _ = fuse_results(source, converted)
    assert sum(b["selected"] for b in blocks) == 2


def test_complex_structure_loss_is_never_quality_pass():
    result = page([block("paragraph", "Plain OCR text", [40, 40, 550, 500])])
    result["text"] = "Plain OCR text"
    quality = assess_result(result, {"table"})
    assert quality["status"] == "needs_review"
    assert any(item["code"] == "missing_complex_structure" for item in quality["issues"])


def test_only_invalid_ordinary_regions_are_requested_for_ocr_on_complex_page():
    source = page([block("paragraph", "Good native text", [40, 40, 550, 80], "good"),
                   block("paragraph", "bad \ufffd\ufffd text", [40, 100, 550, 150], "bad"),
                   block("formula", "\ufffd\ufffd", [40, 200, 550, 230], "math")])
    result = plan_page(source)
    assert result["route"] == "mineru_region"
    assert result["ocr_block_ids"] == ["bad"]


def test_table_continuation_expands_only_explicit_adjacent_page():
    first = page([block("table", "Year | Value", [40, 600, 550, 790], "table")])
    second = {**page([block("paragraph", "续表 1", [40, 40, 550, 80], "continuation")], text="续表 1"), "page": 2}
    third = {**page([block("paragraph", "A new chapter", [40, 40, 550, 80], "chapter")]), "page": 3}
    plans = plan_routes([first, second, third])
    assert plans[2]["route"] == "mineru_page"
    assert plans[2]["reason_codes"] == ["possible_table_continuation"]
    assert plans[3]["route"] == "native"
