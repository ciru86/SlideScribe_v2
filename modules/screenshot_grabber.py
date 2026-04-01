#!/usr/bin/env python3

# ============================================================
# Screenshot_grabber_commented.py
# ============================================================
# Scopo:
#   Estrarre automaticamente le slide da un video di una lezione.
#
# Flusso generale:
#   1) L'utente seleziona la slide come quadrilatero (4 vertici).
#   2) L'utente può opzionalmente selezionare una ROI trigger separata
#      rettangolare, usata solo per rilevare il cambio slide.
#   3) Lo script campiona il video a intervalli regolari.
#   4) Quando la ROI trigger cambia abbastanza, prova a stabilizzare la
#      transizione cercando un frame successivo più "stabile".
#   5) Salva la slide raddrizzata con trasformazione prospettica.
#   6) A fine processo deduplica eventuali quasi-duplicati e rinumera i file.
#
# Nota importante su macOS:
#   cv2.selectROI() su macOS può lasciare la GUI di OpenCV in uno stato
#   sporco (Dock con Python ancora attivo / beachball). Per questo il trigger
#   rettangolare NON usa selectROI(), ma una UI custom basata su mouse callback.
# ============================================================

import os
import csv
import math
import time
import argparse
from dataclasses import dataclass
from typing import List, Optional, Tuple

import cv2
import numpy as np
from skimage.metrics import structural_similarity as ssim

# ------------------------------------------------------------
# Alias di tipo per rendere più leggibili firme e commenti.
# ------------------------------------------------------------
Point = Tuple[int, int]
RectROI = Tuple[int, int, int, int]
QuadROI = np.ndarray  # shape (4, 2), float32 ordered TL, TR, BR, BL


# ============================================================
# DATA STRUCTURES
# ============================================================

@dataclass
class SlideCapture:
    # Rappresenta una slide già salvata su disco.
    # L'immagine tenuta in memoria è quella rettificata, utile per dedup finale.
    index: int
    time_sec: float
    filename: str
    image: np.ndarray


# ============================================================
# UTILITY FUNCTIONS
# ============================================================

def ensure_dir(path: str) -> None:
    # Crea la cartella se non esiste già.
    os.makedirs(path, exist_ok=True)


def format_timestamp(seconds: float) -> str:
    # Formato filename-friendly: HH-MM-SS_mmm
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    ms = int(round((seconds - int(seconds)) * 1000))
    if ms == 1000:
        s += 1
        ms = 0
    return f"{h:02d}-{m:02d}-{s:02d}_{ms:03d}"


def default_output_dir_from_video(video_path: str) -> str:
    # Se l'utente non specifica -o/--output, usa "<nome video> slides"
    base = os.path.splitext(os.path.basename(video_path))[0]
    return f"{base} slides"


def preprocess(img: np.ndarray, max_width: int = 1000) -> np.ndarray:
    # Preprocessing leggero per il confronto immagini:
    # - riduzione opzionale della larghezza
    # - scala di grigi
    # - blur gaussiano leggero per ridurre rumore/flicker
    h, w = img.shape[:2]
    if w > max_width:
        scale = max_width / w
        img = cv2.resize(
            img,
            (int(w * scale), int(h * scale)),
            interpolation=cv2.INTER_AREA,
        )
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    gray = cv2.GaussianBlur(gray, (5, 5), 0)
    return gray


# ============================================================
# IMAGE COMPARISON
# ============================================================

def compare_images(img1: np.ndarray, img2: np.ndarray) -> dict:
    # Confronta due immagini con tre metriche complementari:
    # - SSIM: somiglianza strutturale globale
    # - mean_diff: differenza media per pixel
    # - changed_ratio: percentuale di pixel che superano una soglia di differenza
    #
    # Usare solo SSIM spesso non basta: una slide con piccola animazione o
    # comparsa di un box può avere SSIM ancora alta. Le altre due metriche
    # aiutano a catturare cambiamenti localizzati ma visibili.
    a = preprocess(img1)
    b = preprocess(img2)

    # Allinea le dimensioni al minimo comune, così il confronto non esplode se
    # le immagini differiscono di pochi pixel.
    h = min(a.shape[0], b.shape[0])
    w = min(a.shape[1], b.shape[1])
    a = a[:h, :w]
    b = b[:h, :w]

    ssim_score = float(ssim(a, b))
    diff = cv2.absdiff(a, b)
    mean_diff = float(np.mean(diff))

    _, thresh = cv2.threshold(diff, 20, 255, cv2.THRESH_BINARY)
    changed_ratio = float(np.count_nonzero(thresh)) / thresh.size

    return {
        "ssim": ssim_score,
        "mean_diff": mean_diff,
        "changed_ratio": changed_ratio,
    }


def is_slide_change(
    metrics: dict,
    ssim_threshold: float,
    mean_diff_threshold: float,
    changed_ratio_threshold: float,
) -> bool:
    # Euristica per dire "questa non è più la slide precedente".
    #
    # Logica scelta:
    #   - SSIM deve scendere sotto soglia
    #   - inoltre almeno una tra mean_diff e changed_ratio deve superare soglia
    #
    # Questo evita di salvare cambiamenti minimi dovuti a rumore, compressione,
    # puntatore laser, micro-animazioni o variazioni di luminosità.
    return (
        metrics["ssim"] < ssim_threshold
        and (
            metrics["mean_diff"] > mean_diff_threshold
            or metrics["changed_ratio"] > changed_ratio_threshold
        )
    )


# ============================================================
# FILE OUTPUT
# ============================================================

def save_image(img: np.ndarray, out_dir: str, index: int, time_sec: float) -> str:
    # Salva una slide su disco con nome indicizzato + timestamp.
    ts = format_timestamp(time_sec)
    filename = os.path.join(out_dir, f"slide_{index:03d}_{ts}.png")
    ok = cv2.imwrite(filename, img)
    if not ok:
        raise RuntimeError(f"Impossibile salvare il file: {filename}")
    return filename


def write_csv(records: List[SlideCapture], out_csv: str) -> None:
    # Scrive il mapping slide -> timestamp -> filename.
    # Utile per post-processing, PDF assembly o debugging.
    with open(out_csv, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["slide_index", "timestamp_sec", "timestamp_hms", "filename"])
        for rec in records:
            writer.writerow([
                rec.index,
                f"{rec.time_sec:.3f}",
                format_timestamp(rec.time_sec),
                os.path.basename(rec.filename),
            ])


# ============================================================
# ROI HANDLING
# ============================================================

def clamp_roi(frame: np.ndarray, roi: RectROI) -> Tuple[int, int, int, int]:
    # Converte una ROI (x, y, w, h) nei limiti reali del frame.
    # Ritorna coordinate clampate come x1, y1, x2, y2.
    h, w = frame.shape[:2]
    x, y, rw, rh = roi
    x1 = max(0, min(x, w - 1))
    y1 = max(0, min(y, h - 1))
    x2 = max(x1 + 1, min(x + rw, w))
    y2 = max(y1 + 1, min(y + rh, h))
    return x1, y1, x2, y2


def crop_roi(frame: np.ndarray, roi: RectROI) -> np.ndarray:
    # Estrae una copia della ROI dal frame.
    x1, y1, x2, y2 = clamp_roi(frame, roi)
    return frame[y1:y2, x1:x2].copy()


# ============================================================
# OPENCV UI HELPERS
# ============================================================

def close_cv_ui(window_title: Optional[str] = None) -> None:
    # Chiusura robusta della GUI OpenCV su macOS.
    #
    # Perché è qui:
    #   OpenCV / HighGUI su macOS a volte "chiude" la finestra visivamente ma
    #   lascia il backend GUI mezzo attivo. Qui proviamo a:
    #   1) scollegare callback mouse
    #   2) distruggere la finestra specifica, se nota
    #   3) distruggere tutte le finestre
    #   4) dare qualche tick all'event loop
    try:
        if window_title is not None:
            try:
                cv2.setMouseCallback(window_title, lambda *args: None)
            except cv2.error:
                pass
            try:
                cv2.destroyWindow(window_title)
            except cv2.error:
                pass

        cv2.destroyAllWindows()

        for _ in range(8):
            cv2.waitKey(1)
            time.sleep(0.01)
    except cv2.error:
        pass


def scale_frame_for_display(frame: np.ndarray, max_w: int = 1400) -> Tuple[np.ndarray, float]:
    # Riduce il frame per visualizzazione interattiva, mantenendo il fattore di
    # scala per poi riportare i click alle coordinate originali del video.
    display = frame.copy()
    scale = 1.0
    h, w = display.shape[:2]

    if w > max_w:
        scale = max_w / w
        display = cv2.resize(
            display,
            (int(w * scale), int(h * scale)),
            interpolation=cv2.INTER_AREA,
        )

    return display, scale


# ============================================================
# INTERACTIVE RECT SELECTION
# ============================================================

def build_rect_preview(
    display_frame: np.ndarray,
    start_pt: Optional[Point],
    end_pt: Optional[Point],
    confirmed_roi: Optional[RectROI],
) -> np.ndarray:
    # Costruisce la preview della selezione rettangolare trigger.
    # Se l'utente sta trascinando, mostra il rettangolo temporaneo.
    # Se ha già rilasciato il mouse, mostra il rettangolo confermabile.
    canvas = display_frame.copy()

    instructions = [
        "Drag: seleziona rettangolo trigger | ENTER/SPACE conferma | R reset | ESC annulla",
        "Il rettangolo selezionato viene usato per rilevare il cambio slide",
    ]
    y = 28
    for line in instructions:
        cv2.putText(canvas, line, (20, y), cv2.FONT_HERSHEY_SIMPLEX, 0.72, (0, 255, 255), 2, cv2.LINE_AA)
        y += 28

    rect_to_draw = None

    if start_pt is not None and end_pt is not None:
        x1 = min(start_pt[0], end_pt[0])
        y1 = min(start_pt[1], end_pt[1])
        x2 = max(start_pt[0], end_pt[0])
        y2 = max(start_pt[1], end_pt[1])
        rect_to_draw = (x1, y1, x2 - x1, y2 - y1)
    elif confirmed_roi is not None:
        rect_to_draw = confirmed_roi

    if rect_to_draw is not None:
        x, y, w, h = rect_to_draw
        if w > 0 and h > 0:
            cv2.rectangle(canvas, (x, y), (x + w, y + h), (0, 255, 0), 2, lineType=cv2.LINE_AA)
            label = f"Trigger ROI: x={x}, y={y}, w={w}, h={h}"
            cv2.putText(canvas, label, (20, y + 10 if y > 60 else 90), cv2.FONT_HERSHEY_SIMPLEX, 0.72, (255, 255, 255), 2, cv2.LINE_AA)

    return canvas


def interactive_select_rect(
    video_path: str,
    window_title: str,
    prompt: str,
    preview_time_sec: float = 0.0,
) -> RectROI:
    # Selettore custom del trigger rettangolare.
    # Sostituisce cv2.selectROI() per evitare glitch della GUI su macOS.
    cap = cv2.VideoCapture(video_path)
    if preview_time_sec > 0:
        cap.set(cv2.CAP_PROP_POS_MSEC, preview_time_sec * 1000.0)
    ok, frame = cap.read()
    cap.release()

    if not ok or frame is None:
        raise RuntimeError("Impossibile leggere il frame di anteprima del video.")

    display_frame, scale = scale_frame_for_display(frame)

    start_pt: Optional[Point] = None
    current_pt: Optional[Point] = None
    confirmed_roi: Optional[RectROI] = None
    dragging = False

    def normalize_rect(p1: Point, p2: Point) -> RectROI:
        # Rende il rettangolo indipendente dalla direzione del drag.
        x1 = min(p1[0], p2[0])
        y1 = min(p1[1], p2[1])
        x2 = max(p1[0], p2[0])
        y2 = max(p1[1], p2[1])
        return x1, y1, x2 - x1, y2 - y1

    def on_mouse(event: int, x: int, y: int, flags: int, param: object) -> None:
        nonlocal start_pt, current_pt, confirmed_roi, dragging

        if event == cv2.EVENT_LBUTTONDOWN:
            start_pt = (x, y)
            current_pt = (x, y)
            confirmed_roi = None
            dragging = True
        elif event == cv2.EVENT_MOUSEMOVE and dragging:
            current_pt = (x, y)
        elif event == cv2.EVENT_LBUTTONUP and dragging:
            current_pt = (x, y)
            confirmed_roi = normalize_rect(start_pt, current_pt)
            dragging = False

    print(f"\n{prompt}\n")
    print("Istruzioni: trascina per disegnare il trigger rettangolare. ENTER/SPACE conferma, R resetta, ESC annulla.")

    cv2.namedWindow(window_title, cv2.WINDOW_NORMAL)
    cv2.setMouseCallback(window_title, on_mouse)

    try:
        while True:
            canvas = build_rect_preview(
                display_frame,
                start_pt if dragging else None,
                current_pt if dragging else None,
                confirmed_roi,
            )
            cv2.imshow(window_title, canvas)
            key = cv2.waitKey(20) & 0xFF

            if key in (13, 32):
                if confirmed_roi is not None and confirmed_roi[2] > 0 and confirmed_roi[3] > 0:
                    break
            elif key in (ord('r'), ord('R')):
                start_pt = None
                current_pt = None
                confirmed_roi = None
                dragging = False
            elif key == 27:
                raise RuntimeError("Selezione ROI rettangolare annullata.")
    finally:
        close_cv_ui(window_title)

    if confirmed_roi is None or confirmed_roi[2] <= 0 or confirmed_roi[3] <= 0:
        raise RuntimeError("ROI non selezionata.")

    # Riporta la ROI dalla preview ridimensionata alle coordinate originali.
    x, y, rw, rh = confirmed_roi
    x = int(round(x / scale))
    y = int(round(y / scale))
    rw = int(round(rw / scale))
    rh = int(round(rh / scale))

    return x, y, rw, rh


# ============================================================
# QUADRILATERAL GEOMETRY & PERSPECTIVE WARP
# ============================================================

def order_quad_points(points: np.ndarray) -> QuadROI:
    # Riordina 4 punti cliccati dall'utente nell'ordine:
    #   top-left, top-right, bottom-right, bottom-left
    #
    # Strategia:
    #   1) calcola il centro
    #   2) ordina per angolo rispetto al centro
    #   3) ruota la sequenza per far partire da top-left
    #   4) forza orientamento consistente
    pts = np.asarray(points, dtype=np.float32)
    if pts.shape != (4, 2):
        raise ValueError("Servono esattamente 4 punti.")

    center = np.mean(pts, axis=0)
    angles = np.arctan2(pts[:, 1] - center[1], pts[:, 0] - center[0])
    ordered = pts[np.argsort(angles)]

    sums = ordered[:, 0] + ordered[:, 1]
    start_idx = int(np.argmin(sums))
    ordered = np.roll(ordered, -start_idx, axis=0)

    area2 = 0.0
    for i in range(4):
        x1, y1 = ordered[i]
        x2, y2 = ordered[(i + 1) % 4]
        area2 += (x1 * y2) - (x2 * y1)
    if area2 < 0:
        ordered = np.array([ordered[0], ordered[3], ordered[2], ordered[1]], dtype=np.float32)

    return ordered.astype(np.float32)


def distance(p1: np.ndarray, p2: np.ndarray) -> float:
    # Distanza euclidea tra due punti.
    return float(np.linalg.norm(p1 - p2))


def quad_output_size(quad: QuadROI) -> Tuple[int, int]:
    # Determina dimensione output della slide rettificata a partire dalla
    # geometria reale del quadrilatero selezionato.
    #
    # Invece di un output fisso hardcoded, usa media dei lati opposti per width
    # e height. Così se la slide nel video è molto larga/stretta l'output resta
    # coerente.
    tl, tr, br, bl = quad
    width = int(round((distance(tl, tr) + distance(bl, br)) / 2.0))
    height = int(round((distance(tl, bl) + distance(tr, br)) / 2.0))
    width = max(50, width)
    height = max(50, height)
    return width, height


def clip_quad_to_frame(frame: np.ndarray, quad: QuadROI) -> QuadROI:
    # Clamp dei vertici nei limiti del frame per evitare errori geometrici.
    h, w = frame.shape[:2]
    clipped = quad.copy().astype(np.float32)
    clipped[:, 0] = np.clip(clipped[:, 0], 0, w - 1)
    clipped[:, 1] = np.clip(clipped[:, 1], 0, h - 1)
    return clipped


def warp_quad_to_rect(frame: np.ndarray, quad: QuadROI) -> np.ndarray:
    # Applica trasformazione prospettica: da quadrilatero a rettangolo pulito.
    quad = clip_quad_to_frame(frame, quad)
    out_w, out_h = quad_output_size(quad)

    dst = np.array([
        [0, 0],
        [out_w - 1, 0],
        [out_w - 1, out_h - 1],
        [0, out_h - 1],
    ], dtype=np.float32)

    matrix = cv2.getPerspectiveTransform(quad, dst)
    warped = cv2.warpPerspective(frame, matrix, (out_w, out_h))
    return warped


# ============================================================
# OPTIONAL IMAGE ENHANCEMENT
# ============================================================

def gamma_correction(img: np.ndarray, gamma: float) -> np.ndarray:
    # Gamma > 1: schiarisce leggermente le medie tonalità.
    if gamma <= 0:
        return img

    inv_gamma = 1.0 / gamma
    table = np.array([
        ((i / 255.0) ** inv_gamma) * 255 for i in np.arange(256)
    ]).astype("uint8")
    return cv2.LUT(img, table)


def auto_white_balance_grayworld(img: np.ndarray) -> np.ndarray:
    # Bilanciamento del bianco semplice con ipotesi gray-world.
    # Conservativo ma spesso utile su registrazioni con dominanti strane.
    b, g, r = cv2.split(img.astype(np.float32))
    mean_b = np.mean(b)
    mean_g = np.mean(g)
    mean_r = np.mean(r)
    gray = (mean_b + mean_g + mean_r) / 3.0

    scale_b = gray / mean_b if mean_b > 0 else 1.0
    scale_g = gray / mean_g if mean_g > 0 else 1.0
    scale_r = gray / mean_r if mean_r > 0 else 1.0

    b *= scale_b
    g *= scale_g
    r *= scale_r

    balanced = cv2.merge([b, g, r])
    balanced = np.clip(balanced, 0, 255).astype(np.uint8)
    return balanced


def unsharp_mask(img: np.ndarray, sigma: float = 1.0, amount: float = 0.4) -> np.ndarray:
    # Sharpen leggero per rendere il testo un po' più incisivo senza esagerare.
    blurred = cv2.GaussianBlur(img, (0, 0), sigma)
    sharpened = cv2.addWeighted(img, 1.0 + amount, blurred, -amount, 0)
    return np.clip(sharpened, 0, 255).astype(np.uint8)


def enhance_slide(img: np.ndarray, preset: str = "mild") -> np.ndarray:
    # Pipeline di enhancement volutamente prudente.
    # Pensata per slide e testo, non per fotografia.
    #
    # Ordine scelto:
    #   1) white balance
    #   2) eventuale denoise
    #   3) CLAHE sulla luminanza
    #   4) gamma correction leggera
    #   5) sharpen finale
    if preset == "off":
        return img

    settings = {
        "mild": {
            "clahe_clip": 2.0,
            "clahe_grid": (8, 8),
            "gamma": 1.06,
            "sharpen_amount": 0.30,
            "denoise": False,
        },
        "medium": {
            "clahe_clip": 2.6,
            "clahe_grid": (8, 8),
            "gamma": 1.10,
            "sharpen_amount": 0.42,
            "denoise": True,
        },
        "strong": {
            "clahe_clip": 3.2,
            "clahe_grid": (8, 8),
            "gamma": 1.14,
            "sharpen_amount": 0.55,
            "denoise": True,
        },
    }

    if preset not in settings:
        raise ValueError(f"Preset enhancement non valido: {preset}")

    cfg = settings[preset]
    out = img.copy()

    out = auto_white_balance_grayworld(out)

    if cfg["denoise"]:
        out = cv2.fastNlMeansDenoisingColored(out, None, 3, 3, 7, 21)

    lab = cv2.cvtColor(out, cv2.COLOR_BGR2LAB)
    l, a, b = cv2.split(lab)

    clahe = cv2.createCLAHE(
        clipLimit=cfg["clahe_clip"],
        tileGridSize=cfg["clahe_grid"],
    )
    l = clahe.apply(l)

    lab = cv2.merge((l, a, b))
    out = cv2.cvtColor(lab, cv2.COLOR_LAB2BGR)

    out = gamma_correction(out, cfg["gamma"])
    out = unsharp_mask(out, sigma=1.0, amount=cfg["sharpen_amount"])

    return out


# ============================================================
# INTERACTIVE QUAD SELECTION
# ============================================================

def build_quad_preview(display_frame: np.ndarray, points: List[Point]) -> np.ndarray:
    # Preview della selezione quadrilatera slide.
    # Dopo il 4° punto mostra anche una preview rettificata in piccolo.
    canvas = display_frame.copy()

    instructions = [
        "Click: 4 vertici slide | ENTER/SPACE conferma | R reset | ESC annulla",
        "Dopo il 4° punto viene mostrata una preview rettificata",
    ]
    y = 28
    for line in instructions:
        cv2.putText(canvas, line, (20, y), cv2.FONT_HERSHEY_SIMPLEX, 0.72, (0, 255, 255), 2, cv2.LINE_AA)
        y += 28

    for idx, pt in enumerate(points):
        cv2.circle(canvas, pt, 6, (0, 0, 255), -1, lineType=cv2.LINE_AA)
        cv2.putText(canvas, str(idx + 1), (pt[0] + 8, pt[1] - 8), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (255, 255, 0), 2, cv2.LINE_AA)

    if len(points) >= 2:
        pts_arr = np.array(points, dtype=np.int32)
        cv2.polylines(canvas, [pts_arr], False, (0, 255, 0), 2, lineType=cv2.LINE_AA)

    if len(points) == 4:
        ordered = order_quad_points(np.array(points, dtype=np.float32))
        quad_int = ordered.astype(np.int32)
        cv2.polylines(canvas, [quad_int], True, (0, 255, 0), 2, lineType=cv2.LINE_AA)

        warped = warp_quad_to_rect(display_frame, ordered)
        preview_max_w = 420
        preview_max_h = 260
        wh, ww = warped.shape[:2]
        scale = min(preview_max_w / ww, preview_max_h / wh, 1.0)
        preview = cv2.resize(warped, (max(1, int(ww * scale)), max(1, int(wh * scale))), interpolation=cv2.INTER_AREA)

        px = max(10, canvas.shape[1] - preview.shape[1] - 15)
        py = max(80, 15)
        cv2.rectangle(canvas, (px - 3, py - 25), (px + preview.shape[1] + 3, py + preview.shape[0] + 3), (255, 255, 255), 2)
        cv2.putText(canvas, "Preview rettificata", (px, py - 6), cv2.FONT_HERSHEY_SIMPLEX, 0.65, (255, 255, 255), 2, cv2.LINE_AA)
        canvas[py:py + preview.shape[0], px:px + preview.shape[1]] = preview

    return canvas


def interactive_select_quad(
    video_path: str,
    window_title: str,
    prompt: str,
    preview_time_sec: float = 0.0,
) -> QuadROI:
    # L'utente clicca i 4 vertici della slide in ordine libero.
    # L'algoritmo poi li riordina automaticamente.
    cap = cv2.VideoCapture(video_path)
    if preview_time_sec > 0:
        cap.set(cv2.CAP_PROP_POS_MSEC, preview_time_sec * 1000.0)
    ok, frame = cap.read()
    cap.release()

    if not ok or frame is None:
        raise RuntimeError("Impossibile leggere il frame di anteprima del video.")

    base_frame = frame.copy()
    display_frame, scale = scale_frame_for_display(base_frame)

    points: List[Point] = []

    def on_mouse(event: int, x: int, y: int, flags: int, param: object) -> None:
        nonlocal points
        if event == cv2.EVENT_LBUTTONDOWN and len(points) < 4:
            points.append((x, y))

    print(f"\n{prompt}\n")
    print("Istruzioni: click su 4 vertici della slide. ENTER/SPACE conferma, R resetta, ESC annulla.")

    cv2.namedWindow(window_title, cv2.WINDOW_NORMAL)
    cv2.setMouseCallback(window_title, on_mouse)

    try:
        while True:
            canvas = build_quad_preview(display_frame, points)
            cv2.imshow(window_title, canvas)
            key = cv2.waitKey(20) & 0xFF

            if key in (13, 32):
                if len(points) == 4:
                    break
            elif key in (ord('r'), ord('R')):
                points = []
            elif key == 27:
                raise RuntimeError("Selezione quadrilatero annullata.")
    finally:
        close_cv_ui(window_title)

    pts = np.array(points, dtype=np.float32)
    pts /= scale
    pts = order_quad_points(pts)
    return pts.astype(np.float32)


# ============================================================
# TEMPORAL STABILIZATION
# ============================================================

def maybe_extract_candidate(
    cap: cv2.VideoCapture,
    frame_idx: int,
    fps: float,
    total_frames: int,
    end_frame_exclusive: int,
    trigger_roi: RectROI,
    compare_baseline: np.ndarray,
    step_frames: int,
    stabilization_samples: int,
    stabilization_ssim: float,
) -> Optional[Tuple[np.ndarray, float]]:
    # Quando rilevi un cambio slide, il frame corrente potrebbe essere nel mezzo
    # di una transizione o animazione. Qui cerchiamo uno dei frame successivi che
    # somigli abbastanza al nuovo stato, per salvare una versione più stabile.
    best_candidate = None

    for k in range(1, stabilization_samples + 1):
        future_idx = min(frame_idx + k * step_frames, max(min(end_frame_exclusive - 1, total_frames - 1), 0))
        cap.set(cv2.CAP_PROP_POS_FRAMES, future_idx)
        ok, future_frame = cap.read()
        if not ok or future_frame is None:
            break

        future_roi = crop_roi(future_frame, trigger_roi)
        metrics = compare_images(compare_baseline, future_roi)

        if metrics["ssim"] >= stabilization_ssim:
            best_candidate = (future_frame.copy(), future_idx / fps)
            break

    return best_candidate


# ============================================================
# DEDUPLICATION & RENAMING
# ============================================================

def deduplicate_records(
    records: List[SlideCapture],
    dedup_ssim_threshold: float,
    dedup_mean_diff_threshold: float,
) -> List[SlideCapture]:
    # Dedup finale tra slide consecutive già salvate.
    # Utile per evitare doppio salvataggio di slide quasi identiche.
    if not records:
        return []

    deduped = [records[0]]

    for rec in records[1:]:
        prev = deduped[-1]
        metrics = compare_images(prev.image, rec.image)

        is_duplicate = (
            metrics["ssim"] >= dedup_ssim_threshold
            or metrics["mean_diff"] <= dedup_mean_diff_threshold
        )

        if is_duplicate:
            try:
                os.remove(rec.filename)
            except OSError:
                pass
        else:
            deduped.append(rec)

    return deduped


def renumber_files(records: List[SlideCapture]) -> List[SlideCapture]:
    # Dopo la dedup, rinumera le slide così non restano buchi negli indici.
    updated = []

    for new_idx, rec in enumerate(records, start=1):
        dirname = os.path.dirname(rec.filename)
        new_name = os.path.join(
            dirname,
            f"slide_{new_idx:03d}_{format_timestamp(rec.time_sec)}.png"
        )

        if os.path.abspath(new_name) != os.path.abspath(rec.filename):
            if os.path.exists(new_name):
                os.remove(new_name)
            os.rename(rec.filename, new_name)

        updated.append(
            SlideCapture(
                index=new_idx,
                time_sec=rec.time_sec,
                filename=new_name,
                image=rec.image,
            )
        )

    return updated


# ============================================================
# MAIN EXTRACTION PIPELINE
# ============================================================

def extract_slides(
    video_path: str,
    output_dir: Optional[str],
    sample_every_sec: float,
    ssim_threshold: float,
    mean_diff_threshold: float,
    changed_ratio_threshold: float,
    min_slide_duration_sec: float,
    stabilization_samples: int,
    stabilization_ssim: float,
    save_mode: str,
    dedup_ssim_threshold: float,
    dedup_mean_diff_threshold: float,
    save_first_slide: bool,
    use_separate_trigger_roi: bool,
    enhance_slides: bool,
    enhance_preset: str,
    skip_first_sec: float,
    skip_last_sec: float,
) -> None:
    # Funzione principale: orchestra selezione interattiva, scansione video,
    # salvataggio slide, dedup e scrittura CSV.
    if output_dir is None:
        output_dir = default_output_dir_from_video(video_path)

    ensure_dir(output_dir)

    cap = cv2.VideoCapture(video_path)
    if not cap.isOpened():
        raise RuntimeError(f"Impossibile aprire il video: {video_path}")

    fps = cap.get(cv2.CAP_PROP_FPS)
    if fps <= 0 or math.isnan(fps):
        fps = 25.0

    total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    duration_sec = total_frames / fps if total_frames > 0 else 0.0

    if skip_first_sec < 0 or skip_last_sec < 0:
        raise ValueError("--skip-first-sec e --skip-last-sec non possono essere negativi.")

    effective_start_sec = min(skip_first_sec, duration_sec)
    effective_end_sec = max(effective_start_sec, duration_sec - skip_last_sec)

    start_frame_idx = int(round(effective_start_sec * fps))
    end_frame_exclusive = int(round(effective_end_sec * fps))
    start_frame_idx = max(0, min(start_frame_idx, total_frames))
    end_frame_exclusive = max(start_frame_idx, min(end_frame_exclusive, total_frames))

    effective_duration_sec = max(0.0, effective_end_sec - effective_start_sec)
    if effective_duration_sec <= 0:
        raise RuntimeError(
            "Il range utile del video è vuoto: controlla --skip-first-sec e --skip-last-sec."
        )

    print(f"FPS: {fps:.3f}")
    print(f"Frame totali: {total_frames}")
    print(f"Durata stimata: {duration_sec:.1f} s")
    print(f"Skip iniziale: {skip_first_sec:.1f} s")
    print(f"Skip finale: {skip_last_sec:.1f} s")
    print(f"Range analizzato: {effective_start_sec:.1f}s -> {effective_end_sec:.1f}s ({effective_duration_sec:.1f} s)")
    print(f"Frame usato per la selezione ROI/slide: {effective_start_sec:.1f}s")
    print(f"Output directory: {output_dir}")

    # 1) Selezione area slide come quadrilatero.
    slide_quad = interactive_select_quad(
        video_path,
        "Seleziona slide (4 vertici)",
        "Seleziona i 4 vertici della slide da salvare. Dopo il 4° click vedrai la preview rettificata. Premi INVIO o SPAZIO per confermare.",
        preview_time_sec=effective_start_sec,
    )
    print("Slide quad ordinato (TL, TR, BR, BL):")
    for i, (x, y) in enumerate(slide_quad, start=1):
        print(f"  P{i}: x={x:.1f}, y={y:.1f}")

    # 2) Scelta area trigger: separata oppure bounding box della slide.
    if use_separate_trigger_roi:
        trigger_roi = interactive_select_rect(
            video_path,
            "Seleziona area trigger",
            "Seleziona l'area rettangolare da usare per rilevare il cambio slide, poi premi INVIO o SPAZIO.",
            preview_time_sec=effective_start_sec,
        )
    else:
        xs = slide_quad[:, 0]
        ys = slide_quad[:, 1]
        x1 = int(np.floor(np.min(xs)))
        y1 = int(np.floor(np.min(ys)))
        x2 = int(np.ceil(np.max(xs)))
        y2 = int(np.ceil(np.max(ys)))
        trigger_roi = (x1, y1, max(1, x2 - x1), max(1, y2 - y1))
        print("Trigger ROI non separata: uso il bounding box rettangolare della quadrilatera slide.")

    print(f"ROI trigger: x={trigger_roi[0]}, y={trigger_roi[1]}, w={trigger_roi[2]}, h={trigger_roi[3]}")
    close_cv_ui()

    # step_frames: ogni quanti frame fare sampling.
    # min_gap_samples: quanti sample minimi devono passare tra due slide salvate.
    step_frames = max(1, int(round(sample_every_sec * fps)))
    min_gap_samples = max(1, int(math.ceil(min_slide_duration_sec / sample_every_sec)))

    records: List[SlideCapture] = []

    # prev_saved_roi = baseline della ROI trigger dell'ultima slide salvata.
    prev_saved_roi = None
    last_saved_sample_idx = -10_000
    slide_index = 1

    sample_idx = 0
    frame_idx = start_frame_idx

    while frame_idx < end_frame_exclusive:
        cap.set(cv2.CAP_PROP_POS_FRAMES, frame_idx)
        ok, frame = cap.read()
        if not ok or frame is None:
            break

        current_warped_slide = warp_quad_to_rect(frame, slide_quad)
        current_trigger_roi = crop_roi(frame, trigger_roi)
        time_sec = frame_idx / fps

        if enhance_slides:
            current_warped_slide_to_save = enhance_slide(current_warped_slide, enhance_preset)
        else:
            current_warped_slide_to_save = current_warped_slide

        # Prima iterazione: salva eventualmente subito la prima slide e inizializza baseline.
        if prev_saved_roi is None:
            if save_first_slide:
                img_to_save = current_warped_slide_to_save if save_mode == "crop" else frame
                filename = save_image(img_to_save, output_dir, slide_index, time_sec)
                records.append(SlideCapture(slide_index, time_sec, filename, current_warped_slide_to_save))
                print(f"[SALVATA] Slide {slide_index} @ {time_sec:.1f}s -> {filename}")
                slide_index += 1

            prev_saved_roi = current_trigger_roi.copy()
            last_saved_sample_idx = sample_idx
        else:
            metrics = compare_images(prev_saved_roi, current_trigger_roi)
            enough_time_passed = (sample_idx - last_saved_sample_idx) >= min_gap_samples

            # Cambio slide rilevato solo se:
            # - è passato abbastanza tempo dalla precedente
            # - le metriche superano le soglie
            if enough_time_passed and is_slide_change(
                metrics,
                ssim_threshold=ssim_threshold,
                mean_diff_threshold=mean_diff_threshold,
                changed_ratio_threshold=changed_ratio_threshold,
            ):
                candidate = maybe_extract_candidate(
                    cap=cap,
                    frame_idx=frame_idx,
                    fps=fps,
                    total_frames=total_frames,
                    end_frame_exclusive=end_frame_exclusive,
                    trigger_roi=trigger_roi,
                    compare_baseline=current_trigger_roi,
                    step_frames=step_frames,
                    stabilization_samples=stabilization_samples,
                    stabilization_ssim=stabilization_ssim,
                )

                # Salva solo se trovi un candidato stabile. Questo riduce
                # falsi positivi durante animazioni, transizioni o micro-flicker.
                if candidate is not None:
                    stable_frame, stable_time = candidate
                    stable_warped_slide = warp_quad_to_rect(stable_frame, slide_quad)
                    stable_trigger_roi = crop_roi(stable_frame, trigger_roi)

                    if enhance_slides:
                        stable_warped_slide_to_save = enhance_slide(stable_warped_slide, enhance_preset)
                    else:
                        stable_warped_slide_to_save = stable_warped_slide

                    img_to_save = stable_warped_slide_to_save if save_mode == "crop" else stable_frame
                    filename = save_image(img_to_save, output_dir, slide_index, stable_time)
                    records.append(SlideCapture(slide_index, stable_time, filename, stable_warped_slide_to_save))

                    print(
                        f"[SALVATA] Slide {slide_index} @ {stable_time:.1f}s "
                        f"(ssim={metrics['ssim']:.3f}, mean_diff={metrics['mean_diff']:.2f}, "
                        f"changed_ratio={metrics['changed_ratio']:.3f}) -> {filename}"
                    )

                    prev_saved_roi = stable_trigger_roi.copy()
                    last_saved_sample_idx = sample_idx
                    slide_index += 1

        frame_idx += step_frames
        sample_idx += 1

    cap.release()

    print("\nDeduplicazione finale...")
    records = deduplicate_records(
        records,
        dedup_ssim_threshold=dedup_ssim_threshold,
        dedup_mean_diff_threshold=dedup_mean_diff_threshold,
    )
    records = renumber_files(records)

    csv_path = os.path.join(output_dir, "slides.csv")
    write_csv(records, csv_path)

    print("\nFatto.")
    print(f"Slide finali: {len(records)}")
    print(f"Cartella output: {output_dir}")
    print(f"CSV: {csv_path}")


# ============================================================
# CLI
# ============================================================

def build_parser() -> argparse.ArgumentParser:
    # Parser CLI con tutti i parametri principali della pipeline.
    parser = argparse.ArgumentParser(
        description="Estrae le slide da un video quando cambia il contenuto dell'area trigger; la slide viene selezionata come quadrilatero e raddrizzata prima del salvataggio."
    )

    parser.add_argument("video", help="Percorso del file video")
    parser.add_argument(
        "-o", "--output",
        default=None,
        help='Cartella di output (default: "<nome video> slides")'
    )
    parser.add_argument(
        "--sample-every",
        type=float,
        default=1.0,
        help="Campiona un frame ogni N secondi (default: 1.0)"
    )
    parser.add_argument(
        "--ssim-threshold",
        type=float,
        default=0.92,
        help="Soglia SSIM per rilevamento cambio slide (default: 0.92)"
    )
    parser.add_argument(
        "--mean-diff-threshold",
        type=float,
        default=8.0,
        help="Soglia mean diff per rilevamento cambio slide (default: 8.0)"
    )
    parser.add_argument(
        "--changed-ratio-threshold",
        type=float,
        default=0.06,
        help="Soglia percentuale pixel cambiati (0-1) (default: 0.06)"
    )
    parser.add_argument(
        "--min-slide-duration",
        type=float,
        default=2.0,
        help="Tempo minimo tra due slide salvate (default: 2.0)"
    )
    parser.add_argument(
        "--stabilization-samples",
        type=int,
        default=2,
        help="Quanti campioni successivi controllare per stabilizzare la slide (default: 2)"
    )
    parser.add_argument(
        "--stabilization-ssim",
        type=float,
        default=0.97,
        help="Quanto dev'essere stabile la nuova slide rispetto al campione dopo (default: 0.97)"
    )
    parser.add_argument(
        "--save-mode",
        choices=["crop", "full"],
        default="crop",
        help="Salva la slide rettificata oppure il frame intero (default: crop)"
    )
    parser.add_argument(
        "--dedup-ssim-threshold",
        type=float,
        default=0.985,
        help="Soglia SSIM per deduplicazione finale (default: 0.985)"
    )
    parser.add_argument(
        "--dedup-mean-diff-threshold",
        type=float,
        default=2.0,
        help="Soglia mean diff per deduplicazione finale (default: 2.0)"
    )
    parser.add_argument(
        "--no-first-slide",
        action="store_true",
        help="Non salvare subito la prima slide"
    )
    parser.add_argument(
        "--separate-trigger-roi",
        action="store_true",
        help="Permette di selezionare una ROI trigger rettangolare separata; altrimenti usa il bounding box della quadrilatera slide"
    )
    parser.add_argument(
        "--enhance-slides",
        action="store_true",
        help="Applica un miglioramento automatico conservativo alle slide rettificate prima del salvataggio"
    )
    parser.add_argument(
        "--enhance-preset",
        choices=["mild", "medium", "strong"],
        default="mild",
        help="Preset di enhancement da usare con --enhance-slides (default: mild)"
    )

    parser.add_argument(
        "--skip-first-sec",
        type=float,
        default=0.0,
        help="Salta i primi N secondi del video prima di iniziare il campionamento e il rilevamento delle slide; utile per intro, sigle o schermate iniziali (default: 0.0)"
    )
    parser.add_argument(
        "--skip-last-sec",
        type=float,
        default=0.0,
        help="Esclude gli ultimi N secondi del video dal campionamento e dal rilevamento delle slide; utile per outro, titoli finali o coda non rilevante (default: 0.0)"
    )

    return parser


# ============================================================
# ENTRY POINT
# ============================================================

def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    extract_slides(
        video_path=args.video,
        output_dir=args.output,
        sample_every_sec=args.sample_every,
        ssim_threshold=args.ssim_threshold,
        mean_diff_threshold=args.mean_diff_threshold,
        changed_ratio_threshold=args.changed_ratio_threshold,
        min_slide_duration_sec=args.min_slide_duration,
        stabilization_samples=args.stabilization_samples,
        stabilization_ssim=args.stabilization_ssim,
        save_mode=args.save_mode,
        dedup_ssim_threshold=args.dedup_ssim_threshold,
        dedup_mean_diff_threshold=args.dedup_mean_diff_threshold,
        save_first_slide=not args.no_first_slide,
        use_separate_trigger_roi=args.separate_trigger_roi,
        enhance_slides=args.enhance_slides,
        enhance_preset=args.enhance_preset,
        skip_first_sec=args.skip_first_sec,
        skip_last_sec=args.skip_last_sec,
    )


if __name__ == "__main__":
    main()
