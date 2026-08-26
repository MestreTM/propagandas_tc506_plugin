"""Plugin: Propagandas TC-506M — gera BMP 480×272 e envia aos terminais.

- Template com o mesmo modelo de camadas do Gerador de Cartaz (pixel, canvas fixo)
- Produtos manuais e/ou aleatórios (EAN, balança ou ambos)
- Intervalo de regeneração (minutos / horas / dias)
- Seleção de terminais SC504 + mídias padrão intercaladas
- Render Pillow → BMP, upload INT_MEM e playlist medias.conf
"""

from __future__ import annotations

import io
import json
import logging
import random
import re
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from fastapi import Body, Query, Request, UploadFile, File, Form
from fastapi.responses import HTMLResponse, JSONResponse, Response, FileResponse

log = logging.getLogger("arauto.plugin.propagandas")
_DIR = Path(__file__).resolve().parent

CANVAS_W = 480
CANVAS_H = 272
_ID_SEGURO = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
COSMOS_URL = "https://cdn-cosmos.bluesoft.com.br/products/{barcode}"

# --- estado em memória do agendador ---
_lock = threading.Lock()
_scheduler_stop = threading.Event()
_scheduler_thread: threading.Thread | None = None
_ctx_ref = None  # preenchido no setup

# progresso da geração/envio (lido pelo painel via polling)
_progress_lock = threading.Lock()
_progress: dict = {
    "em_andamento": False,
    "fase": "idle",
    "msg": "",
    "atual": 0,
    "total": 0,
    "pct": 0,
}


def _set_progress(*, fase: str | None = None, msg: str | None = None,
                  atual: int | None = None, total: int | None = None,
                  em_andamento: bool | None = None) -> None:
    with _progress_lock:
        if em_andamento is not None:
            _progress["em_andamento"] = bool(em_andamento)
        if fase is not None:
            _progress["fase"] = fase
        if msg is not None:
            _progress["msg"] = msg
        if atual is not None:
            _progress["atual"] = int(atual)
        if total is not None:
            _progress["total"] = int(total)
        tot = int(_progress.get("total") or 0)
        cur = int(_progress.get("atual") or 0)
        _progress["pct"] = min(100, int(round(100.0 * cur / tot))) if tot > 0 else (100 if not _progress["em_andamento"] and _progress["fase"] == "concluido" else 0)


def _get_progress() -> dict:
    with _progress_lock:
        return dict(_progress)


def _codigo_do_item(item) -> str:
    """Extrai código de item do pool (str legado ou dict)."""
    if isinstance(item, dict):
        return str(item.get("barcode") or item.get("codigo") or "").strip()
    return str(item or "").strip()


def _normalizar_produtos(lista) -> list[dict]:
    """Garante lista de {barcode, description, price_1, venda_peso, preco_modo}."""
    out: list[dict] = []
    vistos: set[str] = set()
    for item in lista or []:
        if isinstance(item, dict):
            bc = str(item.get("barcode") or item.get("codigo") or "").strip()
            if not bc or bc in vistos:
                continue
            vistos.add(bc)
            modo = str(item.get("preco_modo") or "kg").lower()
            if modo not in ("kg", "100g"):
                modo = "kg"
            out.append({
                "barcode": bc,
                "description": str(item.get("description") or item.get("descricao") or ""),
                "price_1": item.get("price_1") if item.get("price_1") is not None else item.get("preco1"),
                "venda_peso": bool(item.get("venda_peso") or item.get("by_weight")),
                "preco_modo": modo,
            })
        else:
            bc = str(item or "").strip()
            if not bc or bc in vistos:
                continue
            vistos.add(bc)
            out.append({
                "barcode": bc,
                "description": "",
                "price_1": None,
                "venda_peso": False,
                "preco_modo": "kg",
            })
    return out


def _page() -> str:
    return (_DIR / "page.html").read_text(encoding="utf-8")


def _pasta() -> Path:
    from arauto.core.settings import APP_DIR
    p = APP_DIR / "propagandas_tc506"
    p.mkdir(parents=True, exist_ok=True)
    return p


def _pasta_midia() -> Path:
    p = _pasta() / "midia"
    p.mkdir(parents=True, exist_ok=True)
    return p


def _pasta_geradas() -> Path:
    p = _pasta() / "geradas"
    p.mkdir(parents=True, exist_ok=True)
    return p


def _cfg_path() -> Path:
    return _pasta() / "config.json"


def _tpl_path() -> Path:
    return _pasta() / "template.json"


def _cfg_padrao() -> dict:
    return {
        "produtos": [],  # pool fixo [{barcode, description, price_1}]
        "modo_aleatorio": "nenhum",  # nenhum | ean | balanca | ambos
        "qtd_aleatorio": 5,  # min 1 quando ativo
        "pack_tamanho": 10,
        "pack_ordem": "sequencial",  # sequencial | aleatorio
        "pack_cursor": 0,
        "intervalo_valor": 15,
        "intervalo_unidade": "horas",  # minutos | horas | dias
        "tempo_exibicao_s": 8,
        "peers": [],  # vazio = todos SC504 conectados
        "midias_padrao": [],
        "tempo_midia_padrao_s": 5,
        "midias_juntas": True,  # exibe todas as mídias padrão em sequência entre props
        "inspecionar_precos": True,  # re-gera se preço do pack atual mudar
        "ativo": True,  # sempre ligado (legado; ignorado pelo scheduler)
        "ultima_geracao": None,
        "ultimo_status": "",
        "preco_modo": "kg",  # kg | 100g
        "ultimo_pack": [],  # [{barcode, description, price_1, origem}]
        "precos_snapshot": {},  # barcode -> preço normalizado na última geração
    }


def _carregar_cfg() -> dict:
    base = _cfg_padrao()
    path = _cfg_path()
    if path.is_file():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                base.update(data)
        except Exception:
            log.exception("ler config propagandas")
    # geração automática e inspeção de preço sempre ativas
    base["ativo"] = True
    base["inspecionar_precos"] = True
    if "midias_juntas" not in base:
        base["midias_juntas"] = True
    if not isinstance(base.get("ultimo_pack"), list):
        base["ultimo_pack"] = []
    if not isinstance(base.get("precos_snapshot"), dict):
        base["precos_snapshot"] = {}
    base["produtos"] = _normalizar_produtos(base.get("produtos") or [])
    return base


def _salvar_cfg(cfg: dict) -> None:
    cfg = dict(cfg or {})
    cfg["ativo"] = True
    cfg["inspecionar_precos"] = True
    _cfg_path().write_text(json.dumps(cfg, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _template_padrao() -> dict:
    """Modelo de exemplo 480×272 (coordenadas em pixels)."""
    return {
        "id": "padrao_480x272",
        "nome": "Propaganda 480×272",
        "largura": CANVAS_W,
        "altura": CANVAS_H,
        "cor_fundo": "#ffffff",
        "camadas": [
            {
                "id": "faixa-topo",
                "nome": "Faixa topo",
                "tipo": "rect",
                "x": 0, "y": 0, "largura": 480, "altura": 42,
                "cor_fundo": "#1d6fe0", "cor_borda": "transparent", "borda_px": 0,
                "z": 0, "visivel": True,
            },
            {
                "id": "titulo",
                "nome": "Título",
                "tipo": "text",
                "texto": "OFERTA",
                "x": 10, "y": 8, "largura": 460, "altura": 30,
                "fonte_px": 22, "negrito": True, "cor": "#ffffff", "align": "center",
                "z": 1, "visivel": True,
            },
            {
                "id": "foto",
                "nome": "Foto produto",
                "tipo": "image_product",
                "x": 16, "y": 52, "largura": 180, "altura": 180,
                "object_fit": "contain", "trava_proporcao": True,
                "z": 2, "visivel": True,
            },
            {
                "id": "desc",
                "nome": "Descrição",
                "tipo": "text_field",
                "campo": "description",
                "x": 210, "y": 60, "largura": 250, "altura": 70,
                "fonte_px": 16, "negrito": True, "cor": "#111111", "align": "left",
                "z": 3, "visivel": True,
            },
            {
                "id": "preco",
                "nome": "Preço",
                "tipo": "text_field",
                "campo": "price_1",
                "x": 210, "y": 140, "largura": 250, "altura": 70,
                "fonte_px": 28, "negrito": True, "cor": "#c62828", "align": "left",
                "z": 4, "visivel": True,
            },
            {
                "id": "ean",
                "nome": "Código",
                "tipo": "text_field",
                "campo": "barcode",
                "x": 210, "y": 230, "largura": 250, "altura": 28,
                "fonte_px": 12, "negrito": False, "cor": "#111111", "align": "left",
                "z": 5, "visivel": True,
            },
        ],
    }


def _carregar_template() -> dict:
    path = _tpl_path()
    if path.is_file():
        try:
            t = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(t, dict) and isinstance(t.get("camadas"), list):
                t.setdefault("largura", CANVAS_W)
                t.setdefault("altura", CANVAS_H)
                return t
        except Exception:
            log.exception("ler template")
    t = _template_padrao()
    _salvar_template(t)
    return t


def _salvar_template(tpl: dict) -> None:
    tpl = dict(tpl)
    tpl["largura"] = CANVAS_W
    tpl["altura"] = CANVAS_H
    if not tpl.get("id"):
        tpl["id"] = "padrao_480x272"
    _tpl_path().write_text(json.dumps(tpl, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


# --------------------------------------------------------------------------- produto helpers
def _sem_acentos(texto: str) -> str:
    import unicodedata
    t = unicodedata.normalize("NFD", texto or "")
    return "".join(c for c in t if unicodedata.category(c) != "Mn").lower()


def _parece_venda_peso(barcode: str, description: str) -> bool:
    bc = (barcode or "").strip()
    dig = "".join(c for c in bc if c.isdigit())
    if dig and len(dig) <= 7:
        return True
    if bc and len(bc) <= 7:
        return True
    desc = _sem_acentos(description or "")
    for token in (" kg", "kg ", "quilo", "balanca", "/kg", "por kg", "p/kg"):
        if token in f" {desc} ":
            return True
    if desc.endswith(" kg") or desc.endswith("/kg"):
        return True
    return False


def _produto_resumo(p) -> dict:
    if hasattr(p, "to_dict"):
        d = p.to_dict()
    else:
        d = {
            "barcode": getattr(p, "barcode", ""),
            "description": getattr(p, "description", ""),
            "price_1": getattr(p, "price1", None),
            "price_2": getattr(p, "price2", None),
        }
    barcode = d.get("barcode") or d.get("codigo_barras") or ""
    description = d.get("description") or d.get("descricao") or ""
    price_1 = d.get("price_1") if d.get("price_1") is not None else d.get("preco1")
    price_2 = d.get("price_2") if d.get("price_2") is not None else d.get("preco2")
    return {
        "barcode": barcode,
        "description": description,
        "price_1": price_1,
        "price_2": price_2,
        "venda_peso": _parece_venda_peso(str(barcode), str(description)),
    }


def _produto_dict(service, codigo: str) -> dict:
    codigo = (codigo or "").strip()
    if not codigo:
        return {"ok": False, "detail": "Código vazio."}
    try:
        if hasattr(service, "query"):
            r = service.query(codigo, channel="plugin_propagandas")
            d_raw = r.to_dict() if hasattr(r, "to_dict") else {}
            if not getattr(r, "found", False) and not d_raw.get("encontrado"):
                return {
                    "ok": False,
                    "detail": d_raw.get("mensagem") or f"Produto {codigo} não encontrado.",
                    "codigo": codigo,
                }
            d = {
                "barcode": d_raw.get("codigo_barras") or getattr(r, "barcode", codigo),
                "description": d_raw.get("descricao") or getattr(r, "description", "") or "",
                "price_1": d_raw.get("preco1") if d_raw.get("preco1") is not None else getattr(r, "price1", None),
                "price_2": d_raw.get("preco2") if d_raw.get("preco2") is not None else getattr(r, "price2", None),
                "by_weight": bool(getattr(r, "by_weight", False)),
            }
            d["venda_peso"] = bool(d["by_weight"]) or _parece_venda_peso(str(d["barcode"]), str(d["description"]))
            return {"ok": True, "produto": d, "codigo": codigo}
        repo = getattr(service, "repo", None)
        if repo and hasattr(repo, "get"):
            prod = repo.get(codigo)
            if not prod:
                return {"ok": False, "detail": f"Produto {codigo} não encontrado.", "codigo": codigo}
            return {"ok": True, "produto": _produto_resumo(prod), "codigo": codigo}
    except Exception as exc:
        log.exception("consulta produto")
        return {"ok": False, "detail": str(exc), "codigo": codigo}
    return {"ok": False, "detail": "Serviço indisponível.", "codigo": codigo}


def _cosmos_bytes(ean: str) -> bytes | None:
    from arauto.core.product_image import baixar_bytes, ean13

    code = ean13(ean) or "".join(c for c in (ean or "") if c.isdigit())
    if not code:
        return None
    candidatos = []
    if len(code) == 13:
        candidatos.extend([code, code.lstrip("0") or code])
    else:
        candidatos.extend([code, code.zfill(13)])
    seen: set[str] = set()
    for c in candidatos:
        if c in seen:
            continue
        seen.add(c)
        data = baixar_bytes(COSMOS_URL.format(barcode=c), timeout=8.0)
        if data and len(data) > 100:
            return data
    return None


def _imagem_produto_bytes(ean: str) -> bytes | None:
    """Local primeiro (cache ~/.arautopy/imagens), depois URL do sistema, Cosmos + grava local."""
    from arauto.core import product_image as pi

    local = pi.ler_imagem_local(ean)
    if local:
        return local
    try:
        data = pi.obter_bytes_produto(ean)
        if data:
            return data
    except Exception:
        log.debug("obter_bytes_produto falhou para %s", ean, exc_info=True)
    # fallback Cosmos e grava no cache local (mesmo fluxo do sistema)
    data = _cosmos_bytes(ean)
    if data:
        try:
            pi.salvar_cache_local(ean, data)
        except Exception:
            log.debug("salvar_cache_local falhou", exc_info=True)
        return data
    return None


def _parse_preco_num(val) -> float | None:
    """Interpreta preço BR/US sem transformar 13.9 em 139."""
    if val is None:
        return None
    if isinstance(val, (int, float)):
        return float(val)
    s = str(val).strip().replace("R$", "").replace("r$", "").strip()
    if not s:
        return None
    try:
        if "," in s and "." in s:
            # 1.234,56 (BR) ou 1,234.56 (US)
            if s.rfind(",") > s.rfind("."):
                s = s.replace(".", "").replace(",", ".")
            else:
                s = s.replace(",", "")
        elif "," in s:
            partes = s.split(",")
            if len(partes[-1]) <= 2:
                s = s.replace(".", "").replace(",", ".")
            else:
                s = s.replace(",", "")
        elif "." in s:
            partes = s.split(".")
            # 13.9 / 13.90 = decimal; 1.234 = milhar sem centavos
            if len(partes) == 2 and len(partes[-1]) <= 2:
                pass
            elif all(p.isdigit() for p in partes):
                s = s.replace(".", "")
        return float(s)
    except (TypeError, ValueError):
        return None


def _fmt_preco_br(n: float) -> str:
    return f"R$ {n:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def _valor_campo(produto: dict, campo: str) -> str:
    placeholders = {
        "barcode": "Código de barras",
        "description": "Descrição do produto",
        "price_1": "R$ 0,00",
        "price_2": "R$ 0,00",
    }
    if not produto:
        return placeholders.get(campo, "")
    mapa = {
        "barcode": ("barcode", "codigo", "ean", "gtin"),
        "description": ("description", "descricao", "nome", "name"),
        "price_1": ("price_1", "preco1", "price", "preco"),
        "price_2": ("price_2", "preco2"),
    }
    chaves = mapa.get(campo, (campo,))
    val = None
    for k in chaves:
        if k in produto and produto[k] is not None and str(produto[k]).strip() != "":
            val = produto[k]
            break
    if val is None:
        return placeholders.get(campo, "")
    if campo in ("price_1", "price_2", "price_1_fmt", "price_2_fmt"):
        n = _parse_preco_num(val)
        if n is None:
            return str(val)
        venda_peso = bool(produto.get("venda_peso") or produto.get("by_weight"))
        modo = (produto.get("preco_modo") or "kg").lower()
        sufixo = ""
        if venda_peso:
            if modo in ("100g", "100", "g", "grama", "gramas"):
                n = n / 10.0
                sufixo = "\n(100g)"
            else:
                sufixo = "\nO kilo"
        return _fmt_preco_br(n) + sufixo
    return str(val)


def _hex_rgb(cor: str, default=(0, 0, 0)) -> tuple[int, int, int]:
    cor = (cor or "").strip().lstrip("#")
    if len(cor) == 3:
        cor = "".join(c * 2 for c in cor)
    if len(cor) != 6:
        return default
    try:
        return int(cor[0:2], 16), int(cor[2:4], 16), int(cor[4:6], 16)
    except ValueError:
        return default


def _font_for(size_px: int, bold: bool = False):
    from PIL import ImageFont

    px = max(8, int(size_px))
    candidatos = [
        "C:/Windows/Fonts/arialbd.ttf" if bold else "C:/Windows/Fonts/arial.ttf",
        "C:/Windows/Fonts/segoeui.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if bold else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf" if bold else "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
    ]
    for path in candidatos:
        if Path(path).is_file():
            try:
                return ImageFont.truetype(path, px)
            except OSError:
                continue
    return ImageFont.load_default()


def _render_pil(template: dict, produto: dict, imagem: bytes | None):
    """Renderiza template 480×272 em RGB (coordenadas em pixels)."""
    from PIL import Image, ImageDraw

    W = int(template.get("largura") or CANVAS_W)
    H = int(template.get("altura") or CANVAS_H)
    fundo = _hex_rgb(template.get("cor_fundo") or "#0d1b2a", (13, 27, 42))
    img = Image.new("RGB", (W, H), fundo)
    draw = ImageDraw.Draw(img)

    camadas = sorted(template.get("camadas") or [], key=lambda c: int(c.get("z") or 0))
    for cam in camadas:
        if cam.get("visivel") is False:
            continue
        tipo = (cam.get("tipo") or "").lower()
        # aceita x/y em px (nativo) ou x_mm/y_mm legado (escala aproximada)
        if "x" in cam:
            x = int(cam.get("x") or 0)
            y = int(cam.get("y") or 0)
            w = max(1, int(cam.get("largura") or 10))
            h = max(1, int(cam.get("altura") or 10))
        else:
            # fallback mm → px assumindo 480≈127mm de largura visual
            scale = W / 210.0
            x = int(round(float(cam.get("x_mm") or 0) * scale))
            y = int(round(float(cam.get("y_mm") or 0) * scale))
            w = max(1, int(round(float(cam.get("largura_mm") or 10) * scale)))
            h = max(1, int(round(float(cam.get("altura_mm") or 10) * scale)))

        if tipo == "rect":
            cor_f = cam.get("cor_fundo") or "#eeeeee"
            cor_b = cam.get("cor_borda") or "#000000"
            fill = None if cor_f == "transparent" else _hex_rgb(cor_f, (238, 238, 238))
            outline = None if cor_b == "transparent" else _hex_rgb(cor_b, (0, 0, 0))
            bw = max(0, int(cam.get("borda_px") or 0))
            if fill is not None or outline is not None:
                draw.rectangle(
                    [x, y, x + w, y + h],
                    fill=fill,
                    outline=outline if (bw or outline) else None,
                    width=bw or 1,
                )
        elif tipo in ("text", "text_field"):
            if tipo == "text_field":
                texto = _valor_campo(produto, cam.get("campo") or "description")
            else:
                texto = str(cam.get("texto") or "")
            if not texto or (cam.get("cor") or "") == "transparent":
                continue
            cor = _hex_rgb(cam.get("cor") or "#ffffff")
            size_px = int(cam.get("fonte_px") or cam.get("fonte_mm") and float(cam["fonte_mm"]) * 3.8 or 16)
            bold = bool(cam.get("negrito"))
            font = _font_for(size_px, bold)
            max_w = w if w > 10 else W - x
            linhas: list[str] = []
            for paragrafo in texto.split("\n"):
                palavras = paragrafo.split(" ")
                atual = ""
                for p in palavras:
                    teste = (atual + " " + p).strip()
                    bbox = draw.textbbox((0, 0), teste, font=font)
                    if bbox[2] - bbox[0] <= max_w or not atual:
                        atual = teste
                    else:
                        linhas.append(atual)
                        atual = p
                if atual:
                    linhas.append(atual)
            line_h = int(getattr(font, "size", size_px) * 1.25)
            cy = y
            align = (cam.get("align") or "left").lower()
            font_unid = None
            for ln in linhas:
                eh_unid = ln.strip() in ("O kilo", "(100g)")
                f = font
                if eh_unid:
                    if font_unid is None:
                        font_unid = _font_for(max(10, int(size_px * 0.35)), True)
                    f = font_unid
                bbox = draw.textbbox((0, 0), ln, font=f)
                tw = bbox[2] - bbox[0]
                if align == "center":
                    tx = x + max(0, (w - tw) // 2)
                elif align == "right":
                    tx = x + max(0, w - tw)
                else:
                    tx = x
                draw.text((tx, cy), ln, fill=cor, font=f)
                cy += int((getattr(f, "size", size_px) * 1.15) if eh_unid else line_h)
                if cy > y + h + line_h:
                    break
        elif tipo in ("image_product", "image_custom"):
            raw = None
            if tipo == "image_custom":
                src = (cam.get("src") or "").strip().replace("\\", "/")
                if src.startswith("data:"):
                    try:
                        import base64
                        raw = base64.b64decode(src.split(",", 1)[-1])
                    except Exception:
                        raw = None
                elif "/api/midia/" in src or src.startswith("midia/"):
                    nome = src.rsplit("/", 1)[-1]
                    mid = _pasta_midia() / nome
                    if mid.is_file():
                        raw = mid.read_bytes()
                elif src.startswith("http://") or src.startswith("https://"):
                    from arauto.core.product_image import baixar_bytes
                    raw = baixar_bytes(src, timeout=10.0)
            else:
                raw = imagem
            if not raw:
                continue
            try:
                from PIL import Image as PILImage
                im = PILImage.open(io.BytesIO(raw)).convert("RGBA")
                if im.width < 1 or im.height < 1:
                    continue
                fit = (cam.get("object_fit") or "contain").lower()
                if fit in ("fill_height", "preencher_vertical", "height"):
                    scale = h / im.height
                elif fit in ("fill_width", "preencher_horizontal", "width"):
                    scale = w / im.width
                elif fit == "cover":
                    scale = max(w / im.width, h / im.height)
                else:
                    scale = min(w / im.width, h / im.height)
                nw = max(1, int(round(im.width * scale)))
                nh = max(1, int(round(im.height * scale)))
                im = im.resize((nw, nh), PILImage.Resampling.LANCZOS)
                px = int(round(x + (w - nw) / 2))
                py = int(round(y + (h - nh) / 2))
                left, top = max(0, px), max(0, py)
                right, bottom = min(W, px + nw), min(H, py + nh)
                if right > left and bottom > top:
                    sx0, sy0 = left - px, top - py
                    recorte = im.crop((sx0, sy0, sx0 + (right - left), sy0 + (bottom - top)))
                    if recorte.mode == "RGBA":
                        img.paste(recorte, (left, top), recorte)
                    else:
                        img.paste(recorte.convert("RGB"), (left, top))
            except Exception:
                log.debug("falha ao colar imagem", exc_info=True)
    return img


def _pil_to_bmp(pil) -> bytes:
    buf = io.BytesIO()
    pil.convert("RGB").save(buf, format="BMP")
    return buf.getvalue()


# --------------------------------------------------------------------------- seleção de produtos
def _listar_produtos_repo(service, limit: int = 5000) -> list:
    repo = getattr(service, "repo", None)
    if not repo:
        return []
    itens = []
    try:
        total = int(repo.count() or 0)
    except Exception:
        total = 0
    offset = 0
    page = 800
    while offset < max(total, page) and len(itens) < limit:
        try:
            batch = list(repo.search("", limit=page, offset=offset) or [])
        except Exception:
            break
        if not batch:
            break
        itens.extend(batch)
        if len(batch) < page:
            break
        offset += page
    return itens


def _pool_aleatorio_repo(service, modo: str, qtd: int, excluir: set[str] | None = None) -> list[str]:
    """Sorteia códigos do repositório (EAN / balança / ambos)."""
    excluir = excluir or set()
    qtd = max(0, min(int(qtd or 0), 50))
    if qtd <= 0 or modo not in ("ean", "balanca", "ambos"):
        return []
    todos = _listar_produtos_repo(service)
    candidatos: list[str] = []
    for p in todos:
        r = _produto_resumo(p)
        bc = (r.get("barcode") or "").strip()
        if not bc or bc in excluir:
            continue
        is_bal = bool(r.get("venda_peso"))
        dig = "".join(ch for ch in bc if ch.isdigit())
        is_ean = len(dig) in (8, 12, 13, 14) and not is_bal
        if modo == "ean" and is_ean:
            candidatos.append(bc)
        elif modo == "balanca" and is_bal:
            candidatos.append(bc)
        elif modo == "ambos":
            candidatos.append(bc)
    random.shuffle(candidatos)
    return candidatos[:qtd]


def _selecionar_codigos(service, cfg: dict) -> list[dict]:
    """Monta o pack do ciclo: pool fixo (+ aleatório) → fatia pack_tamanho.

    Retorna lista de {barcode, origem, preco_modo}.
    """
    pool_fixo: list[str] = []
    origem_map: dict[str, str] = {}
    modo_map: dict[str, str] = {}
    vistos: set[str] = set()
    for item in cfg.get("produtos") or []:
        c = _codigo_do_item(item)
        if c and c not in vistos:
            vistos.add(c)
            pool_fixo.append(c)
            origem_map[c] = "fixo"
            if isinstance(item, dict):
                m = str(item.get("preco_modo") or "kg").lower()
                modo_map[c] = m if m in ("kg", "100g") else "kg"
            else:
                modo_map[c] = "kg"

    pool = list(pool_fixo)
    modo = (cfg.get("modo_aleatorio") or "nenhum").lower()
    qtd_rand = max(0, min(int(cfg.get("qtd_aleatorio") or 0), 50))
    if modo in ("ean", "balanca", "ambos") and qtd_rand > 0:
        for bc in _pool_aleatorio_repo(service, modo, qtd_rand, vistos):
            if bc not in vistos:
                vistos.add(bc)
                pool.append(bc)
                origem_map[bc] = "aleatorio"
                modo_map[bc] = "kg"

    if not pool:
        return []

    pack = max(1, min(int(cfg.get("pack_tamanho") or len(pool)), 50))
    ordem = (cfg.get("pack_ordem") or "sequencial").lower()

    if ordem == "aleatorio":
        escolhidos = pool[:]
        random.shuffle(escolhidos)
        escolhidos = escolhidos[: min(pack, len(escolhidos))]
    else:
        cursor = int(cfg.get("pack_cursor") or 0) % len(pool)
        escolhidos = []
        for i in range(min(pack, len(pool))):
            escolhidos.append(pool[(cursor + i) % len(pool)])
        cfg["pack_cursor"] = (cursor + len(escolhidos)) % len(pool)

    return [
        {
            "barcode": c,
            "origem": origem_map.get(c, "fixo"),
            "preco_modo": modo_map.get(c, "kg"),
        }
        for c in escolhidos
    ]


def _preco_chave(val) -> str:
    """Chave estável para comparar preços entre inspeções."""
    n = _parse_preco_num(val)
    if n is None:
        return ""
    return f"{n:.4f}"


# --------------------------------------------------------------------------- deploy SC504
def _peers_alvo(cfg: dict) -> list[str]:
    """Peers ``ip:porta`` vivos que devem receber o deploy.

    A config pode guardar MAC (preferido) ou peer legado; resolve pelos dois.
    """
    from arauto.core import runtime

    vivos = runtime.peers_sc504()
    escolhidos = [str(x).strip() for x in (cfg.get("peers") or []) if str(x).strip()]
    if not escolhidos:
        return [p["peer"] for p in vivos if p.get("peer")]

    # normaliza escolhidos (MAC ou peer)
    def _chave(s: str) -> str:
        s = (s or "").strip()
        if ":" in s and len(s) >= 17 and s.count(":") == 5:
            return runtime._normalizar_mac(s)
        if "-" in s and len(s.replace("-", "")) == 12:
            return runtime._normalizar_mac(s)
        return s

    alvos = {_chave(x) for x in escolhidos}
    out: list[str] = []
    for p in vivos:
        peer = p.get("peer") or ""
        mac = runtime._normalizar_mac(p.get("mac") or "")
        pid = p.get("id") or mac or peer
        if peer in alvos or mac in alvos or pid in alvos or _chave(peer) in alvos:
            if peer and peer not in out:
                out.append(peer)
    return out


def _enviar_e_montar_playlist(peer: str, arquivos: list[tuple[str, bytes]], cfg: dict) -> str:
    """Envia BMPs + mídias padrão e grava medias.conf intercalando.

    arquivos: lista de (nome_arquivo, bytes_bmp)
    """
    from arauto.core import runtime
    from arauto.protocol import sc504_media as media

    conn = runtime.conexao_sc504(peer)
    if not conn:
        return f"{peer}: não conectado"

    # 0) limpa memória interna antes de subir (evita encher o terminal)
    try:
        if not conn.limpar_memoria_midias():
            log.warning("%s: limpeza de memória retornou falha — seguindo com upload", peer)
    except Exception:
        log.exception("%s: falha ao limpar memória de mídia", peer)

    # 1) envia propagas geradas
    paths_prop = []
    for nome, data in arquivos:
        path = "INT_MEM/" + nome
        if not conn.enviar_arquivo(path, data):
            return f"{peer}: falha ao enviar {nome}"
        paths_prop.append(path)

    # 2) envia mídias padrão (já no servidor)
    paths_padrao = []
    for nome in cfg.get("midias_padrao") or []:
        nome = Path(str(nome)).name
        if not re.match(r"^[A-Za-z0-9._-]+$", nome):
            continue
        local = _pasta_midia() / nome
        if not local.is_file():
            continue
        path = "INT_MEM/" + nome
        if conn.enviar_arquivo(path, local.read_bytes()):
            paths_padrao.append(path)

    # 3) inventário do zero (após limpeza) só com o que acabamos de subir
    try:
        nomes_ok = [Path(p).name for p in paths_prop + paths_padrao]
        itens = []
        for i, nome in enumerate(nomes_ok, 1):
            itens.append({
                "chave": f"media{i}",
                "arquivo": nome,
                "destino": "INT_MEM",
                "caminho": "INT_MEM/" + nome,
                "tipo": media.tipo_da_extensao(nome),
            })
        texto = media.montar_inventario(itens) or "[INT_MEM]\n"
        conn.enviar_arquivo(media.ARQ_INVENTARIO, texto.encode(media.CHARSET, errors="replace"))
    except Exception:
        log.exception("inventario %s", peer)

    # 4) playlist: prop ↔ padrão
    tempo_p = max(1, int(cfg.get("tempo_exibicao_s") or 8))
    tempo_m = max(1, int(cfg.get("tempo_midia_padrao_s") or 5))
    juntas = bool(cfg.get("midias_juntas", True))
    seq = []
    for i, path in enumerate(paths_prop):
        seq.append(media.ItemPlaylist(caminho=path, tempo=tempo_p, vezes=1))
        if paths_padrao:
            if juntas:
                # todas as mídias padrão em sequência entre uma prop e outra
                for pad in paths_padrao:
                    seq.append(media.ItemPlaylist(caminho=pad, tempo=tempo_m, vezes=1))
            else:
                pad = paths_padrao[i % len(paths_padrao)]
                seq.append(media.ItemPlaylist(caminho=pad, tempo=tempo_m, vezes=1))
    if not paths_prop and paths_padrao:
        for path in paths_padrao:
            seq.append(media.ItemPlaylist(caminho=path, tempo=tempo_m, vezes=1))

    if seq:
        texto = media.montar_playlist(seq)
        if not conn.enviar_arquivo(media.ARQ_PLAYLIST, texto.encode(media.CHARSET, errors="replace")):
            return f"{peer}: falha ao gravar medias.conf"
        conn.atualizar_midias()

    return f"{peer}: ok ({len(paths_prop)} prop + {len(paths_padrao)} padrao)"


def _gerar_e_enviar(
    service,
    cfg: dict | None = None,
    *,
    itens_pack: list[dict] | None = None,
    motivo: str = "ciclo",
) -> dict:
    """Gera BMPs dos produtos selecionados e envia aos peers.

    itens_pack: se informado, usa essa lista ({barcode, origem}) sem re-sortear.
    motivo: 'ciclo' | 'preco' | 'manual'
    """
    cfg = cfg or _carregar_cfg()
    tpl = _carregar_template()
    _set_progress(em_andamento=True, fase="selecao", msg="Selecionando produtos…", atual=0, total=0)

    if itens_pack is not None:
        selecionados = []
        for it in itens_pack:
            bc = str(it.get("barcode") or "").strip()
            if not bc:
                continue
            m = str(it.get("preco_modo") or "kg").lower()
            if m not in ("kg", "100g"):
                m = "kg"
            selecionados.append({
                "barcode": bc,
                "origem": it.get("origem") or "fixo",
                "preco_modo": m,
            })
    else:
        selecionados = _selecionar_codigos(service, cfg)

    if not selecionados:
        msg = "Nenhum produto para gerar (adicione códigos ou ative produtos aleatórios)."
        cfg["ultimo_status"] = msg
        cfg["ultima_geracao"] = time.strftime("%Y-%m-%d %H:%M:%S")
        _salvar_cfg(cfg)
        _set_progress(em_andamento=False, fase="erro", msg=msg, atual=0, total=0)
        return {"ok": False, "detail": msg, "arquivos": []}

    # mapa de preco_modo do pool fixo (fallback)
    pool_modo: dict[str, str] = {}
    for it in cfg.get("produtos") or []:
        if isinstance(it, dict):
            bc = _codigo_do_item(it)
            if bc:
                m = str(it.get("preco_modo") or "kg").lower()
                pool_modo[bc] = m if m in ("kg", "100g") else "kg"

    arquivos: list[tuple[str, bytes]] = []
    detalhes = []
    ultimo_pack: list[dict] = []
    snapshot: dict[str, str] = {}
    pasta = _pasta_geradas()
    for f in pasta.glob("prop_*.bmp"):
        try:
            f.unlink()
        except OSError:
            pass

    n_cod = len(selecionados)
    peers_prev = _peers_alvo(cfg)
    total_steps = n_cod + max(1, len(peers_prev))
    _set_progress(
        em_andamento=True, fase="gerando",
        msg=f"Gerando imagens (0/{n_cod})…",
        atual=0, total=total_steps,
    )

    for i, item in enumerate(selecionados):
        codigo = item["barcode"]
        origem = item.get("origem") or "fixo"
        preco_modo = item.get("preco_modo") or pool_modo.get(codigo) or "kg"
        _set_progress(
            fase="gerando",
            msg=f"Gerando imagem {i + 1}/{n_cod}: {codigo}",
            atual=i, total=total_steps,
        )
        prod_r = _produto_dict(service, codigo)
        if not prod_r.get("ok"):
            detalhes.append(f"{codigo}: não encontrado")
            continue
        produto = dict(prod_r["produto"])
        produto["preco_modo"] = preco_modo
        img_bytes = _imagem_produto_bytes(codigo)
        try:
            pil = _render_pil(tpl, produto, img_bytes)
            bmp = _pil_to_bmp(pil)
        except Exception as exc:
            log.exception("render %s", codigo)
            detalhes.append(f"{codigo}: render {exc}")
            continue
        nome = f"prop_{i+1:02d}_{_slug(codigo)}.bmp"
        (pasta / nome).write_bytes(bmp)
        arquivos.append((nome, bmp))
        detalhes.append(f"{codigo}: {nome} ({len(bmp)} bytes)")
        chave = _preco_chave(produto.get("price_1"))
        snapshot[codigo] = chave
        ultimo_pack.append({
            "barcode": codigo,
            "description": produto.get("description") or "",
            "price_1": produto.get("price_1"),
            "origem": origem,
            "venda_peso": bool(produto.get("venda_peso") or produto.get("by_weight")),
            "preco_modo": preco_modo,
        })
        _set_progress(atual=i + 1, msg=f"Gerada {i + 1}/{n_cod}: {codigo}")

    if not arquivos:
        msg = "Falha ao renderizar qualquer propaganda. " + "; ".join(detalhes)
        cfg["ultimo_status"] = msg
        cfg["ultima_geracao"] = time.strftime("%Y-%m-%d %H:%M:%S")
        _salvar_cfg(cfg)
        _set_progress(em_andamento=False, fase="erro", msg=msg)
        return {"ok": False, "detail": msg, "arquivos": []}

    peers = _peers_alvo(cfg)
    resultados = []
    base = n_cod
    if not peers:
        resultados.append("Nenhum terminal SC504 conectado/selecionado — imagens só geradas localmente.")
        _set_progress(
            fase="envio",
            msg="Imagens geradas (nenhum terminal conectado).",
            atual=total_steps, total=total_steps,
        )
    for pi, peer in enumerate(peers):
        _set_progress(
            fase="envio",
            msg=f"Enviando para terminal {pi + 1}/{len(peers)}: {peer}",
            atual=base + pi, total=total_steps,
        )
        try:
            resultados.append(_enviar_e_montar_playlist(peer, arquivos, cfg))
        except Exception as exc:
            log.exception("deploy %s", peer)
            resultados.append(f"{peer}: erro {exc}")
        _set_progress(atual=base + pi + 1)

    prefixo = {
        "ciclo": "Ciclo",
        "preco": "Preço alterado",
        "manual": "Manual",
    }.get(motivo, motivo)
    msg = f"{prefixo}: {len(arquivos)} propaganda(s). " + " | ".join(resultados)
    cfg["ultimo_pack"] = ultimo_pack
    cfg["precos_snapshot"] = snapshot
    cfg["ultimo_status"] = msg
    # só avança o relógio de ciclo em geração normal/manual (não em refresh de preço)
    if motivo != "preco":
        cfg["ultima_geracao"] = time.strftime("%Y-%m-%d %H:%M:%S")
    else:
        cfg["ultima_inspecao_preco"] = time.strftime("%Y-%m-%d %H:%M:%S")
    _salvar_cfg(cfg)
    _set_progress(em_andamento=False, fase="concluido", msg=msg, atual=total_steps, total=total_steps)
    return {
        "ok": True,
        "detail": msg,
        "arquivos": [{"nome": n, "bytes": len(b)} for n, b in arquivos],
        "peers": peers,
        "detalhes": detalhes,
        "ultimo_pack": ultimo_pack,
        "motivo": motivo,
    }


def _verificar_precos_e_atualizar(service, cfg: dict) -> dict | None:
    """Se algum preço do pack atual mudou, regenera e envia. Retorna resultado ou None."""
    if not cfg.get("inspecionar_precos", True):
        return None
    pack = cfg.get("ultimo_pack") or []
    if not pack:
        return None
    snapshot = cfg.get("precos_snapshot") or {}
    mudou: list[str] = []
    for it in pack:
        bc = str(it.get("barcode") or "").strip()
        if not bc:
            continue
        prod_r = _produto_dict(service, bc)
        if not prod_r.get("ok"):
            continue
        atual = _preco_chave(prod_r["produto"].get("price_1"))
        antigo = snapshot.get(bc)
        if antigo is not None and antigo != atual:
            mudou.append(bc)
    if not mudou:
        return None
    log.info("preços alterados em %s — regenerando pack", ", ".join(mudou))
    itens = []
    for it in pack:
        bc = str(it.get("barcode") or "").strip()
        if not bc:
            continue
        m = str(it.get("preco_modo") or "kg").lower()
        if m not in ("kg", "100g"):
            m = "kg"
        itens.append({
            "barcode": bc,
            "origem": it.get("origem") or "fixo",
            "preco_modo": m,
        })
    return _gerar_e_enviar(service, cfg, itens_pack=itens, motivo="preco")


def _slug(codigo: str) -> str:
    s = re.sub(r"[^A-Za-z0-9]+", "_", (codigo or ""))[:24]
    return s or "x"


def _intervalo_segundos(cfg: dict) -> int:
    v = max(1, int(cfg.get("intervalo_valor") or 1))
    u = (cfg.get("intervalo_unidade") or "horas").lower()
    if u in ("minuto", "minutos", "min"):
        return v * 60
    if u in ("dia", "dias", "d"):
        return v * 86400
    return v * 3600  # horas


def _scheduler_loop():
    log.info("agendador propagandas iniciado (sempre ativo)")
    while not _scheduler_stop.is_set():
        try:
            if _ctx_ref is not None:
                cfg = _carregar_cfg()
                # 1) inspeção de preços do pack atual (entre ciclos)
                try:
                    r = _verificar_precos_e_atualizar(_ctx_ref.service, cfg)
                    if r and r.get("ok"):
                        log.info("agendador: pack atualizado por mudança de preço")
                except Exception:
                    log.exception("agendador inspeção preços")

                # 2) ciclo completo pelo intervalo configurado
                cfg = _carregar_cfg()
                precisa = True
                ultima = cfg.get("ultima_geracao")
                if ultima:
                    try:
                        from datetime import datetime
                        t0 = datetime.strptime(ultima, "%Y-%m-%d %H:%M:%S")
                        decorrido = (datetime.now() - t0).total_seconds()
                        precisa = decorrido >= _intervalo_segundos(cfg)
                    except Exception:
                        precisa = True
                if precisa:
                    log.info("agendador: novo ciclo de propagandas…")
                    try:
                        _gerar_e_enviar(_ctx_ref.service, cfg, motivo="ciclo")
                    except Exception:
                        log.exception("agendador gerar")
        except Exception:
            log.exception("agendador loop")
        _scheduler_stop.wait(30)


def _garantir_scheduler():
    global _scheduler_thread
    with _lock:
        if _scheduler_thread and _scheduler_thread.is_alive():
            return
        _scheduler_stop.clear()
        _scheduler_thread = threading.Thread(target=_scheduler_loop, name="prop-tc506-sched", daemon=True)
        _scheduler_thread.start()


# --------------------------------------------------------------------------- setup
def setup(ctx):
    global _ctx_ref
    _ctx_ref = ctx
    _garantir_scheduler()

    ctx.adicionar_aba("propagandas-tc506", "Propagandas TC-506", "/plugins/propagandas-tc506/", ordem=42)

    @ctx.app.get("/plugins/propagandas-tc506/", response_class=HTMLResponse)
    def pagina(request: Request):
        scripts = '<script src="/plugins/propagandas-tc506/static/app.js"></script>'
        return ctx.render(
            request,
            titulo="Propagandas TC-506M",
            conteudo=_page(),
            pagina="propagandas-tc506",
            scripts=scripts,
        )

    @ctx.app.get("/plugins/propagandas-tc506/static/app.js")
    def static_js():
        return FileResponse(_DIR / "app.js", media_type="application/javascript")

    # ---- meta / template ----
    @ctx.app.get("/plugins/propagandas-tc506/api/meta")
    def api_meta():
        return {
            "ok": True,
            "canvas": {"largura": CANVAS_W, "altura": CANVAS_H},
            "campos_produto": [
                {"id": "barcode", "rotulo": "Código de barras"},
                {"id": "description", "rotulo": "Descrição"},
                {"id": "price_1", "rotulo": "Preço 1"},
                {"id": "price_2", "rotulo": "Preço 2"},
            ],
            "tipos_camada": [
                {"id": "text", "rotulo": "Texto livre"},
                {"id": "text_field", "rotulo": "Campo do produto"},
                {"id": "image_product", "rotulo": "Imagem Cosmos (EAN)"},
                {"id": "image_custom", "rotulo": "Imagem personalizada"},
                {"id": "rect", "rotulo": "Retângulo"},
            ],
            "template_padrao": _template_padrao(),
        }

    @ctx.app.get("/plugins/propagandas-tc506/api/template")
    def api_get_template():
        return {"ok": True, "template": _carregar_template()}

    @ctx.app.post("/plugins/propagandas-tc506/api/template")
    def api_salvar_template(corpo: dict = Body(...)):
        tpl = corpo.get("template") if isinstance(corpo, dict) else None
        if not isinstance(tpl, dict) or not isinstance(tpl.get("camadas"), list):
            return JSONResponse({"ok": False, "detail": "Template inválido."}, status_code=400)
        tpl["largura"] = CANVAS_W
        tpl["altura"] = CANVAS_H
        tpl["nome"] = (tpl.get("nome") or "Propaganda 480×272").strip()
        _salvar_template(tpl)
        return {"ok": True, "detail": "Template salvo."}

    @ctx.app.post("/plugins/propagandas-tc506/api/template/reset")
    def api_reset_template():
        t = _template_padrao()
        _salvar_template(t)
        return {"ok": True, "template": t}

    # ---- produto / preview ----
    @ctx.app.get("/plugins/propagandas-tc506/api/produto")
    def api_produto(codigo: str = Query("")):
        return _produto_dict(ctx.service, codigo)

    @ctx.app.get("/plugins/propagandas-tc506/api/buscar")
    def api_buscar(q: str = Query(""), limit: int = Query(30)):
        q = (q or "").strip()
        limit = max(1, min(int(limit or 30), 60))
        itens = []
        try:
            if q:
                dig = "".join(c for c in q if c.isdigit())
                # prioriza consulta completa (service.query) — descrição/preço corretos
                if dig and dig == q.replace(" ", ""):
                    for c in (q, dig, dig.zfill(13), dig.lstrip("0") or dig):
                        if not c:
                            continue
                        r = _produto_dict(ctx.service, c)
                        if r.get("ok") and r.get("produto"):
                            itens.append(r["produto"])
                            break
                if not itens:
                    repo = getattr(ctx.service, "repo", None)
                    if repo:
                        if dig:
                            for c in (q, dig, dig.zfill(13), dig.lstrip("0") or dig):
                                if not c:
                                    continue
                                p = repo.get(c)
                                if p:
                                    itens.append(_produto_resumo(p))
                                    break
                        if not itens:
                            for p in repo.search(q, limit=limit) or []:
                                itens.append(_produto_resumo(p))
                                if len(itens) >= limit:
                                    break
            else:
                repo = getattr(ctx.service, "repo", None)
                if repo:
                    for p in repo.search("", limit=limit) or []:
                        itens.append(_produto_resumo(p))
        except Exception as exc:
            return JSONResponse({"ok": False, "detail": str(exc), "itens": []}, status_code=500)
        return {"ok": True, "itens": itens[:limit]}

    @ctx.app.get("/plugins/propagandas-tc506/api/imagem-produto")
    def api_imagem_produto(codigo: str = Query("")):
        data = _imagem_produto_bytes(codigo)
        if not data:
            return JSONResponse({"ok": False, "detail": "Imagem não encontrada (local/Cosmos)."}, status_code=404)
        # detecta tipo aproximado
        mt = "image/jpeg"
        if data[:8] == b"\x89PNG\r\n\x1a\n":
            mt = "image/png"
        elif data[:2] == b"BM":
            mt = "image/bmp"
        return Response(content=data, media_type=mt)

    # alias legado
    @ctx.app.get("/plugins/propagandas-tc506/api/imagem-cosmos")
    def api_imagem_cosmos(codigo: str = Query("")):
        return api_imagem_produto(codigo)

    def _preview_png(codigo: str, preco_modo: str = "kg", tpl: dict | None = None) -> bytes:
        codigo = (codigo or "").strip()
        modo = (preco_modo or "kg").lower()
        if modo not in ("kg", "100g"):
            modo = "kg"
        if not isinstance(tpl, dict):
            tpl = _carregar_template()
        prod: dict = {}
        img_bytes = None
        if codigo:
            r = _produto_dict(ctx.service, codigo)
            prod = dict(r.get("produto") or {})
            prod["preco_modo"] = modo
            img_bytes = _imagem_produto_bytes(codigo)
        pil = _render_pil(tpl, prod, img_bytes)
        buf = io.BytesIO()
        pil.save(buf, format="PNG")
        return buf.getvalue()

    @ctx.app.post("/plugins/propagandas-tc506/api/preview")
    def api_preview(corpo: dict = Body(...)):
        tpl = corpo.get("template") if isinstance(corpo, dict) else None
        codigo = (corpo.get("codigo") or "").strip() if isinstance(corpo, dict) else ""
        modo = (corpo.get("preco_modo") if isinstance(corpo, dict) else None) or "kg"
        try:
            data = _preview_png(codigo, modo, tpl if isinstance(tpl, dict) else None)
            return Response(content=data, media_type="image/png")
        except Exception as exc:
            log.exception("preview")
            return JSONResponse({"ok": False, "detail": str(exc)}, status_code=500)

    @ctx.app.get("/plugins/propagandas-tc506/api/preview-produto")
    def api_preview_produto(
        codigo: str = Query(""),
        preco_modo: str = Query("kg"),
    ):
        """Prévia BMP/PNG do template atual + produto (para thumbs na pool)."""
        try:
            data = _preview_png(codigo, preco_modo, None)
            return Response(
                content=data,
                media_type="image/png",
                headers={"Cache-Control": "private, max-age=30"},
            )
        except Exception as exc:
            log.exception("preview-produto")
            return JSONResponse({"ok": False, "detail": str(exc)}, status_code=500)

    # ---- config ----
    @ctx.app.get("/plugins/propagandas-tc506/api/config")
    def api_get_config():
        return {"ok": True, "config": _carregar_cfg()}

    @ctx.app.post("/plugins/propagandas-tc506/api/config")
    async def api_salvar_config(corpo: dict = Body(...)):
        import asyncio

        cfg = _carregar_cfg()
        if not isinstance(corpo, dict):
            return JSONResponse({"ok": False, "detail": "JSON inválido."}, status_code=400)
        for k in (
            "produtos", "modo_aleatorio", "qtd_aleatorio", "pack_tamanho",
            "pack_ordem", "pack_cursor", "intervalo_valor", "intervalo_unidade",
            "tempo_exibicao_s", "peers", "midias_padrao", "tempo_midia_padrao_s",
            "midias_juntas", "inspecionar_precos", "preco_modo",
        ):
            if k in corpo:
                cfg[k] = corpo[k]
        # normaliza
        cfg["produtos"] = _normalizar_produtos(cfg.get("produtos") or [])
        modo_a = (cfg.get("modo_aleatorio") or "nenhum").lower()
        if modo_a not in ("ean", "balanca", "ambos", "nenhum"):
            modo_a = "nenhum"
        cfg["modo_aleatorio"] = modo_a
        qtd_a = int(cfg.get("qtd_aleatorio") or 5)
        if modo_a == "nenhum":
            cfg["qtd_aleatorio"] = max(1, min(qtd_a, 50))  # mantém valor para quando reativar
        else:
            cfg["qtd_aleatorio"] = max(1, min(qtd_a, 50))
        cfg["pack_tamanho"] = max(1, min(int(cfg.get("pack_tamanho") or 10), 50))
        ordem = (cfg.get("pack_ordem") or "sequencial").lower()
        cfg["pack_ordem"] = "aleatorio" if ordem == "aleatorio" else "sequencial"
        cfg["pack_cursor"] = max(0, int(cfg.get("pack_cursor") or 0))
        cfg["intervalo_valor"] = max(1, int(cfg.get("intervalo_valor") or 1))
        cfg["tempo_exibicao_s"] = max(1, int(cfg.get("tempo_exibicao_s") or 8))
        cfg["tempo_midia_padrao_s"] = max(1, int(cfg.get("tempo_midia_padrao_s") or 5))
        cfg["midias_juntas"] = bool(cfg.get("midias_juntas", True))
        cfg["inspecionar_precos"] = True
        cfg["peers"] = [str(x) for x in (cfg.get("peers") or []) if x]
        cfg["midias_padrao"] = [Path(str(x)).name for x in (cfg.get("midias_padrao") or [])]
        cfg["ativo"] = True
        enviar = bool(corpo.get("enviar") or corpo.get("deploy"))
        _salvar_cfg(cfg)
        resultado = {"ok": True, "config": cfg}
        if enviar:
            try:
                loop = asyncio.get_event_loop()
                ger = await loop.run_in_executor(
                    None, lambda: _gerar_e_enviar(ctx.service, cfg, motivo="manual")
                )
                resultado["deploy"] = ger
                resultado["detail"] = ger.get("detail") or "Config salva e enviada."
                if ger.get("ultimo_pack") is not None:
                    resultado["config"] = _carregar_cfg()
            except Exception as exc:
                log.exception("deploy ao salvar config")
                _set_progress(em_andamento=False, fase="erro", msg=str(exc))
                resultado["deploy"] = {"ok": False, "detail": str(exc)}
                resultado["detail"] = f"Config salva, mas falha no envio: {exc}"
        return resultado

    # ---- peers / midia padrão ----
    @ctx.app.get("/plugins/propagandas-tc506/api/peers")
    def api_peers():
        from arauto.core import runtime
        return {
            "peers": [
                {
                    "peer": p.get("peer") or "",
                    "mac": p.get("mac") or "",
                    "id": p.get("id") or p.get("mac") or p.get("peer") or "",
                    "model": p.get("modelo") or p.get("nome_aparelho") or "",
                    "nome_aparelho": p.get("nome_aparelho") or "",
                    "tipo": p.get("tipo"),
                }
                for p in runtime.peers_sc504()
            ]
        }

    @ctx.app.get("/plugins/propagandas-tc506/api/midias-padrao")
    def api_listar_midias():
        itens = []
        for f in sorted(_pasta_midia().iterdir()):
            if f.is_file() and f.suffix.lower() in (".bmp", ".jpg", ".jpeg", ".png", ".gif"):
                itens.append({"nome": f.name, "bytes": f.stat().st_size})
        return {"ok": True, "midias": itens}

    @ctx.app.post("/plugins/propagandas-tc506/api/midias-padrao/upload")
    async def api_upload_midia(arquivo: UploadFile = File(...)):
        from arauto.protocol import sc504_media as media
        nome = media.nome_seguro(Path(arquivo.filename or "midia.bin").name)
        data = await arquivo.read()
        if not data:
            return JSONResponse({"ok": False, "detail": "Arquivo vazio."}, status_code=400)
        if len(data) > 8 * 1024 * 1024:
            return JSONResponse({"ok": False, "detail": "Máximo 8 MB."}, status_code=400)
        path = _pasta_midia() / nome
        path.write_bytes(data)
        return {"ok": True, "nome": nome, "bytes": len(data)}

    @ctx.app.delete("/plugins/propagandas-tc506/api/midias-padrao/{nome}")
    def api_apagar_midia(nome: str):
        nome = Path(nome).name
        if not re.match(r"^[A-Za-z0-9._-]+$", nome):
            return JSONResponse({"ok": False, "detail": "Nome inválido."}, status_code=400)
        path = _pasta_midia() / nome
        if path.is_file():
            path.unlink()
        cfg = _carregar_cfg()
        cfg["midias_padrao"] = [m for m in (cfg.get("midias_padrao") or []) if m != nome]
        _salvar_cfg(cfg)
        return {"ok": True}

    @ctx.app.get("/plugins/propagandas-tc506/api/midias-padrao/{nome}")
    def api_servir_midia(nome: str):
        nome = Path(nome).name
        if not re.match(r"^[A-Za-z0-9._-]+$", nome):
            return JSONResponse({"ok": False, "detail": "Nome inválido."}, status_code=400)
        path = _pasta_midia() / nome
        if not path.is_file():
            return JSONResponse({"ok": False, "detail": "Não encontrado."}, status_code=404)
        ext = path.suffix.lower()
        mt = "image/bmp" if ext == ".bmp" else "image/jpeg" if ext in (".jpg", ".jpeg") else "image/png"
        return Response(content=path.read_bytes(), media_type=mt)

    @ctx.app.post("/plugins/propagandas-tc506/api/upload-custom")
    async def api_upload_custom(arquivo: UploadFile = File(...)):
        """Imagem custom para camada do template."""
        data = await arquivo.read()
        if not data or len(data) < 32:
            return JSONResponse({"ok": False, "detail": "Arquivo vazio."}, status_code=400)
        if len(data) > 8 * 1024 * 1024:
            return JSONResponse({"ok": False, "detail": "Máximo 8 MB."}, status_code=400)
        ext = Path(arquivo.filename or "img.png").suffix.lower() or ".png"
        if ext not in (".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"):
            ext = ".png"
        nome = uuid.uuid4().hex[:16] + ext
        (_pasta_midia() / nome).write_bytes(data)
        url = f"/plugins/propagandas-tc506/api/midias-padrao/{nome}"
        return {"ok": True, "url": url, "nome": nome}

    # ---- gerar / status ----
    @ctx.app.post("/plugins/propagandas-tc506/api/gerar")
    async def api_gerar():
        import asyncio

        try:
            loop = asyncio.get_event_loop()
            return await loop.run_in_executor(None, lambda: _gerar_e_enviar(ctx.service))
        except Exception as exc:
            log.exception("gerar")
            _set_progress(em_andamento=False, fase="erro", msg=str(exc))
            return JSONResponse({"ok": False, "detail": str(exc)}, status_code=500)

    @ctx.app.get("/plugins/propagandas-tc506/api/progresso")
    def api_progresso():
        return {"ok": True, **_get_progress()}

    @ctx.app.get("/plugins/propagandas-tc506/api/geradas")
    def api_geradas():
        itens = []
        for f in sorted(_pasta_geradas().glob("prop_*.bmp")):
            itens.append({"nome": f.name, "bytes": f.stat().st_size})
        return {"ok": True, "arquivos": itens}

    @ctx.app.get("/plugins/propagandas-tc506/api/geradas/{nome}")
    def api_gerada(nome: str):
        nome = Path(nome).name
        if not re.match(r"^[A-Za-z0-9._-]+$", nome):
            return JSONResponse({"ok": False, "detail": "Nome inválido."}, status_code=400)
        path = _pasta_geradas() / nome
        if not path.is_file():
            return JSONResponse({"ok": False, "detail": "Não encontrado."}, status_code=404)
        return Response(content=path.read_bytes(), media_type="image/bmp")

    @ctx.app.get("/plugins/propagandas-tc506/api/status")
    def api_status():
        cfg = _carregar_cfg()
        from arauto.core import runtime
        return {
            "ok": True,
            "ativo": True,
            "inspecionar_precos": bool(cfg.get("inspecionar_precos", True)),
            "midias_juntas": bool(cfg.get("midias_juntas", True)),
            "ultima_geracao": cfg.get("ultima_geracao"),
            "ultima_inspecao_preco": cfg.get("ultima_inspecao_preco"),
            "ultimo_status": cfg.get("ultimo_status"),
            "intervalo_s": _intervalo_segundos(cfg),
            "intervalo_valor": cfg.get("intervalo_valor"),
            "intervalo_unidade": cfg.get("intervalo_unidade"),
            "peers_conectados": len(runtime.peers_sc504()),
            "peers_alvo": _peers_alvo(cfg),
            "ultimo_pack": cfg.get("ultimo_pack") or [],
            "progresso": _get_progress(),
        }
