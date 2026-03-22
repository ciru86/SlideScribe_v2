#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from __future__ import annotations

import argparse
import csv
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional


@dataclass
class Slide:
    slide_index: int
    timestamp_sec: float
    timestamp_hms: str = ""
    filename: str = ""


@dataclass
class SRTBlock:
    index: int
    start_sec: float
    end_sec: float
    text: str

    @property
    def midpoint_sec(self) -> float:
        return (self.start_sec + self.end_sec) / 2.0


def eprint(*args, **kwargs) -> None:
    print(*args, file=sys.stderr, **kwargs)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Assegna il contenuto di un SRT alle slide in base ai timestamp di slides.csv "
            "ed esporta chunk testuali per LLM."
        )
    )
    parser.add_argument(
        "--srt",
        required=True,
        help="Percorso del file SRT sorgente (usa .original.srt nel nuovo flusso).",
    )
    parser.add_argument(
        "--slides-csv",
        required=True,
        help="Percorso di slides.csv generato da Screenshot_grabber.",
    )
    parser.add_argument(
        "--output-dir",
        required=True,
        help="Cartella dove scrivere i chunk per LLM.",
    )
    parser.add_argument(
        "--base-name",
        required=True,
        help="Base name del progetto/file, es. OSAS2.",
    )
    parser.add_argument(
        "--chunk-size",
        type=int,
        default=20,
        help="Numero di slide per chunk. Default: 20.",
    )
    parser.add_argument(
        "--empty-placeholder",
        default="",
        help="Testo da mettere nelle slide senza contenuto. Default: stringa vuota.",
    )
    return parser.parse_args()


def srt_timestamp_to_seconds(ts: str) -> float:
    m = re.match(r"^(\d{2}):(\d{2}):(\d{2}),(\d{3})$", ts.strip())
    if not m:
        raise ValueError(f"Timestamp SRT non valido: {ts!r}")
    hh, mm, ss, ms = map(int, m.groups())
    return hh * 3600 + mm * 60 + ss + ms / 1000.0


def normalize_whitespace(text: str) -> str:
    text = text.replace("\ufeff", "")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = [ln.strip() for ln in text.split("\n")]
    lines = [ln for ln in lines if ln]
    joined = " ".join(lines)
    joined = re.sub(r"\s+", " ", joined).strip()
    return joined


def parse_srt(path: Path) -> List[SRTBlock]:
    raw = path.read_text(encoding="utf-8-sig", errors="replace")
    raw = raw.replace("\r\n", "\n").replace("\r", "\n").strip()

    if not raw:
        raise ValueError(f"SRT vuoto: {path}")

    blocks_raw = re.split(r"\n\s*\n", raw)
    blocks: List[SRTBlock] = []

    # Parser più tollerante:
    # - accetta attributi extra dopo il timestamp finale
    # - salta blocchi malformati invece di morire subito
    ts_line_re = re.compile(
        r"^(\d{2}:\d{2}:\d{2},\d{3})\s+-->\s+(\d{2}:\d{2}:\d{2},\d{3})(?:\s+.*)?$"
    )

    skipped_blocks = 0

    for n, block_raw in enumerate(blocks_raw, start=1):
        lines = [ln.rstrip() for ln in block_raw.split("\n")]
        lines = [ln for ln in lines if ln.strip() != ""]

        if len(lines) < 2:
            skipped_blocks += 1
            continue

        idx: Optional[int] = None
        ts_line: Optional[str] = None
        text_lines: List[str] = []

        # Caso standard:
        # 1
        # 00:00:00,000 --> 00:00:01,000
        # testo...
        if lines[0].strip().isdigit():
            idx = int(lines[0].strip())
            if len(lines) >= 2 and ts_line_re.match(lines[1].strip()):
                ts_line = lines[1].strip()
                text_lines = lines[2:]
            else:
                skipped_blocks += 1
                continue

        # Caso non standard:
        # 00:00:00,000 --> 00:00:01,000
        # testo...
        elif ts_line_re.match(lines[0].strip()):
            idx = len(blocks) + 1
            ts_line = lines[0].strip()
            text_lines = lines[1:]

        else:
            skipped_blocks += 1
            continue

        m = ts_line_re.match(ts_line)
        if not m:
            skipped_blocks += 1
            continue

        start_sec = srt_timestamp_to_seconds(m.group(1))
        end_sec = srt_timestamp_to_seconds(m.group(2))
        text = normalize_whitespace("\n".join(text_lines))

        if not text:
            skipped_blocks += 1
            continue

        blocks.append(
            SRTBlock(
                index=idx,
                start_sec=start_sec,
                end_sec=end_sec,
                text=text,
            )
        )

    if not blocks:
        raise ValueError(f"Nessun blocco SRT valido trovato in {path}")

    eprint(f"[INFO] Blocchi SRT validi: {len(blocks)}")
    if skipped_blocks:
        eprint(f"[INFO] Blocchi SRT scartati: {skipped_blocks}")

    return blocks


def parse_slides_csv(path: Path) -> List[Slide]:
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        if reader.fieldnames is None:
            raise ValueError(f"CSV senza intestazione: {path}")

        normalized_fields = {name.strip().lower(): name for name in reader.fieldnames}

        def get_field(row: dict, *candidates: str, required: bool = True) -> str:
            for candidate in candidates:
                key = normalized_fields.get(candidate.lower())
                if key is not None:
                    value = row.get(key, "")
                    if value is None:
                        value = ""
                    value = str(value).strip()
                    if value != "" or not required:
                        return value
            if required:
                raise KeyError(
                    f"Campo richiesto non trovato tra: {', '.join(candidates)}"
                )
            return ""

        slides: List[Slide] = []

        for i, row in enumerate(reader, start=2):
            try:
                slide_index_str = get_field(row, "slide_index", "slide", "index")
                timestamp_sec_str = get_field(
                    row, "timestamp_sec", "time_sec", "seconds", "timestamp"
                )
                timestamp_hms = get_field(
                    row, "timestamp_hms", "time_hms", "hms", required=False
                )
                filename = get_field(
                    row, "filename", "file", "image", "slide_filename", required=False
                )
            except KeyError as exc:
                raise ValueError(f"Errore intestazione CSV {path}: {exc}") from exc

            try:
                slide_index = int(slide_index_str)
            except Exception as exc:
                raise ValueError(
                    f"slide_index non valido alla riga CSV {i}: {slide_index_str!r}"
                ) from exc

            try:
                timestamp_sec = float(timestamp_sec_str)
            except Exception as exc:
                raise ValueError(
                    f"timestamp_sec non valido alla riga CSV {i}: {timestamp_sec_str!r}"
                ) from exc

            slides.append(
                Slide(
                    slide_index=slide_index,
                    timestamp_sec=timestamp_sec,
                    timestamp_hms=timestamp_hms,
                    filename=filename,
                )
            )

    if not slides:
        raise ValueError(f"Nessuna slide trovata in {path}")

    slides.sort(key=lambda s: s.slide_index)
    return slides


def find_slide_for_time(midpoint_sec: float, slides: List[Slide]) -> Slide:
    for i in range(len(slides) - 1):
        cur = slides[i]
        nxt = slides[i + 1]
        if cur.timestamp_sec <= midpoint_sec < nxt.timestamp_sec:
            return cur
    return slides[-1]


def aggregate_text_by_slide(
    slides: List[Slide],
    srt_blocks: List[SRTBlock],
) -> dict[int, List[str]]:
    by_slide: dict[int, List[str]] = {s.slide_index: [] for s in slides}

    for block in srt_blocks:
        if not block.text:
            continue
        slide = find_slide_for_time(block.midpoint_sec, slides)
        by_slide[slide.slide_index].append(block.text)

    return by_slide


def dedupe_adjacent_lines(lines: List[str]) -> List[str]:
    out: List[str] = []
    prev: Optional[str] = None
    for line in lines:
        if line == prev:
            continue
        out.append(line)
        prev = line
    return out


def join_slide_text(lines: List[str], empty_placeholder: str = "") -> str:
    lines = dedupe_adjacent_lines(lines)
    lines = [normalize_whitespace(x) for x in lines]
    lines = [x for x in lines if x]

    if not lines:
        return empty_placeholder

    text = " ".join(lines)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def chunked(seq: List[Slide], size: int) -> List[List[Slide]]:
    return [seq[i:i + size] for i in range(0, len(seq), size)]


def write_chunk_file(
    path: Path,
    chunk_number: int,
    slides_chunk: List[Slide],
    slide_text_map: dict[int, str],
) -> None:
    lines: List[str] = []
    lines.append(f"===== BEGIN CHUNK {chunk_number:03d} =====")
    lines.append("")

    for slide in slides_chunk:
        idx = slide.slide_index
        text = slide_text_map.get(idx, "")

        lines.append(f"----- BEGIN SLIDE {idx:04d} -----")
        lines.append("TEXT:")
        if text:
            lines.append(text)
        lines.append("")
        lines.append(f"----- END SLIDE {idx:04d} -----")
        lines.append("")

    lines.append(f"===== END CHUNK {chunk_number:03d} =====")
    lines.append("")

    path.write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    args = parse_args()

    srt_path = Path(args.srt)
    slides_csv_path = Path(args.slides_csv)
    output_dir = Path(args.output_dir)
    base_name = args.base_name
    chunk_size = args.chunk_size
    empty_placeholder = args.empty_placeholder

    if chunk_size <= 0:
        eprint("Errore: --chunk-size deve essere > 0")
        return 1

    if not srt_path.is_file():
        eprint(f"Errore: SRT non trovato: {srt_path}")
        return 1

    if not slides_csv_path.is_file():
        eprint(f"Errore: slides.csv non trovato: {slides_csv_path}")
        return 1

    output_dir.mkdir(parents=True, exist_ok=True)

    eprint(f"[INFO] Leggo slides.csv: {slides_csv_path}")
    slides = parse_slides_csv(slides_csv_path)
    eprint(f"[INFO] Slide trovate: {len(slides)}")

    eprint(f"[INFO] Leggo SRT: {srt_path}")
    srt_blocks = parse_srt(srt_path)

    eprint("[INFO] Assegno i blocchi SRT alle slide...")
    by_slide_lines = aggregate_text_by_slide(slides, srt_blocks)

    slide_text_map: dict[int, str] = {}
    non_empty_slides = 0

    for slide in slides:
        text = join_slide_text(
            by_slide_lines.get(slide.slide_index, []),
            empty_placeholder=empty_placeholder,
        )
        slide_text_map[slide.slide_index] = text
        if text.strip():
            non_empty_slides += 1

    eprint(f"[INFO] Slide con testo non vuoto: {non_empty_slides}/{len(slides)}")

    slide_chunks = chunked(slides, chunk_size)
    eprint(f"[INFO] Chunk da scrivere: {len(slide_chunks)}")

    for chunk_number, slides_chunk in enumerate(slide_chunks, start=1):
        first_slide = slides_chunk[0].slide_index
        last_slide = slides_chunk[-1].slide_index

        filename = (
            f"{base_name}.chunk_{chunk_number:03d}_"
            f"slides_{first_slide:04d}_{last_slide:04d}.txt"
        )
        chunk_path = output_dir / filename

        write_chunk_file(
            path=chunk_path,
            chunk_number=chunk_number,
            slides_chunk=slides_chunk,
            slide_text_map=slide_text_map,
        )

        eprint(f"[OK] Scritto: {chunk_path}")

    eprint("[DONE] Export per LLM completato.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())