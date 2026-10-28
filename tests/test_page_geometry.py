import math

import pymupdf
import pytest

from app.page_geometry import normalized_bbox, visual_bbox, visual_geometry


@pytest.mark.parametrize("rotation", [0, 90, 180, 270])
def test_native_bbox_roundtrip_on_rotated_cropped_page(rotation):
    document = pymupdf.open()
    page = document.new_page(width=200, height=300)
    page.set_cropbox(pymupdf.Rect(10, 20, 180, 280))
    page.set_rotation(rotation)
    geometry = visual_geometry(page)
    native = pymupdf.Rect(10, 15, 60, 55)
    visual = visual_bbox(page, native)
    recovered = pymupdf.Rect(visual) * page.derotation_matrix
    assert list(recovered) == pytest.approx(list(native), abs=0.0001)
    assert geometry["width"] == (260 if rotation in (90, 270) else 170)
    assert geometry["height"] == (170 if rotation in (90, 270) else 260)
    assert geometry["cropbox"] == [10, 20, 180, 280]
    document.close()


def test_normalized_bbox_uses_visual_page_dimensions():
    assert normalized_bbox([0.1, 0.2, 0.8, 0.9], {"width": 300, "height": 200}) == [30, 40, 240, 180]


@pytest.mark.parametrize("bbox", [[0, 0, 100, 100], [-0.1, 0, 1, 1], [0, 0, 0, 1],
                                 [0, 0, math.nan, 1], [False, 0, 1, 1]])
def test_normalized_bbox_rejects_invalid_or_wrong_units(bbox):
    with pytest.raises(ValueError):
        normalized_bbox(bbox, {"width": 200, "height": 300})


def test_native_bbox_outside_page_is_not_silently_used():
    with pymupdf.open() as document:
        page = document.new_page(width=100, height=100)
        with pytest.raises(ValueError, match="outside"):
            visual_bbox(page, [200, 200, 250, 250])
