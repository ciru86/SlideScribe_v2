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


# ============================================================
# COSTANTI
# ============================================================

# Durata minima accettata per un blocco SRT.
#
# Serve a filtrare blocchi "spuri" o rumore, per esempio:
# - sottotitoli vuoti o quasi vuoti
# - flash brevissimi generati male
# - blocchi corrotti con durata praticamente nulla
MIN_BLOCK_DURATION_SEC = 0.15


# ============================================================
# DATA STRUCTURES
# ============================================================

@dataclass
class Slide:
    # Rappresenta una slide letta dal file slides.csv.
    #
    # slide_index:
    #   numero progressivo della slide, usato come identificatore logico.
    #
    # timestamp_sec:
    #   tempo in secondi in cui quella slide compare nel video.
    #   Serve per assegnare i blocchi SRT alla slide corretta.
    #
    # timestamp_hms:
    #   versione leggibile del timestamp, se presente nel CSV.
    #   Non è indispensabile al mapping, ma è utile come metadato.
    #
    # filename:
    #   nome del file immagine della slide, se presente nel CSV.
    #   Anche questo è metadato utile per debugging o workflow futuri.
    slide_index: int
    timestamp_sec: float
    timestamp_hms: str = ""
    filename: str = ""


@dataclass
class SRTBlock:
    # Rappresenta un singolo blocco di sottotitolo estratto dal file SRT.
    #
    # index:
    #   indice del blocco SRT. Se il file è standard viene preso dal blocco;
    #   se il file è non standard può essere generato dal parser.
    #
    # start_sec / end_sec:
    #   intervallo temporale del blocco espresso in secondi.
    #
    # text:
    #   contenuto testuale già normalizzato.
    index: int
    start_sec: float
    end_sec: float
    text: str

    @property
    def midpoint_sec(self) -> float:
        # Punto medio temporale del blocco SRT.
        #
        # In questa versione dello script non viene usato nel mapping,
        # ma può essere utile se in futuro volessi assegnare i blocchi
        # alla slide in base al centro del sottotitolo invece che all'inizio.
        return (self.start_sec + self.end_sec) / 2.0


# ============================================================
# STDERR / LOGGING
# ============================================================

def eprint(*args, **kwargs) -> None:
    # Stampa su stderr invece che su stdout.
    #
    # Questo è utile per separare:
    # - messaggi di log
    # - errori / warning
    # - output potenzialmente consumabili da altri tool
    print(*args, file=sys.stderr, **kwargs)


# ============================================================
# ARGUMENT PARSING
# ============================================================

def parse_args() -> argparse.Namespace:
    # Definisce tutti gli argomenti da riga di comando.
    #
    # Obiettivo dello script:
    # 1) leggere un file SRT
    # 2) leggere slides.csv con i timestamp delle slide
    # 3) assegnare i blocchi SRT alle slide in base al tempo
    # 4) pulire / deduplicare il testo
    # 5) esportare file chunkati da dare all'LLM
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


# ============================================================
# TIMESTAMP / NORMALIZATION HELPERS
# ============================================================

def srt_timestamp_to_seconds(ts: str) -> float:
    # Converte un timestamp SRT nel formato:
    #   HH:MM:SS,mmm
    # in secondi float.
    #
    # Esempio:
    #   00:01:23,450  ->  83.45
    #
    # Se il formato non è valido, alza ValueError.
    m = re.match(r"^(\d{2}):(\d{2}):(\d{2}),(\d{3})$", ts.strip())
    if not m:
        raise ValueError(f"Timestamp SRT non valido: {ts!r}")
    hh, mm, ss, ms = map(int, m.groups())
    return hh * 3600 + mm * 60 + ss + ms / 1000.0


def normalize_whitespace(text: str) -> str:
    # Normalizza whitespace e newline in modo aggressivo ma prevedibile.
    #
    # Operazioni eseguite:
    # 1) rimuove BOM residui
    # 2) converte tutti i newline in \n
    # 3) trimma ogni riga
    # 4) elimina righe vuote
    # 5) unisce tutto in una sola riga
    # 6) comprime spazi multipli
    #
    # Perché farlo:
    # - i sottotitoli spesso hanno ritorni a capo arbitari
    # - vogliamo testo pulito per l'LLM
    # - facilita il confronto tra stringhe per dedup e overlap
    text = text.replace("\ufeff", "")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = [ln.strip() for ln in text.split("\n")]
    lines = [ln for ln in lines if ln]
    joined = " ".join(lines)
    joined = re.sub(r"\s+", " ", joined).strip()
    return joined


# ============================================================
# SRT PARSING
# ============================================================

def parse_srt(path: Path) -> List[SRTBlock]:
    # Legge un file SRT e restituisce una lista di blocchi validi.
    #
    # Parser volutamente tollerante:
    # - accetta formato standard con indice numerico
    # - accetta anche blocchi senza indice
    # - accetta eventuali attributi extra dopo il timestamp finale
    # - scarta blocchi malformati senza interrompere tutto subito
    raw = path.read_text(encoding="utf-8-sig", errors="replace")
    raw = raw.replace("\r\n", "\n").replace("\r", "\n").strip()

    if not raw:
        raise ValueError(f"SRT vuoto: {path}")

    # Divide il file in blocchi separati da una o più righe vuote.
    blocks_raw = re.split(r"\n\s*\n", raw)
    blocks: List[SRTBlock] = []

    # Regex per la riga timestamp.
    #
    # Supporta:
    #   00:00:00,000 --> 00:00:01,000
    #
    # e anche:
    #   00:00:00,000 --> 00:00:01,000 align:start position:0%
    #
    # Quindi tutto ciò che segue il secondo timestamp viene ignorato.
    ts_line_re = re.compile(
        r"^(\d{2}:\d{2}:\d{2},\d{3})\s+-->\s+(\d{2}:\d{2}:\d{2},\d{3})(?:\s+.*)?$"
    )

    skipped_blocks = 0

    for n, block_raw in enumerate(blocks_raw, start=1):
        # Rimuove solo gli spazi finali di ogni riga;
        # poi elimina righe completamente vuote.
        lines = [ln.rstrip() for ln in block_raw.split("\n")]
        lines = [ln for ln in lines if ln.strip() != ""]

        # Un blocco con meno di 2 righe non è utile:
        # manca almeno timestamp o testo.
        if len(lines) < 2:
            skipped_blocks += 1
            continue

        idx: Optional[int] = None
        ts_line: Optional[str] = None
        text_lines: List[str] = []

        # ----------------------------------------------------
        # CASO STANDARD
        # ----------------------------------------------------
        #
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

        # ----------------------------------------------------
        # CASO NON STANDARD
        # ----------------------------------------------------
        #
        # 00:00:00,000 --> 00:00:01,000
        # testo...
        #
        # Qui non c'è indice numerico iniziale.
        # Lo ricostruiamo in modo progressivo.
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

        # Blocchi senza testo non servono.
        if not text:
            skipped_blocks += 1
            continue

        duration = end_sec - start_sec

        # Scarta blocchi troppo brevi.
        if duration < MIN_BLOCK_DURATION_SEC:
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


# ============================================================
# SLIDES CSV PARSING
# ============================================================

def parse_slides_csv(path: Path) -> List[Slide]:
    # Legge slides.csv e costruisce la lista Slide.
    #
    # Il parser è relativamente flessibile sui nomi colonna:
    # - slide_index / slide / index
    # - timestamp_sec / time_sec / seconds / timestamp
    # - timestamp_hms / time_hms / hms
    # - filename / file / image / slide_filename
    #
    # Questo aiuta a tollerare variazioni moderate nel CSV prodotto
    # da versioni diverse dello script a monte.
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        if reader.fieldnames is None:
            raise ValueError(f"CSV senza intestazione: {path}")

        # Mappa "nome normalizzato" -> "nome originale nel CSV"
        #
        # Esempio:
        #   " slide_index " -> "slide_index"
        #   "TIMESTAMP_SEC" -> "TIMESTAMP_SEC"
        #
        # Serve per trovare le colonne in modo case-insensitive
        # e robusto rispetto a spazi accidentali nell'header.
        normalized_fields = {name.strip().lower(): name for name in reader.fieldnames}

        def get_field(row: dict, *candidates: str, required: bool = True) -> str:
            # Cerca la prima colonna disponibile tra i candidati.
            #
            # Se required=True:
            # - se non trova nessuna colonna valida -> errore
            #
            # Se required=False:
            # - se non trova nulla -> restituisce stringa vuota
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

        # start=2 perché la riga 1 è l'intestazione CSV.
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

            # Converte slide_index in int.
            try:
                slide_index = int(slide_index_str)
            except Exception as exc:
                raise ValueError(
                    f"slide_index non valido alla riga CSV {i}: {slide_index_str!r}"
                ) from exc

            # Converte timestamp_sec in float.
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

    # Ordina per indice slide, così tutto il resto del flusso lavora
    # su una sequenza consistente e prevedibile.
    slides.sort(key=lambda s: s.slide_index)
    return slides


# ============================================================
# TEMPORAL MAPPING
# ============================================================

def find_slide_for_time(time_sec: float, slides: List[Slide]) -> Slide:
    # Dato un timestamp in secondi, restituisce la slide attiva in quel momento.
    #
    # Logica:
    # - se time_sec cade tra slide[i] e slide[i+1], appartiene a slide[i]
    # - se supera tutte le soglie, viene assegnato all'ultima slide
    #
    # In pratica usa il timestamp di comparsa di ogni slide come "inizio
    # intervallo" valido fino alla slide successiva.
    for i in range(len(slides) - 1):
        cur = slides[i]
        nxt = slides[i + 1]
        if cur.timestamp_sec <= time_sec < nxt.timestamp_sec:
            return cur
    return slides[-1]


# ============================================================
# OVERLAP / DEDUP HELPERS
# ============================================================

def _tokenize_for_overlap(text: str) -> List[str]:
    # Tokenizza il testo in modo semplice ma utile per confronti di overlap.
    #
    # Output:
    # - parole
    # - punteggiatura come token separati
    #
    # Questo è più robusto del semplice split() quando cambia leggermente
    # la punteggiatura tra una riga e l'altra.
    text = normalize_whitespace(text).lower()
    return re.findall(r"\w+|[^\w\s]", text, flags=re.UNICODE)


def _find_overlap_token_count(
    prev: str,
    curr: str,
    min_words: int = 2,
    max_words: int = 30,
) -> int:
    # Cerca quante parole iniziali di curr sono in sovrapposizione
    # con la coda di prev.
    #
    # Scopo:
    # eliminare ripetizioni del tipo
    #   prev = "questo è un esempio di frase"
    #   curr = "di frase che continua..."
    #
    # oppure casi un po' più sporchi con punteggiatura diversa.
    prev = normalize_whitespace(prev)
    curr = normalize_whitespace(curr)

    if not prev or not curr:
        return 0

    # Se le due stringhe sono identiche, overlap totale.
    if curr == prev:
        return len(curr.split())

    prev_words = prev.split()
    curr_words = curr.split()
    max_k = min(len(prev_words), len(curr_words), max_words)

    # --------------------------------------------------------
    # TENTATIVO 1: MATCH ESATTO SU PAROLE
    # --------------------------------------------------------
    #
    # Confronta suffisso di prev con prefisso di curr.
    # Parte dal match più lungo possibile e scende.
    for k in range(max_k, min_words - 1, -1):
        if prev_words[-k:] == curr_words[:k]:
            return k

    # --------------------------------------------------------
    # TENTATIVO 2: MATCH TOKENIZZATO / PIÙ TOLLERANTE
    # --------------------------------------------------------
    #
    # Serve a reggere meglio casi con punteggiatura leggermente diversa.
    prev_tokens = _tokenize_for_overlap(prev)
    curr_tokens = _tokenize_for_overlap(curr)

    if not prev_tokens or not curr_tokens:
        return 0

    max_t = min(len(prev_tokens), len(curr_tokens), max_words * 3)
    best_words = 0

    for t in range(max_t, 0, -1):
        if prev_tokens[-t:] != curr_tokens[:t]:
            continue

        # Ricostruisce il testo overlappato per stimare quante parole
        # rappresenta davvero quel match tokenizzato.
        overlap_text = "".join(
            ch if re.match(r"[^\w\s]", ch, flags=re.UNICODE) else f" {ch}"
            for ch in curr_tokens[:t]
        )
        overlap_text = normalize_whitespace(overlap_text)
        overlap_words = len(overlap_text.split())

        if overlap_words >= min_words:
            best_words = overlap_words
            break

    return best_words


def strip_overlap(prev: str, curr: str, min_words: int = 2, max_words: int = 30) -> str:
    # Rimuove dal testo corrente l'eventuale prefisso già presente
    # in coda al testo precedente.
    #
    # Esempio:
    #   prev = "oggi parliamo di anatomia funzionale"
    #   curr = "di anatomia funzionale dell'articolazione"
    #
    # risultato:
    #   "dell'articolazione"
    prev = normalize_whitespace(prev)
    curr = normalize_whitespace(curr)

    if not prev or not curr:
        return curr

    overlap_words = _find_overlap_token_count(
        prev,
        curr,
        min_words=min_words,
        max_words=max_words,
    )

    if overlap_words <= 0:
        return curr

    curr_words = curr.split()
    if overlap_words >= len(curr_words):
        return ""

    trimmed = " ".join(curr_words[overlap_words:]).strip()
    return normalize_whitespace(trimmed)


def clean_slide_lines(lines: List[str]) -> List[str]:
    # Pulisce la lista dei blocchi testuali assegnati a una singola slide.
    #
    # Operazioni:
    # 1) normalizza ogni riga/blocco
    # 2) elimina vuoti
    # 3) rimuove overlap tra blocco precedente e blocco corrente
    # 4) deduplica eventuali righe uguali adiacenti
    out: List[str] = []
    prev_raw: Optional[str] = None

    for raw_line in lines:
        raw_line = normalize_whitespace(raw_line)
        if not raw_line:
            continue

        if prev_raw is None:
            out.append(raw_line)
            prev_raw = raw_line
            continue

        trimmed = strip_overlap(prev_raw, raw_line)
        if trimmed:
            out.append(trimmed)

        prev_raw = raw_line

    return dedupe_adjacent_lines(out)


# ============================================================
# AGGREGAZIONE TESTO PER SLIDE
# ============================================================

def aggregate_text_by_slide(
    slides: List[Slide],
    srt_blocks: List[SRTBlock],
) -> dict[int, List[str]]:
    # Costruisce una mappa:
    #   slide_index -> lista di blocchi testo assegnati a quella slide
    #
    # Strategia di assegnazione:
    # - usa start_sec del blocco SRT
    # - trova la slide attiva in quel momento
    # - aggiunge il testo del blocco alla lista della slide
    by_slide: dict[int, List[str]] = {s.slide_index: [] for s in slides}

    for block in srt_blocks:
        if not block.text:
            continue
        slide = find_slide_for_time(block.start_sec, slides)
        by_slide[slide.slide_index].append(block.text)

    return by_slide


def dedupe_adjacent_lines(lines: List[str]) -> List[str]:
    # Elimina duplicati adiacenti esatti.
    #
    # Esempio:
    #   ["ciao", "ciao", "mondo"] -> ["ciao", "mondo"]
    #
    # Non elimina duplicati non adiacenti perché qui vogliamo solo
    # ripulire ridondanze immediate, non fare dedup globale aggressivo.
    out: List[str] = []
    prev: Optional[str] = None
    for line in lines:
        if line == prev:
            continue
        out.append(line)
        prev = line
    return out


def join_slide_text(lines: List[str], empty_placeholder: str = "") -> str:
    # Converte la lista dei blocchi testuali di una slide in un'unica stringa finale.
    #
    # Flusso:
    # 1) pulizia / overlap removal tra righe
    # 2) normalizzazione finale
    # 3) rimozione vuoti
    # 4) join con spazio
    #
    # Se alla fine non resta nulla:
    # - restituisce empty_placeholder
    lines = clean_slide_lines(lines)
    lines = [normalize_whitespace(x) for x in lines]
    lines = [x for x in lines if x]

    if not lines:
        return empty_placeholder

    text = " ".join(lines)
    text = re.sub(r"\s+", " ", text).strip()
    return text


# ============================================================
# DEDUP TRA SLIDE CONSECUTIVE
# ============================================================

def dedupe_across_slides(
    slides: List[Slide],
    slide_text_map: dict[int, str],
    empty_placeholder: str = "",
) -> dict[int, str]:
    # Rimuove overlap tra slide consecutive.
    #
    # Perché serve:
    # quando la voce del relatore attraversa il cambio slide, parte del testo
    # può finire sia nella slide precedente sia in quella successiva.
    #
    # Qui facciamo un secondo passaggio, a livello di slide intere.
    if not slides:
        return slide_text_map

    cleaned = dict(slide_text_map)

    for i in range(1, len(slides)):
        prev_idx = slides[i - 1].slide_index
        curr_idx = slides[i].slide_index

        prev_text = normalize_whitespace(cleaned.get(prev_idx, ""))
        curr_text = normalize_whitespace(cleaned.get(curr_idx, ""))

        if not curr_text:
            continue

        trimmed_curr = strip_overlap(prev_text, curr_text, min_words=3, max_words=40)

        # Secondo passaggio più brutale:
        # se il testo corrente è praticamente già contenuto nella coda della
        # slide precedente, allora si scarta del tutto.
        #
        # Questo cattura casi tipo:
        # - testo molto corto
        # - ripetizione quasi completa
        # - chiusura del parlato rimasta appesa tra due slide
        if trimmed_curr:
            prev_words = prev_text.split()
            curr_words = curr_text.split()
            tail = " ".join(prev_words[-min(len(prev_words), 50):])
            if tail and curr_text and curr_text in tail:
                trimmed_curr = ""

        cleaned[curr_idx] = trimmed_curr if trimmed_curr else empty_placeholder

    return cleaned


# ============================================================
# CHUNKING / OUTPUT
# ============================================================

def chunked(seq: List[Slide], size: int) -> List[List[Slide]]:
    # Divide la lista slide in sottoliste di dimensione massima "size".
    #
    # Esempio:
    #   45 slide, size=20
    #   -> [slide 1-20], [21-40], [41-45]
    return [seq[i:i + size] for i in range(0, len(seq), size)]


def write_chunk_file(
    path: Path,
    chunk_number: int,
    slides_chunk: List[Slide],
    slide_text_map: dict[int, str],
) -> None:
    # Scrive un file chunk nel formato strutturato atteso dal workflow LLM.
    #
    # Formato:
    #
    # ===== BEGIN CHUNK 001 =====
    #
    # ----- BEGIN SLIDE 0001 -----
    # TEXT:
    # ...
    #
    # ----- END SLIDE 0001 -----
    #
    # ...
    # ===== END CHUNK 001 =====
    #
    # Questo formato è pensato per:
    # - essere leggibile da umano
    # - essere facilmente parsabile dopo la correzione LLM
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


# ============================================================
# MAIN
# ============================================================

def main() -> int:
    # Entry point operativo dello script.
    #
    # Flusso generale:
    # 1) legge argomenti
    # 2) valida input
    # 3) legge CSV slide
    # 4) legge e parsea SRT
    # 5) assegna i blocchi SRT alle slide
    # 6) genera testo finale per ogni slide
    # 7) fa dedup tra slide consecutive
    # 8) divide in chunk
    # 9) scrive i file output
    args = parse_args()

    srt_path = Path(args.srt)
    slides_csv_path = Path(args.slides_csv)
    output_dir = Path(args.output_dir)
    base_name = args.base_name
    chunk_size = args.chunk_size
    empty_placeholder = args.empty_placeholder

    # Validazione chunk size.
    if chunk_size <= 0:
        eprint("Errore: --chunk-size deve essere > 0")
        return 1

    # Verifica esistenza file input.
    if not srt_path.is_file():
        eprint(f"Errore: SRT non trovato: {srt_path}")
        return 1

    if not slides_csv_path.is_file():
        eprint(f"Errore: slides.csv non trovato: {slides_csv_path}")
        return 1

    # Crea output dir se necessario.
    output_dir.mkdir(parents=True, exist_ok=True)

    eprint(f"[INFO] Leggo slides.csv: {slides_csv_path}")
    slides = parse_slides_csv(slides_csv_path)
    eprint(f"[INFO] Slide trovate: {len(slides)}")

    eprint(f"[INFO] Leggo SRT: {srt_path}")
    srt_blocks = parse_srt(srt_path)

    eprint("[INFO] Assegno i blocchi SRT alle slide...")
    by_slide_lines = aggregate_text_by_slide(slides, srt_blocks)

    # Mappa finale:
    #   slide_index -> testo consolidato della slide
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

    # Dedup finale tra slide consecutive.
    slide_text_map = dedupe_across_slides(
        slides,
        slide_text_map,
        empty_placeholder=empty_placeholder,
    )

    non_empty_slides = sum(1 for text in slide_text_map.values() if text.strip())
    eprint(f"[INFO] Slide con testo non vuoto: {non_empty_slides}/{len(slides)}")

    # Divide le slide in chunk da dare all'LLM.
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


# ============================================================
# ENTRY POINT
# ============================================================

if __name__ == "__main__":
    # Restituisce come exit code il valore ritornato da main().
    raise SystemExit(main())