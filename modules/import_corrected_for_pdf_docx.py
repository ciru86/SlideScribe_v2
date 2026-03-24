#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List


@dataclass
class ParsedSlide:
    slide_index: int
    text: str


def eprint(*args, **kwargs) -> None:
    print(*args, file=sys.stderr, **kwargs)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Importa i chunk corretti per LLM, valida la struttura e ricostruisce "
            "un file unico con il testo finale per slide."
        )
    )
    parser.add_argument(
        "--input-dir",
        required=True,
        help="Cartella con i file .corrected.txt restituiti da ChatGPT.",
    )
    parser.add_argument(
        "--base-name",
        required=True,
        help="Base name del progetto/file, es. OSAS2.",
    )
    parser.add_argument(
        "--output-dir",
        required=True,
        help="Cartella dove scrivere gli output ricomposti.",
    )
    parser.add_argument(
        "--expected-slides",
        type=int,
        required=True,
        help="Numero totale atteso di slide, es. 150.",
    )
    parser.add_argument(
        "--glob",
        default="*.corrected.txt",
        help="Pattern glob dei file corretti. Default: *.corrected.txt",
    )
    return parser.parse_args()


def normalize_newlines(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


def extract_chunk_number(path: Path) -> int:
    m = re.search(r"chunk_(\d{3})", path.name)
    if not m:
        raise ValueError(f"Impossibile estrarre chunk number da: {path.name}")
    return int(m.group(1))


def parse_corrected_chunk(path: Path) -> List[ParsedSlide]:
    raw = path.read_text(encoding="utf-8-sig", errors="replace")
    raw = normalize_newlines(raw)

    begin_chunk_re = re.compile(r"^===== BEGIN CHUNK (\d{3}) =====\s*$", re.MULTILINE)
    end_chunk_re = re.compile(r"^===== END CHUNK (\d{3}) =====\s*$", re.MULTILINE)

    if not begin_chunk_re.search(raw):
        raise ValueError(f"BEGIN CHUNK mancante in {path}")
    if not end_chunk_re.search(raw):
        raise ValueError(f"END CHUNK mancante in {path}")

    slide_pattern = re.compile(
        r"^----- BEGIN SLIDE (\d{4}) -----\n"
        r"TEXT:\n"
        r"(.*?)"
        r"\n----- END SLIDE \1 -----\s*$",
        re.MULTILINE | re.DOTALL,
    )

    slides: List[ParsedSlide] = []

    for m in slide_pattern.finditer(raw):
        slide_index = int(m.group(1))
        text = m.group(2)

        # pulizia molto conservativa:
        # - togli newline iniziali/finali spurie
        # - preserva il testo interno
        text = text.strip("\n")
        text = text.strip()

        slides.append(ParsedSlide(slide_index=slide_index, text=text))

    if not slides:
        raise ValueError(f"Nessuna slide parsata in {path}")

    # validazione ordine e duplicati interni al file
    seen = set()
    prev = None
    for slide in slides:
        if slide.slide_index in seen:
            raise ValueError(
                f"Slide duplicata dentro lo stesso file {path.name}: {slide.slide_index}"
            )
        seen.add(slide.slide_index)

        if prev is not None and slide.slide_index <= prev:
            raise ValueError(
                f"Ordine slide non crescente in {path.name}: "
                f"{prev} -> {slide.slide_index}"
            )
        prev = slide.slide_index

    return slides


def write_debug_txt(path: Path, slide_map: Dict[int, str]) -> None:
    lines: List[str] = []

    for slide_index in sorted(slide_map):
        lines.append(f"----- BEGIN SLIDE {slide_index:04d} -----")
        lines.append("TEXT:")
        if slide_map[slide_index]:
            lines.append(slide_map[slide_index])
        lines.append("")
        lines.append(f"----- END SLIDE {slide_index:04d} -----")
        lines.append("")

    path.write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    args = parse_args()

    input_dir = Path(args.input_dir)
    output_dir = Path(args.output_dir)
    base_name = args.base_name
    expected_slides = args.expected_slides
    glob_pattern = args.glob

    if expected_slides <= 0:
        eprint("Errore: --expected-slides deve essere > 0")
        return 1

    if not input_dir.is_dir():
        eprint(f"Errore: cartella input non trovata: {input_dir}")
        return 1

    output_dir.mkdir(parents=True, exist_ok=True)

    files = sorted(
        input_dir.glob(glob_pattern),
        key=extract_chunk_number,
    )

    if not files:
        eprint(f"Errore: nessun file trovato in {input_dir} con glob {glob_pattern!r}")
        return 1

    eprint(f"[INFO] File corretti trovati: {len(files)}")

    slide_map: Dict[int, str] = {}

    for file_path in files:
        eprint(f"[INFO] Parsing: {file_path.name}")
        slides = parse_corrected_chunk(file_path)

        for slide in slides:
            if slide.slide_index in slide_map:
                eprint(
                    f"Errore: slide {slide.slide_index:04d} duplicata "
                    f"tra più chunk (file: {file_path.name})"
                )
                return 1

            slide_map[slide.slide_index] = slide.text

    # validazione completezza
    expected_set = set(range(1, expected_slides + 1))
    found_set = set(slide_map.keys())

    missing = sorted(expected_set - found_set)
    extra = sorted(found_set - expected_set)

    if missing:
        eprint(
            "[ERRORE] Mancano slide attese: "
            + ", ".join(f"{x:04d}" for x in missing[:20])
            + (" ..." if len(missing) > 20 else "")
        )
        return 1

    if extra:
        eprint(
            "[ERRORE] Slide fuori range atteso: "
            + ", ".join(f"{x:04d}" for x in extra[:20])
            + (" ..." if len(extra) > 20 else "")
        )
        return 1

    # output JSON per uso macchina
    json_payload = {
        "base_name": base_name,
        "total_slides": expected_slides,
        "slides": [
            {
                "slide_index": idx,
                "text": slide_map[idx],
            }
            for idx in range(1, expected_slides + 1)
        ],
    }

    json_path = output_dir / f"{base_name}.slide_texts.json"
    json_path.write_text(
        json.dumps(json_payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    # output txt per debug umano
    txt_path = output_dir / f"{base_name}.slide_texts.txt"
    write_debug_txt(txt_path, slide_map)

    eprint(f"[OK] Scritto JSON: {json_path}")
    eprint(f"[OK] Scritto TXT:  {txt_path}")
    eprint("[DONE] Import e ricomposizione completati.")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())