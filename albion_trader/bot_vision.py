"""Автоопределение точек интерфейса игры.

Два способа, оба без сторонних библиотек:

* **По надписям.** Снимок клиентской области окна игры (GDI, :meth:`Desktop.capture`),
  распознавание текста встроенным в Windows 10/11 OCR (``Windows.Media.Ocr`` через
  PowerShell) и поиск подписей кнопок и полей на русском и английском с допуском на
  ошибки распознавания. Кнопка — центр найденной надписи; поле (цена, количество) —
  правее своей подписи.
* **Пробными кликами** (для торговца и сундука — у них нет надписи): бот кликает по
  кругу рядом с персонажем; клик по земле даёт только запрос движения, а клик по
  торговцу или сундуку — другой запрос игры (открылось окно). Эта точка и есть нужная.

Найденное показывается на снимке во вкладке «Боты»: точки можно принять или поправить
кликом прямо по картинке.
"""

from __future__ import annotations

import base64
import difflib
import json
import os
import re
import struct
import subprocess
import tempfile
import zlib
from pathlib import Path

# Подписи кнопок и полей (русский и английский клиент). Сравнение — без регистра,
# «ё» = «е», с допуском на ошибки распознавания.
LABELS: dict[str, dict] = {
    "sell_tab": {"texts": ["Продать", "Продажа", "Sell"], "kind": "button"},
    "buy_tab": {"texts": ["Купить", "Покупка", "Buy"], "kind": "button"},
    "search": {"texts": ["Поиск", "Найти", "Search"], "kind": "button"},
    "sell_order": {"texts": ["Заказ на продажу", "Создать заказ на продажу", "Sell Order", "Create Sell Order"],
                   "kind": "button"},
    "buy_order": {"texts": ["Заказ на покупку", "Создать заказ на покупку", "Buy Order", "Create Buy Order"],
                  "kind": "button"},
    "price": {"texts": ["Цена за штуку", "Цена", "Price per unit", "Price"], "kind": "field"},
    "qty": {"texts": ["Количество", "Кол-во", "Amount", "Quantity"], "kind": "field"},
    "confirm": {"texts": ["Создать заказ", "Подтвердить", "Разместить", "Create Order", "Place Order", "Confirm"],
                "kind": "button"},
    "loot_all": {"texts": ["Взять всё", "Забрать всё", "Взять все", "Take All", "Loot All"], "kind": "button"},
    "stash_deposit": {"texts": ["Положить всё", "Положить все", "Внести всё", "Deposit All", "Store All"],
                      "kind": "button"},
    "dungeon_enter": {"texts": ["Войти", "Enter"], "kind": "button"},
}
# Точки без надписей — только пробными кликами или вручную по снимку.
PROBE_POINTS = ("market_npc", "stash_open")
MIN_SCORE = 0.78
FIELD_OFFSET = 4.0        # поле — правее подписи на столько высот строки


def norm(text: str) -> str:
    t = (text or "").lower().replace("ё", "е")
    return re.sub(r"[^\w\s]", "", t).strip()


def similarity(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, norm(a), norm(b)).ratio()


# --- изображение ------------------------------------------------------------------
def scale2x(width: int, height: int, bgra: bytes) -> tuple[int, int, bytes]:
    """Увеличение вдвое (ближайший сосед): мелкий текст интерфейса распознаётся лучше."""
    src = memoryview(bgra)
    wide = bytearray(len(bgra) * 2)
    for c in range(4):
        wide[c::8] = src[c::4]
        wide[4 + c::8] = src[c::4]
    row = width * 8
    out = bytearray()
    for y in range(height):
        line = wide[y * row:(y + 1) * row]
        out += line
        out += line
    return width * 2, height * 2, bytes(out)


def png_encode(width: int, height: int, bgra: bytes) -> bytes:
    """PNG из пикселей BGRA (сверху вниз) — без сторонних библиотек."""
    rgba = bytearray(bgra)
    rgba[0::4], rgba[2::4] = bgra[2::4], bgra[0::4]
    rgba[3::4] = b"\xff" * (len(bgra) // 4)
    stride = width * 4
    raw = bytearray()
    for y in range(height):
        raw.append(0)
        raw += rgba[y * stride:(y + 1) * stride]

    def chunk(tag: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(bytes(raw), 6)) + chunk(b"IEND", b""))


def data_url(png: bytes) -> str:
    return "data:image/png;base64," + base64.b64encode(png).decode("ascii")


# --- распознавание текста (Windows) ------------------------------------------------
OCR_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Runtime.WindowsRuntime
$null = [Windows.Storage.StorageFile, Windows.Storage, ContentType = WindowsRuntime]
$null = [Windows.Media.Ocr.OcrEngine, Windows.Foundation, ContentType = WindowsRuntime]
$null = [Windows.Graphics.Imaging.BitmapDecoder, Windows.Foundation, ContentType = WindowsRuntime]
$null = [Windows.Globalization.Language, Windows.Foundation, ContentType = WindowsRuntime]
$asTask = ([System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
  $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and
  $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1' })[0]
function Await($op, $type) { $t = $asTask.MakeGenericMethod($type).Invoke($null, @($op)); $t.Wait(-1) | Out-Null; $t.Result }
$file = Await ([Windows.Storage.StorageFile]::GetFileFromPathAsync($env:AT_OCR_IMAGE)) ([Windows.Storage.StorageFile])
$stream = Await ($file.OpenAsync([Windows.Storage.FileAccessMode]::Read)) ([Windows.Storage.Streams.IRandomAccessStream])
$decoder = Await ([Windows.Graphics.Imaging.BitmapDecoder]::CreateAsync($stream)) ([Windows.Graphics.Imaging.BitmapDecoder])
$bitmap = Await ($decoder.GetSoftwareBitmapAsync()) ([Windows.Graphics.Imaging.SoftwareBitmap])
$engine = $null
if ($env:AT_OCR_LANG) {
  $lang = New-Object Windows.Globalization.Language $env:AT_OCR_LANG
  if ([Windows.Media.Ocr.OcrEngine]::IsLanguageSupported($lang)) { $engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromLanguage($lang) }
}
if ($engine -eq $null) { $engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromUserProfileLanguages() }
if ($engine -eq $null) { Write-Output '{"error": "no-ocr-language"}'; exit 0 }
$result = Await ($engine.RecognizeAsync($bitmap)) ([Windows.Media.Ocr.OcrResult])
$lines = @()
foreach ($line in $result.Lines) {
  $words = @()
  foreach ($w in $line.Words) {
    $r = $w.BoundingRect
    $words += @{ t = $w.Text; x = [double]$r.X; y = [double]$r.Y; w = [double]$r.Width; h = [double]$r.Height }
  }
  $lines += @{ text = $line.Text; words = $words }
}
Write-Output (ConvertTo-Json -Depth 5 -Compress @{ lang = $engine.RecognizerLanguage.LanguageTag; lines = $lines })
"""


class OcrError(RuntimeError):
    pass


def ocr_windows(png: bytes, lang: str = "", run=subprocess.run) -> dict:
    """Распознать текст на снимке встроенным OCR Windows. {"lang", "lines": [{text, words}]}."""
    fd, path = tempfile.mkstemp(suffix=".png", prefix="albion-ocr-")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(png)
        env = {**os.environ, "AT_OCR_IMAGE": str(Path(path).resolve()), "AT_OCR_LANG": lang}
        try:
            out = run(["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command",
                       OCR_SCRIPT], capture_output=True, text=True, timeout=60, env=env,
                      creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except FileNotFoundError:
            raise OcrError("PowerShell не найден — распознавание текста доступно только в Windows 10/11") from None
        except subprocess.TimeoutExpired:
            raise OcrError("распознавание текста не уложилось в минуту") from None
        text = (out.stdout or "").strip()
        if out.returncode != 0 or not text:
            raise OcrError("распознавание текста не сработало: " + ((out.stderr or "").strip()[:300] or "нет ответа"))
        try:
            data = json.loads(text.splitlines()[-1])
        except ValueError:
            raise OcrError("распознавание текста вернуло непонятный ответ") from None
        if data.get("error") == "no-ocr-language":
            raise OcrError("в Windows нет языка для распознавания текста — добавьте русский или английский язык "
                           "(Параметры → Время и язык → Язык)")
        lines = data.get("lines") or []
        if isinstance(lines, dict):
            lines = [lines]
        for ln in lines:
            if isinstance(ln.get("words"), dict):
                ln["words"] = [ln["words"]]
        return {"lang": data.get("lang", ""), "lines": lines}
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


# --- поиск подписей ----------------------------------------------------------------
def _spans(line: dict, n: int):
    """Подряд идущие группы по ``n`` слов строки: (текст, рамка x, y, w, h)."""
    words = line.get("words") or []
    for i in range(0, max(0, len(words) - n + 1)):
        group = words[i:i + n]
        x0 = min(w["x"] for w in group)
        y0 = min(w["y"] for w in group)
        x1 = max(w["x"] + w["w"] for w in group)
        y1 = max(w["y"] + w["h"] for w in group)
        yield " ".join(w["t"] for w in group), (x0, y0, x1 - x0, y1 - y0)


def find_points(lines: list[dict], width: int, height: int, scale: float = 1.0) -> list[dict]:
    """Найти точки интерфейса по распознанным строкам. Координаты — доли окна."""
    found = []
    used: list = []

    def overlaps(li, box):
        x, y, w, h = box
        return any(li == ul and x < ux + uw and ux < x + w and y < uy + uh and uy < y + h
                   for ul, (ux, uy, uw, uh) in used)
    # Длинные подписи раньше коротких: «Заказ на продажу» не должен съесть «Продать».
    order = sorted(LABELS.items(), key=lambda kv: -max(len(t) for t in kv[1]["texts"]))
    for name, spec in order:
        best = None
        for li, line in enumerate(lines):
            for label in spec["texts"]:
                n = max(1, len(label.split()))
                for text, box in _spans(line, n):
                    if overlaps(li, box):
                        continue
                    score = similarity(text, label)
                    if score >= MIN_SCORE and (best is None or score > best[0]):
                        best = (score, text, box, li)
        if best is None:
            continue
        score, text, (x, y, w, h), li = best
        used.append((li, (x, y, w, h)))
        if spec["kind"] == "field":
            px, py, approx = x + w + FIELD_OFFSET * h, y + h / 2, True
        else:
            px, py, approx = x + w / 2, y + h / 2, False
        found.append({"name": name, "x": round(min(1.0, px / scale / max(width - 1, 1)), 4),
                      "y": round(min(1.0, py / scale / max(height - 1, 1)), 4),
                      "text": text, "score": round(score, 2), "approx": approx})
    found.sort(key=lambda f: list(LABELS).index(f["name"]))
    return found


def probe_offsets(steps: int = 12) -> list[tuple[float, float]]:
    """Смещения пробных кликов вокруг персонажа (доли высоты окна): кольца от близких к дальним."""
    import math
    out = []
    for r in (0.05, 0.09, 0.13, 0.18):
        for i in range(steps):
            a = 2 * math.pi * i / steps
            out.append((round(r * math.cos(a), 4), round(r * math.sin(a), 4)))
    return out
