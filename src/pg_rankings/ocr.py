from __future__ import annotations

import logging
import os
import re
import subprocess
import sys
import unicodedata
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np
import pytesseract
from PIL import Image
from pytesseract import Output

from .config import OcrConfig, Rect
from .models import RankingEntry, canonical_pilot_name, pilot_key

_DEFAULT_TESSERACT_SUBPROCESS_ARGS = pytesseract.pytesseract.subprocess_args
LOGGER = logging.getLogger(__name__)
_VISUAL_NAME_TRANSLATION = str.maketrans({"0": "o", "1": "l", "i": "l", "j": "l"})
_DUPLICABLE_VISUAL_GLYPHS = frozenset({"o", "l"})
_MIN_DUPLICATION_MATCH_LENGTH = 8
KNOWN_NAME_MINIMUM_CONFIDENCE = 35.0


class OcrError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class OcrResult:
    entries: tuple[RankingEntry, ...]
    anchor_confidence: float
    anchor_text: str


@dataclass(frozen=True, slots=True)
class LineResult:
    text: str
    confidence: float


def hidden_tesseract_subprocess_args(include_stdout: bool = True) -> dict[str, object]:
    kwargs = _DEFAULT_TESSERACT_SUBPROCESS_ARGS(include_stdout)
    if sys.platform == "win32":
        # Windows 11 can delegate console programs to Windows Terminal even
        # when STARTUPINFO requests SW_HIDE. CREATE_NO_WINDOW prevents both
        # the visible terminal and the resulting ConPTY startup hang.
        no_window = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
        kwargs["creationflags"] = int(kwargs.get("creationflags", 0)) | no_window
    return kwargs


def configure_tesseract() -> None:
    command = os.environ.get("TESSERACT_CMD", "").strip()
    if not command and sys.platform == "win32":
        candidates = (
            Path(os.environ.get("PROGRAMFILES", "")) / "Tesseract-OCR" / "tesseract.exe",
            Path(os.environ.get("PROGRAMFILES(X86)", "")) / "Tesseract-OCR" / "tesseract.exe",
        )
        command = str(next((candidate for candidate in candidates if candidate.is_file()), ""))
    if command:
        pytesseract.pytesseract.tesseract_cmd = command
    pytesseract.pytesseract.subprocess_args = hidden_tesseract_subprocess_args


def _pixels(value: float, extent: int) -> int:
    return round(value * extent)


def crop_normalized(image: Image.Image, rect: Rect) -> Image.Image:
    left = _pixels(rect.x, image.width)
    top = _pixels(rect.y, image.height)
    right = _pixels(rect.x + rect.width, image.width)
    bottom = _pixels(rect.y + rect.height, image.height)
    return image.crop((left, top, right, bottom))


def crop_row(
    image: Image.Image,
    *,
    x: float,
    width: float,
    y: float,
    height: float,
) -> Image.Image:
    return crop_normalized(image, Rect(x=x, y=y, width=width, height=height))


def _variants(image: Image.Image, *, numeric: bool) -> tuple[np.ndarray, ...]:
    rgb = np.asarray(image.convert("RGB"))
    gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
    scale = 3 if numeric else 2
    gray = cv2.resize(gray, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC)
    contrast = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8)).apply(gray)
    _, otsu = cv2.threshold(contrast, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    return contrast, otsu


def _read_variant(image: np.ndarray, *, language: str, numeric: bool) -> LineResult:
    config = "--oem 3 --psm 7"
    if numeric:
        config += " -c tessedit_char_whitelist=0123456789,. "
    data = pytesseract.image_to_data(
        image,
        lang=language,
        config=config,
        output_type=Output.DICT,
    )
    tokens: list[str] = []
    confidences: list[float] = []
    for text, raw_confidence in zip(data["text"], data["conf"], strict=True):
        value = str(text).strip()
        try:
            confidence = float(raw_confidence)
        except (TypeError, ValueError):
            continue
        if value and confidence >= 0:
            tokens.append(value)
            confidences.append(confidence)
    if not tokens:
        return LineResult("", 0.0)
    return LineResult(" ".join(tokens), sum(confidences) / len(confidences))


def read_line(image: Image.Image, *, language: str, numeric: bool = False) -> LineResult:
    results = (
        _read_variant(variant, language=language, numeric=numeric)
        for variant in _variants(image, numeric=numeric)
    )
    return max(results, key=lambda result: result.confidence)


def _rows_from_data(
    data: dict[str, list[object]], *, row_count: int, image_height: int
) -> tuple[LineResult, ...]:
    tokens: list[list[tuple[int, str, float]]] = [[] for _ in range(row_count)]
    for text, raw_confidence, left, top, height in zip(
        data["text"],
        data["conf"],
        data["left"],
        data["top"],
        data["height"],
        strict=True,
    ):
        value = str(text).strip()
        try:
            confidence = float(raw_confidence)
            center = float(top) + float(height) / 2
            horizontal_position = int(left)
        except (TypeError, ValueError):
            continue
        if not value or confidence < 0:
            continue
        row = int(center * row_count / image_height)
        if 0 <= row < row_count:
            tokens[row].append((horizontal_position, value, confidence))

    results: list[LineResult] = []
    for row_tokens in tokens:
        row_tokens.sort(key=lambda token: token[0])
        if not row_tokens:
            results.append(LineResult("", 0.0))
            continue
        results.append(
            LineResult(
                " ".join(token[1] for token in row_tokens),
                sum(token[2] for token in row_tokens) / len(row_tokens),
            )
        )
    return tuple(results)


def _read_rows_variant(
    image: np.ndarray,
    *,
    language: str,
    numeric: bool,
    row_count: int,
) -> tuple[LineResult, ...]:
    config = "--oem 3 --psm 6"
    if numeric:
        config += " -c tessedit_char_whitelist=0123456789,. "
    data = pytesseract.image_to_data(
        image,
        lang=language,
        config=config,
        output_type=Output.DICT,
    )
    return _rows_from_data(data, row_count=row_count, image_height=image.shape[0])


def read_rows(
    image: Image.Image,
    *,
    language: str,
    numeric: bool,
    row_count: int,
) -> tuple[LineResult, ...]:
    variants = tuple(
        _read_rows_variant(
            variant,
            language=language,
            numeric=numeric,
            row_count=row_count,
        )
        for variant in _variants(image, numeric=numeric)
    )
    return tuple(
        max(
            (variant[index] for variant in variants),
            key=lambda result: (result.confidence, len(result.text)),
        )
        for index in range(row_count)
    )


def clean_name(
    value: str,
    *,
    name_aliases: tuple[tuple[str, str], ...] = (),
) -> str:
    value = unicodedata.normalize("NFKC", value)
    value = re.sub(r"\s+", " ", value).strip(" |[]")
    value = re.sub(r"^[^\w.('-]+|[^\w.)'-]+$", "", value, flags=re.UNICODE)
    return canonical_pilot_name(value.strip(), name_aliases)


def parse_score(value: str) -> int:
    value = value.strip()
    if not re.fullmatch(r"[0-9][0-9,.\s]*", value):
        raise OcrError(f"score contains unexpected characters: {value!r}")
    digits = re.sub(r"\D", "", value)
    if not digits:
        raise OcrError(f"score contains no digits: {value!r}")
    if len(digits) > 12:
        raise OcrError(f"score has an implausible number of digits: {value!r}")
    return int(digits)


def row_contains_single_zero_glyph(image: Image.Image) -> bool:
    rgb = np.asarray(image.convert("RGB"))
    left = round(image.width * 0.15)
    right = round(image.width * 0.94)
    region = rgb[:, left:right]
    low = region.min(axis=2)
    high = region.max(axis=2)
    mask = ((low >= 150) & ((high - low) <= 45)).astype(np.uint8)
    _count, _labels, stats, _centroids = cv2.connectedComponentsWithStats(mask, 8)
    components = [component for component in stats[1:] if int(component[4]) >= 2]
    if len(components) != 1:
        return False

    x, y, width, height, area = (int(value) for value in components[0])
    if not (5 <= width <= 10 and 7 <= height <= 13 and 0.35 <= area / (width * height) <= 0.75):
        return False
    glyph = mask[y : y + height, x : x + width]
    center = glyph[height // 3 : (2 * height + 2) // 3, width // 3 : (2 * width + 2) // 3]
    # At 800px the antialiased zero can be only five pixels wide, leaving one
    # lit edge column inside the integer-rounded center slice (mean 1/3).
    # The surrounding single-component, size, density, and four-edge checks
    # still keep this fallback specific to one zero glyph.
    if center.size == 0 or float(center.mean()) > 0.35:
        return False
    return bool(glyph[:2].any() and glyph[-2:].any() and glyph[:, :2].any() and glyph[:, -2:].any())


def read_score_with_zero_fallback(image: Image.Image, *, column_result: LineResult) -> int:
    """Parse column OCR, accepting only a visually verified lone zero when it misses a row."""
    try:
        return parse_score(column_result.text)
    except OcrError as column_error:
        if row_contains_single_zero_glyph(image):
            return 0
        raise column_error from None


def _validate_entries(entries: tuple[RankingEntry, ...], *, minimum_confidence: float) -> None:
    if any(entry.confidence < minimum_confidence for entry in entries):
        low = min(entry.confidence for entry in entries)
        raise OcrError(f"name OCR confidence {low:.1f} is below {minimum_confidence:.1f}")
    if any(len(entry.name) < 2 or len(entry.name) > 40 for entry in entries):
        raise OcrError("one or more pilot names have an implausible length")
    normalized_names = [normalize_name(entry.name) for entry in entries]
    if len(set(normalized_names)) != len(normalized_names):
        raise OcrError("duplicate pilot names detected")
    scores = [entry.score for entry in entries]
    if any(left < right for left, right in zip(scores, scores[1:], strict=False)):
        raise OcrError("scores are not sorted in descending order")


def normalize_name(value: str) -> str:
    return pilot_key(value)


def visual_name_key(value: str) -> str:
    """Normalize glyphs that Pirate Galaxy's font makes indistinguishable to OCR."""
    return normalize_name(value).translate(_VISUAL_NAME_TRANSLATION)


def _is_single_confusable_duplication(shorter: str, longer: str) -> bool:
    if len(shorter) < _MIN_DUPLICATION_MATCH_LENGTH or len(longer) != len(shorter) + 1:
        return False
    for index, character in enumerate(longer):
        if character not in _DUPLICABLE_VISUAL_GLYPHS:
            continue
        duplicate_neighbor = (index > 0 and longer[index - 1] == character) or (
            index + 1 < len(longer) and longer[index + 1] == character
        )
        if duplicate_neighbor and longer[:index] + longer[index + 1 :] == shorter:
            return True
    return False


def _is_single_l_tg_confusion(shorter: str, longer: str) -> bool:
    """Accept the observed compact-font OCR split of one ``l`` into ``tg``."""
    if len(shorter) < _MIN_DUPLICATION_MATCH_LENGTH or len(longer) != len(shorter) + 1:
        return False
    return any(
        character == "l" and shorter[:index] + "tg" + shorter[index + 1 :] == longer
        for index, character in enumerate(shorter)
    )


def names_agree_for_consensus(first: str, second: str) -> bool:
    first_normalized = normalize_name(first)
    second_normalized = normalize_name(second)
    if first_normalized == second_normalized:
        return True

    first_visual = visual_name_key(first)
    second_visual = visual_name_key(second)
    if first_visual == second_visual:
        return True
    shorter, longer = sorted((first_visual, second_visual), key=len)
    return _is_single_confusable_duplication(shorter, longer) or _is_single_l_tg_confusion(
        shorter, longer
    )


def _name_matches_known_history(read: str, known: str) -> bool:
    if names_agree_for_consensus(read, known):
        return True
    read_normalized = normalize_name(read)
    known_normalized = normalize_name(known)
    # In the compact font Tesseract can read a final two-digit suffix as "ss"
    # in two independent captures. Keep this exception restricted to
    # an otherwise exact, previously verified name with a two-digit suffix.
    return (
        len(read_normalized) >= 5
        and len(read_normalized) == len(known_normalized)
        and read_normalized[:-2] == known_normalized[:-2]
        and read_normalized[-2:] == "ss"
        and known_normalized[-2:].isdigit()
    )


def _preferred_consensus_entry(
    first: RankingEntry,
    second: RankingEntry,
) -> RankingEntry:
    if normalize_name(first.name) == normalize_name(second.name):
        return second
    # Inserted O/0 and l/1 glyphs are the observed Tesseract failure mode.
    # Prefer the shorter reading, then confidence, while the publisher keeps a
    # previously confirmed spelling stable across the ranking week.
    return min(
        (first, second),
        key=lambda entry: (
            len(visual_name_key(entry.name)),
            -entry.confidence,
        ),
    )


def extract_rankings(
    image: Image.Image,
    *,
    config: OcrConfig,
    top_n: int,
    diagnostics_directory: Path | None = None,
    minimum_confidence: float | None = None,
) -> OcrResult:
    configure_tesseract()
    anchor_crop = crop_normalized(image, config.anchor)
    anchor = read_line(anchor_crop, language=config.language)
    anchor_normalized = re.sub(r"\s+", "", anchor.text.casefold())
    if "division" not in anchor_normalized or "1" not in anchor_normalized:
        raise OcrError(f"Division 1 anchor not found (read {anchor.text!r})")
    if anchor.confidence < config.anchor_minimum_confidence:
        raise OcrError(
            f"anchor OCR confidence {anchor.confidence:.1f} is below "
            f"{config.anchor_minimum_confidence:.1f}"
        )

    if diagnostics_directory is not None:
        diagnostics_directory.mkdir(parents=True, exist_ok=True)
        anchor_crop.save(diagnostics_directory / "anchor.png")

    rows_height = (top_n - 1) * config.row_height + config.row_content_height
    names_crop = crop_row(
        image,
        x=config.name_x,
        width=config.name_width,
        y=config.first_row_y,
        height=rows_height,
    )
    scores_crop = crop_row(
        image,
        x=config.score_x,
        width=config.score_width,
        y=config.first_row_y,
        height=rows_height,
    )
    name_results = read_rows(
        names_crop,
        language=config.language,
        numeric=False,
        row_count=top_n,
    )
    score_results = read_rows(
        scores_crop,
        language=config.language,
        numeric=True,
        row_count=top_n,
    )
    if diagnostics_directory is not None:
        names_crop.save(diagnostics_directory / "names-column.png")
        scores_crop.save(diagnostics_directory / "scores-column.png")

    entries: list[RankingEntry] = []
    for index in range(top_n):
        y = config.first_row_y + index * config.row_height
        name_crop = crop_row(
            image,
            x=config.name_x,
            width=config.name_width,
            y=y,
            height=config.row_content_height,
        )
        score_crop = crop_row(
            image,
            x=config.score_x,
            width=config.score_width,
            y=y,
            height=config.row_content_height,
        )
        if diagnostics_directory is not None:
            name_crop.save(diagnostics_directory / f"row-{index + 1:02d}-name.png")
            score_crop.save(diagnostics_directory / f"row-{index + 1:02d}-score.png")

        name_result = name_results[index]
        score_result = score_results[index]
        if not name_result.text or name_result.confidence < config.minimum_confidence:
            left_name_crop = crop_row(
                image,
                x=max(0.0, config.name_x - 0.008),
                width=config.name_width,
                y=y,
                height=config.row_content_height,
            )
            individual_names = (
                read_line(name_crop, language=config.language),
                read_line(left_name_crop, language=config.language),
            )
            name_result = max(
                (name_result, *individual_names),
                key=lambda result: (result.confidence, len(result.text)),
            )
        if not score_result.text:
            individual_score = read_line(
                score_crop,
                language=config.language,
                numeric=True,
            )
            score_result = max(
                (score_result, individual_score),
                key=lambda result: (result.confidence, len(result.text)),
            )
        name = clean_name(name_result.text, name_aliases=config.name_aliases)
        score = read_score_with_zero_fallback(
            score_crop,
            column_result=score_result,
        )
        entries.append(
            RankingEntry(
                rank=index + 1,
                name=name,
                score=score,
                # Tesseract commonly assigns zero confidence to exact numeric
                # reads when a digit whitelist is active. Scores are instead
                # protected by strict parsing, ordering, and two-capture consensus.
                confidence=name_result.confidence,
            )
        )

    result = tuple(entries)
    _validate_entries(
        result,
        minimum_confidence=(
            config.minimum_confidence if minimum_confidence is None else minimum_confidence
        ),
    )
    return OcrResult(result, anchor.confidence, anchor.text)


def _known_entry_for_borderline_read(
    first: RankingEntry,
    second: RankingEntry,
    known_entries: tuple[RankingEntry, ...],
) -> RankingEntry | None:
    matches = tuple(
        known
        for known in known_entries
        if _name_matches_known_history(first.name, known.name)
        and _name_matches_known_history(second.name, known.name)
    )
    return matches[0] if len(matches) == 1 else None


def require_consensus(
    first: OcrResult,
    second: OcrResult,
    *,
    minimum_confidence: float = 0.0,
    known_entries: tuple[RankingEntry, ...] = (),
) -> OcrResult:
    if len(first.entries) != len(second.entries):
        raise OcrError("the two OCR reads disagree; Discord was not updated")

    agreed_entries: list[RankingEntry] = []
    for first_entry, second_entry in zip(first.entries, second.entries, strict=True):
        if (
            first_entry.rank != second_entry.rank
            or first_entry.score != second_entry.score
            or not names_agree_for_consensus(first_entry.name, second_entry.name)
        ):
            raise OcrError("the two OCR reads disagree; Discord was not updated")
        agreed = _preferred_consensus_entry(first_entry, second_entry)
        lowest_confidence = min(first_entry.confidence, second_entry.confidence)
        if lowest_confidence < minimum_confidence:
            soft_minimum = min(minimum_confidence, KNOWN_NAME_MINIMUM_CONFIDENCE)
            if lowest_confidence < soft_minimum:
                raise OcrError(
                    f"name OCR confidence {lowest_confidence:.1f} is below "
                    f"the known-name floor {soft_minimum:.1f}"
                )
            known = _known_entry_for_borderline_read(
                first_entry,
                second_entry,
                known_entries,
            )
            if known is None:
                raise OcrError("borderline name OCR does not uniquely match the last valid Top 11")
            if agreed.score < known.score:
                raise OcrError("borderline name OCR has a decreasing score")
            LOGGER.info(
                "accepted known pilot at rank %d with borderline name confidence %.1f/%.1f: %r",
                agreed.rank,
                first_entry.confidence,
                second_entry.confidence,
                known.name,
            )
            agreed = RankingEntry(
                rank=agreed.rank,
                name=known.name,
                score=agreed.score,
                confidence=agreed.confidence,
            )
        else:
            known = _known_entry_for_borderline_read(
                first_entry,
                second_entry,
                known_entries,
            )
            if (
                known is not None
                and agreed.score >= known.score
                and normalize_name(agreed.name) != normalize_name(known.name)
            ):
                LOGGER.info(
                    "kept known spelling for visually equivalent pilot at rank %d: %r -> %r",
                    agreed.rank,
                    agreed.name,
                    known.name,
                )
                agreed = RankingEntry(
                    rank=agreed.rank,
                    name=known.name,
                    score=agreed.score,
                    confidence=agreed.confidence,
                )
        if normalize_name(first_entry.name) != normalize_name(second_entry.name):
            LOGGER.info(
                "accepted visually equivalent OCR name readings at rank %d: %r / %r",
                agreed.rank,
                first_entry.name,
                second_entry.name,
            )
        agreed_entries.append(agreed)

    visual_names = [visual_name_key(entry.name) for entry in agreed_entries]
    if len(set(visual_names)) != len(visual_names):
        raise OcrError("duplicate pilot names detected after OCR consensus")
    return OcrResult(tuple(agreed_entries), second.anchor_confidence, second.anchor_text)
