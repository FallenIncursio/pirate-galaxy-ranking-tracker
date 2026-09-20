from PIL import Image, ImageDraw

import pg_rankings.ocr as ocr_module
from pg_rankings.models import RankingEntry
from pg_rankings.ocr import (
    LineResult,
    OcrError,
    OcrResult,
    _rows_from_data,
    clean_name,
    hidden_tesseract_subprocess_args,
    parse_score,
    read_score_with_zero_fallback,
    require_consensus,
    row_contains_single_zero_glyph,
)


def result(name: str, score: int) -> OcrResult:
    return OcrResult((RankingEntry(1, name, score, 90.0),), 90.0, "Division 1")


def result_with_confidence(name: str, score: int, confidence: float) -> OcrResult:
    return OcrResult(
        (RankingEntry(1, name, score, confidence),),
        90.0,
        "Division 1",
    )


def test_parse_score_accepts_german_and_english_separators() -> None:
    assert parse_score("1,692,889") == 1_692_889
    assert parse_score("1.692.889") == 1_692_889
    assert parse_score("0") == 0


def test_parse_score_rejects_non_numeric_artifacts() -> None:
    try:
        parse_score("1,69Z,889")
    except OcrError as error:
        assert "unexpected characters" in str(error)
    else:
        raise AssertionError("score artifacts should have been rejected")


def test_parse_score_rejects_implausibly_long_values() -> None:
    try:
        parse_score("1,234,567,890,123")
    except OcrError as error:
        assert "implausible" in str(error)
    else:
        raise AssertionError("implausibly long scores should have been rejected")


def zero_score_row() -> Image.Image:
    image = Image.new("RGB", (104, 30), (78, 78, 78))
    ImageDraw.Draw(image).ellipse((87, 10, 93, 18), outline="white", width=2)
    return image


def test_score_accepts_a_visually_verified_zero_when_column_ocr_is_empty() -> None:
    image = zero_score_row()

    score = read_score_with_zero_fallback(
        image,
        column_result=LineResult("", 0.0),
    )

    assert score == 0
    assert row_contains_single_zero_glyph(image)


def test_score_keeps_valid_column_result() -> None:
    score = read_score_with_zero_fallback(
        Image.new("RGB", (100, 30), "black"),
        column_result=LineResult("52,950", 90.0),
    )

    assert score == 52_950


def test_score_accepts_the_compact_800px_zero_glyph() -> None:
    image = Image.new("RGB", (68, 30), (78, 78, 78))
    glyph = (
        "00111",
        "01111",
        "11000",
        "11000",
        "11000",
        "11000",
        "11000",
        "01111",
        "00111",
    )
    pixels = image.load()
    for y, row in enumerate(glyph, start=11):
        for x, value in enumerate(row, start=59):
            if value == "1":
                pixels[x, y] = (235, 235, 235)

    assert row_contains_single_zero_glyph(image)
    assert read_score_with_zero_fallback(image, column_result=LineResult(".8", 0.0)) == 0


def test_empty_score_without_a_zero_glyph_still_fails() -> None:
    try:
        read_score_with_zero_fallback(
            Image.new("RGB", (104, 30), (78, 78, 78)),
            column_result=LineResult("", 0.0),
        )
    except OcrError as error:
        assert "unexpected characters" in str(error)
    else:
        raise AssertionError("blank score rows must not be guessed as zero")


def test_tesseract_subprocess_uses_no_window_flag_on_windows(monkeypatch) -> None:
    monkeypatch.setattr(ocr_module.sys, "platform", "win32")
    monkeypatch.setattr(ocr_module.subprocess, "CREATE_NO_WINDOW", 0x08000000, raising=False)

    kwargs = hidden_tesseract_subprocess_args()

    assert kwargs["creationflags"] & 0x08000000


def test_clean_name_preserves_expected_punctuation() -> None:
    assert clean_name(" | Pilot.Alpha ") == "Pilot.Alpha"
    assert clean_name("werki-oldman") == "werki-oldman"


def test_clean_name_applies_configured_exact_font_misread_alias() -> None:
    aliases = (("Pilotss", "Pilot98"),)
    assert clean_name("Pilotss", name_aliases=aliases) == "Pilot98"
    assert clean_name("PILOTSS", name_aliases=aliases) == "Pilot98"


def test_clean_name_does_not_rewrite_similar_unverified_names() -> None:
    aliases = (("Pilotss", "Pilot98"),)
    assert clean_name("OtherSs", name_aliases=aliases) == "OtherSs"
    assert clean_name("Pilotsss", name_aliases=aliases) == "Pilotsss"


def test_consensus_normalizes_case_and_punctuation() -> None:
    assert (
        require_consensus(result("Pilot Beta", 10), result("PILOT-BETA", 10)).entries[0].score == 10
    )


def test_consensus_rejects_score_change() -> None:
    try:
        require_consensus(result("Pilot Beta", 10), result("Pilot Beta", 11))
    except OcrError as error:
        assert "disagree" in str(error)
    else:
        raise AssertionError("consensus should have failed")


def test_consensus_accepts_live_confusable_name_duplication() -> None:
    agreed = require_consensus(
        result_with_confidence("Stellar Pilot01", 49_588, 72.5),
        result_with_confidence("Stellar PilotO0l", 49_588, 67.0),
    )

    assert agreed.entries == (RankingEntry(1, "Stellar Pilot01", 49_588, 72.5),)


def test_consensus_accepts_only_visual_glyph_substitutions() -> None:
    agreed = require_consensus(
        result("PilotO1", 123),
        result("Pilot01", 123),
    )

    assert agreed.entries[0].score == 123


def test_consensus_accepts_compact_font_l_as_tg_split() -> None:
    agreed = require_consensus(
        result("Raventg999", 99),
        result("Ravenl999", 99),
    )

    assert agreed.entries == (RankingEntry(1, "Ravenl999", 99, 90.0),)


def test_consensus_keeps_known_spelling_for_post_update_i_j_glyph() -> None:
    agreed = require_consensus(
        result_with_confidence("Rjjan", 744_215, 89.0),
        result_with_confidence("Rjjan", 744_215, 88.0),
        minimum_confidence=40.0,
        known_entries=(RankingEntry(1, "Rijan", 704_550, 90.0),),
    )

    assert agreed.entries == (RankingEntry(1, "Rijan", 744_215, 88.0),)


def test_consensus_keeps_known_two_digit_suffix_when_both_reads_return_ss() -> None:
    agreed = require_consensus(
        result_with_confidence("Pilotss", 761_767, 68.0),
        result_with_confidence("Pilotss", 761_767, 70.0),
        minimum_confidence=40.0,
        known_entries=(RankingEntry(1, "Pilot98", 734_709, 90.0),),
    )

    assert agreed.entries == (RankingEntry(1, "Pilot98", 761_767, 70.0),)


def test_consensus_rejects_arbitrary_long_name_difference() -> None:
    try:
        require_consensus(
            result("Stellar Pilot01", 49_588),
            result("Stellar Pixot01", 49_588),
        )
    except OcrError as error:
        assert "disagree" in str(error)
    else:
        raise AssertionError("unrelated name changes must not be accepted")


def test_consensus_rejects_confusable_name_when_score_changes() -> None:
    try:
        require_consensus(
            result("Stellar Pilot01", 49_588),
            result("Stellar PilotO0l", 49_589),
        )
    except OcrError as error:
        assert "disagree" in str(error)
    else:
        raise AssertionError("score changes must remain strict")


def test_consensus_accepts_borderline_confidence_for_a_known_pilot() -> None:
    agreed = require_consensus(
        result_with_confidence("Stellar PilotOl", 52_000, 72.0),
        result_with_confidence("Stellar PilotO1", 52_000, 37.0),
        minimum_confidence=40.0,
        known_entries=(RankingEntry(7, "Stellar Pilot01", 49_588, 90.0),),
    )

    assert agreed.entries == (RankingEntry(1, "Stellar Pilot01", 52_000, 72.0),)


def test_consensus_rejects_borderline_confidence_for_an_unknown_pilot() -> None:
    try:
        require_consensus(
            result_with_confidence("New Pilot", 52_000, 72.0),
            result_with_confidence("New Pilot", 52_000, 37.0),
            minimum_confidence=40.0,
            known_entries=(RankingEntry(7, "Known Pilot", 49_588, 90.0),),
        )
    except OcrError as error:
        assert "does not uniquely match" in str(error)
    else:
        raise AssertionError("unknown low-confidence names must not be accepted")


def test_consensus_rejects_known_pilot_below_the_soft_confidence_floor() -> None:
    try:
        require_consensus(
            result_with_confidence("Known Pilot", 52_000, 72.0),
            result_with_confidence("Known Pilot", 52_000, 34.9),
            minimum_confidence=40.0,
            known_entries=(RankingEntry(7, "Known Pilot", 49_588, 90.0),),
        )
    except OcrError as error:
        assert "known-name floor 35.0" in str(error)
    else:
        raise AssertionError("names below the soft confidence floor must not be accepted")


def test_consensus_rejects_borderline_confidence_when_the_score_decreases() -> None:
    try:
        require_consensus(
            result_with_confidence("Known Pilot", 49_000, 72.0),
            result_with_confidence("Known Pilot", 49_000, 37.0),
            minimum_confidence=40.0,
            known_entries=(RankingEntry(7, "Known Pilot", 49_588, 90.0),),
        )
    except OcrError as error:
        assert "decreasing score" in str(error)
    else:
        raise AssertionError("borderline reads must not bridge a score reset")


def test_rows_from_data_maps_tokens_by_vertical_center_and_horizontal_order() -> None:
    data = {
        "text": ["Beta", "Pilot", "Pilot Gamma"],
        "conf": ["90", "80", "95"],
        "left": [60, 10, 12],
        "top": [2, 3, 36],
        "height": [20, 20, 20],
    }

    rows = _rows_from_data(data, row_count=2, image_height=60)

    assert rows[0].text == "Pilot Beta"
    assert rows[0].confidence == 85
    assert rows[1].text == "Pilot Gamma"


def test_rows_from_data_preserves_an_empty_row() -> None:
    data = {
        "text": ["Pilot 1", "Pilot 3"],
        "conf": ["90", "90"],
        "left": [0, 0],
        "top": [1, 41],
        "height": [10, 10],
    }

    rows = _rows_from_data(data, row_count=3, image_height=60)

    assert [row.text for row in rows] == ["Pilot 1", "", "Pilot 3"]
