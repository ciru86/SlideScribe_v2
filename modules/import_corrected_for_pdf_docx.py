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

# ============================================================
# DATA STRUCTURES
# ============================================================

@dataclass
class ParsedSlide:
    # Rappresenta una singola slide estratta da un file chunk corretto.
    #
    # slide_index:
    #   numero progressivo della slide, es. 1, 2, 3...
    #
    # text:
    #   testo finale corretto associato a quella slide.
    slide_index: int
    text: str


# ============================================================
# LOGGING / STDERR HELPERS
# ============================================================

def eprint(*args, **kwargs) -> None:
    # Stampa su stderr invece che su stdout.
    #
    # Utile per:
    # - messaggi informativi di processo
    # - warning / errori
    # - tenere separato il logging dall'output macchina eventualmente
    #   redirezionato su stdout in altri contesti
    print(*args, file=sys.stderr, **kwargs)


# ============================================================
# ARGUMENT PARSING
# ============================================================

def parse_args() -> argparse.Namespace:
    # Definisce e valida gli argomenti da riga di comando.
    #
    # Questo script si aspetta:
    # - una cartella input contenente i file *.corrected.txt
    # - un base name per nominare gli output finali
    # - una cartella output
    # - il numero totale atteso di slide
    # - opzionalmente un glob custom per trovare i file corretti
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


# ============================================================
# TEXT / NORMALIZATION HELPERS
# ============================================================

def normalize_newlines(text: str) -> str:
    # Normalizza gli a-capo in formato Unix (\n).
    #
    # Perché serve:
    # - alcuni file possono arrivare con newline Windows (\r\n)
    # - altri con vecchi newline Mac (\r)
    # - le regex sotto assumono una struttura coerente
    #
    # Se non uniformi prima i newline, parsing e matching possono rompersi.
    return text.replace("\r\n", "\n").replace("\r", "\n")


def extract_chunk_number(path: Path) -> int:
    # Estrae il numero chunk dal nome file.
    #
    # Esempio atteso:
    #   qualcosa_chunk_003.corrected.txt
    #
    # Regex:
    #   chunk_(\d{3})
    #
    # Restituisce l'intero, es. 3.
    #
    # Questo serve soprattutto per ordinare i file in modo sensato prima
    # del parsing, invece di affidarsi al puro ordine alfabetico generico.
    m = re.search(r"chunk_(\d{3})", path.name)
    if not m:
        raise ValueError(f"Impossibile estrarre chunk number da: {path.name}")
    return int(m.group(1))


# ============================================================
# CHUNK PARSING
# ============================================================

def parse_corrected_chunk(path: Path) -> List[ParsedSlide]:
    # Legge un singolo file .corrected.txt e ne estrae le slide strutturate.
    #
    # Il file deve avere una struttura molto precisa, ad esempio:
    #
    # ===== BEGIN CHUNK 001 =====
    # ----- BEGIN SLIDE 0001 -----
    # TEXT:
    # contenuto slide
    # ----- END SLIDE 0001 -----
    # ...
    # ===== END CHUNK 001 =====
    #
    # Il parser:
    # 1) legge il file
    # 2) normalizza i newline
    # 3) verifica che ci siano marker BEGIN/END CHUNK
    # 4) estrae tutte le slide con regex
    # 5) pulisce in modo conservativo il testo
    # 6) valida duplicati e ordine interno
    raw = path.read_text(encoding="utf-8-sig", errors="replace")
    raw = normalize_newlines(raw)

    # Regex per validare la presenza del contenitore chunk.
    #
    # MULTILINE serve per far sì che ^ e $ lavorino riga per riga.
    begin_chunk_re = re.compile(r"^===== BEGIN CHUNK (\d{3}) =====\s*$", re.MULTILINE)
    end_chunk_re = re.compile(r"^===== END CHUNK (\d{3}) =====\s*$", re.MULTILINE)

    if not begin_chunk_re.search(raw):
        raise ValueError(f"BEGIN CHUNK mancante in {path}")

    if not end_chunk_re.search(raw):
        raise ValueError(f"END CHUNK mancante in {path}")

    # Regex principale per estrarre le slide.
    #
    # Struttura attesa:
    # ----- BEGIN SLIDE 0001 -----
    # TEXT:
    # ...
    # ----- END SLIDE 0001 -----
    #
    # Note:
    # - (\d{4}) cattura l'indice slide a 4 cifre
    # - (.*?) cattura il contenuto della slide in modo non greedy
    # - \1 nel marker END impone che il numero finale coincida con quello iniziale
    # - DOTALL permette a "." di includere anche newline
    # - MULTILINE permette a ^ e $ di lavorare per linea
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

        # Pulizia conservativa:
        # - rimuove newline spurie all'inizio/fine
        # - rimuove spazi esterni residui
        # - non altera volutamente il contenuto interno
        #
        # È una scelta prudente:
        # vogliamo evitare di "normalizzare troppo" e rischiare di cambiare
        # il testo corretto restituito dal modello.
        text = text.strip("\n")
        text = text.strip()

        slides.append(ParsedSlide(slide_index=slide_index, text=text))

    if not slides:
        raise ValueError(f"Nessuna slide parsata in {path}")

    # --------------------------------------------------------
    # VALIDAZIONE INTERNA DEL FILE
    # --------------------------------------------------------
    #
    # Controlli eseguiti:
    # 1) nessuna slide duplicata nello stesso chunk
    # 2) numerazione strettamente crescente all'interno del file
    #
    # Questo intercetta:
    # - output corrotti
    # - slide ripetute
    # - blocchi fuori ordine
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


# ============================================================
# DEBUG / HUMAN-READABLE OUTPUT
# ============================================================

def write_debug_txt(path: Path, slide_map: Dict[int, str]) -> None:
    # Scrive un file TXT leggibile da umano, ricostruendo tutte le slide
    # nel formato:
    #
    # ----- BEGIN SLIDE 0001 -----
    # TEXT:
    # ...
    # ----- END SLIDE 0001 -----
    #
    # Questo output serve soprattutto per:
    # - debug manuale
    # - ispezione rapida del testo ricomposto
    # - confronto con input / chunk originali
    lines: List[str] = []

    for slide_index in sorted(slide_map):
        lines.append(f"----- BEGIN SLIDE {slide_index:04d} -----")
        lines.append("TEXT:")

        # Se il testo non è vuoto, lo scrive.
        # Se invece è vuoto, lascia comunque il blocco formalmente valido.
        if slide_map[slide_index]:
            lines.append(slide_map[slide_index])

        lines.append("")
        lines.append(f"----- END SLIDE {slide_index:04d} -----")
        lines.append("")

    path.write_text("\n".join(lines), encoding="utf-8")


# ============================================================
# MAIN
# ============================================================

def main() -> int:
    # Funzione principale:
    #
    # Flusso:
    # 1) legge argomenti
    # 2) valida input base
    # 3) trova i file corretti
    # 4) parsifica ogni chunk
    # 5) ricompone tutte le slide in una mappa unica
    # 6) valida completezza e range
    # 7) scrive output JSON e TXT
    # 8) restituisce exit code 0 se tutto OK
    args = parse_args()

    input_dir = Path(args.input_dir)
    output_dir = Path(args.output_dir)
    base_name = args.base_name
    expected_slides = args.expected_slides
    glob_pattern = args.glob

    # Validazione basilare del numero atteso di slide.
    if expected_slides <= 0:
        eprint("Errore: --expected-slides deve essere > 0")
        return 1

    # Verifica che la cartella input esista davvero.
    if not input_dir.is_dir():
        eprint(f"Errore: cartella input non trovata: {input_dir}")
        return 1

    # Crea la cartella output se non esiste.
    output_dir.mkdir(parents=True, exist_ok=True)

    # Recupera tutti i file che matchano il glob richiesto
    # e li ordina usando il numero chunk estratto dal filename.
    #
    # Questo evita problemi del tipo:
    # chunk_010 prima di chunk_002 per ordinamento testuale sbagliato,
    # se i nomi non sono consistenti o se in futuro cambiassi formato.
    files = sorted(
        input_dir.glob(glob_pattern),
        key=extract_chunk_number,
    )

    if not files:
        eprint(f"Errore: nessun file trovato in {input_dir} con glob {glob_pattern!r}")
        return 1

    eprint(f"[INFO] File corretti trovati: {len(files)}")

    # Mappa finale:
    #   chiave   = slide_index
    #   valore   = testo corretto della slide
    #
    # Serve a:
    # - ricostruire l'intero deck
    # - rilevare duplicati tra chunk diversi
    slide_map: Dict[int, str] = {}

    for file_path in files:
        eprint(f"[INFO] Parsing: {file_path.name}")
        slides = parse_corrected_chunk(file_path)

        for slide in slides:
            # Se una slide compare in più chunk è un errore strutturale:
            # non puoi sapere quale versione tenere in modo sicuro.
            if slide.slide_index in slide_map:
                eprint(
                    f"Errore: slide {slide.slide_index:04d} duplicata "
                    f"tra più chunk (file: {file_path.name})"
                )
                return 1

            slide_map[slide.slide_index] = slide.text

    # --------------------------------------------------------
    # VALIDAZIONE COMPLETEZZA GLOBALE
    # --------------------------------------------------------
    #
    # Costruisce:
    # - insieme atteso: da 1 a expected_slides
    # - insieme trovato: quello realmente presente nei chunk
    #
    # Da qui ricava:
    # - missing: slide mancanti
    # - extra: slide fuori range atteso
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

    # --------------------------------------------------------
    # OUTPUT JSON
    # --------------------------------------------------------
    #
    # Formato pensato per uso macchina / pipeline successive.
    #
    # Struttura:
    # {
    #   "base_name": "...",
    #   "total_slides": N,
    #   "slides": [
    #     {"slide_index": 1, "text": "..."},
    #     ...
    #   ]
    # }
    #
    # Nota:
    # iteriamo da 1 a expected_slides per garantire ordine completo.
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

    # --------------------------------------------------------
    # OUTPUT TXT
    # --------------------------------------------------------
    #
    # File leggibile per controllo umano / debug.
    txt_path = output_dir / f"{base_name}.slide_texts.txt"
    write_debug_txt(txt_path, slide_map)

    eprint(f"[OK] Scritto JSON: {json_path}")
    eprint(f"[OK] Scritto TXT:  {txt_path}")
    eprint("[DONE] Import e ricomposizione completati.")

    return 0


# ============================================================
# ENTRY POINT
# ============================================================

if __name__ == "__main__":
    # Esegue main() e usa il suo valore come exit code del processo.
    raise SystemExit(main())