(function () {
  const BASE = "/plugins/propagandas-tc506/api";
  const CW = 480, CH = 272;
  const SNAP = 10;
  const $ = (id) => document.getElementById(id);
  const esc = (s) =>
    String(s == null ? "" : s)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;")
      .replace(/>/g, "&gt;").replace(/"/g, "&quot;");

  const estado = {
    template: null,
    selecionada: null,
    produto: null,
    codigo: "",
    meta: null,
    config: null,
    peers: [],
    midias: [],
    arraste: null,
    imgCache: {},
  };

  async function api(path, opts) {
    const r = await fetch(BASE + path, opts);
    const ct = r.headers.get("content-type") || "";
    if (ct.includes("application/json")) {
      const j = await r.json();
      if (!r.ok && j && j.detail) throw new Error(j.detail);
      return j;
    }
    if (!r.ok) throw new Error("HTTP " + r.status);
    return r;
  }

  function uid(prefix) {
    return (prefix || "c") + "_" + Math.random().toString(36).slice(2, 9);
  }

  function aviso(msg, erro) {
    if (window.TC && window.TC.aviso) window.TC.aviso(msg, !!erro);
    else console[erro ? "error" : "log"](msg);
  }

  /* ---------- abas ---------- */
  document.querySelectorAll(".prop-tab").forEach((btn) => {
    btn.addEventListener("click", () => {
      document.querySelectorAll(".prop-tab").forEach((b) => b.classList.remove("ativa"));
      document.querySelectorAll(".prop-painel").forEach((p) => p.classList.remove("ativo"));
      btn.classList.add("ativa");
      const p = $("painel-" + btn.dataset.tab);
      if (p) p.classList.add("ativo");
      if (btn.dataset.tab === "config") {
        carregarPeers();
        carregarMidias();
        carregarStatus();
      }
      if (btn.dataset.tab === "produtos") {
        renderProdutos();
        carregarGeradas();
      }
    });
  });

  /* ---------- template helpers ---------- */
  function tplAtual() {
    if (!estado.template) return null;
    estado.template.nome = ($("prop-tpl-nome") && $("prop-tpl-nome").value) || estado.template.nome;
    estado.template.cor_fundo = ($("prop-fundo") && $("prop-fundo").value) || estado.template.cor_fundo;
    estado.template.largura = CW;
    estado.template.altura = CH;
    return estado.template;
  }

  function parsePreco(v) {
    if (v == null || v === "") return NaN;
    if (typeof v === "number") return v;
    const s0 = String(v).replace(/R\$\s?/gi, "").trim();
    if (!s0) return NaN;
    // BR com milhar: 1.234,56
    if (s0.indexOf(",") >= 0 && s0.indexOf(".") >= 0) {
      if (s0.lastIndexOf(",") > s0.lastIndexOf("."))
        return parseFloat(s0.replace(/\./g, "").replace(",", "."));
      return parseFloat(s0.replace(/,/g, ""));
    }
    // vírgula decimal: 13,90
    if (s0.indexOf(",") >= 0) {
      const partes = s0.split(",");
      if (partes[partes.length - 1].length <= 2)
        return parseFloat(s0.replace(/\./g, "").replace(",", "."));
      return parseFloat(s0.replace(/,/g, ""));
    }
    // só ponto: 13.9 / 13.90 = decimal; 1.234 = milhar
    if (s0.indexOf(".") >= 0) {
      const partes = s0.split(".");
      if (partes.length === 2 && partes[1].length <= 2)
        return parseFloat(s0);
      if (partes.every((x) => /^\d+$/.test(x)))
        return parseFloat(s0.replace(/\./g, ""));
    }
    return parseFloat(s0.replace(/[^0-9.]/g, ""));
  }

  function formatPrecoBR(n) {
    if (isNaN(n)) return "—";
    const s = n.toFixed(2).replace(".", ",");
    const partes = s.split(",");
    partes[0] = partes[0].replace(/\B(?=(\d{3})+(?!\d))/g, ".");
    return "R$ " + partes.join(",");
  }

  function valorCampoHtml(campo) {
    const p = estado.produto || {};
    const placeholders = {
      barcode: "Código de barras",
      description: "Descrição do produto",
      price_1: "R$ 0,00",
      price_2: "R$ 0,00",
    };
    if (campo === "price_1" || campo === "price_2") {
      let bruto = p[campo];
      if (bruto == null || bruto === "") return esc(placeholders[campo]);
      let num = parsePreco(bruto);
      if (isNaN(num)) return esc(String(bruto));
      let suf = "";
      if (p.venda_peso) {
        const modo = p.preco_modo || "kg";
        if (modo === "100g") { num = num / 10; suf = "(100g)"; }
        else suf = "O kilo";
      }
      const preco = formatPrecoBR(num);
      if (suf) return esc(preco) + '<span class="fp-unid">' + esc(suf) + "</span>";
      return esc(preco);
    }
    if (campo === "description") {
      const v = p.description || "";
      return esc(v || placeholders.description);
    }
    if (campo === "barcode") {
      const v = p.barcode || estado.codigo || "";
      return esc(v || placeholders.barcode);
    }
    return esc(p[campo] || placeholders[campo] || "");
  }

  function srcImagem(c) {
    if (c.tipo === "image_custom") return c.src || "";
    if (c.tipo === "image_product" && estado.codigo) {
      const key = "prod:" + estado.codigo;
      return estado.imgCache[key] || BASE + "/imagem-produto?codigo=" + encodeURIComponent(estado.codigo);
    }
    return "";
  }

  function prefetchImg(codigo) {
    if (!codigo) return;
    const key = "prod:" + codigo;
    if (estado.imgCache[key]) return;
    const url = BASE + "/imagem-produto?codigo=" + encodeURIComponent(codigo);
    const img = new Image();
    img.onload = () => {
      estado.imgCache[key] = url;
      document.querySelectorAll(".prop-camada-img img[data-key='" + key + "']").forEach((el) => {
        el.src = url;
      });
    };
    img.src = url;
  }

  /* ---------- guias / snap ---------- */
  function limparGuias() {
    const folha = $("prop-folha");
    if (!folha) return;
    folha.querySelectorAll(".prop-guia").forEach((g) => g.remove());
  }

  function mostrarGuias(xs, ys) {
    const folha = $("prop-folha");
    if (!folha) return;
    limparGuias();
    const near = (a, b) => Math.abs(a - b) < 0.5;
    (xs || []).forEach((x) => {
      const g = document.createElement("div");
      g.className = "prop-guia prop-guia-v" + (near(x, CW / 2) ? " prop-guia-centro" : "");
      g.style.left = x + "px";
      g.style.height = CH + "px";
      folha.appendChild(g);
    });
    (ys || []).forEach((y) => {
      const g = document.createElement("div");
      g.className = "prop-guia prop-guia-h" + (near(y, CH / 2) ? " prop-guia-centro" : "");
      g.style.top = y + "px";
      g.style.width = CW + "px";
      folha.appendChild(g);
    });
  }

  function guiasAlvo(excetoId) {
    const xs = [0, CW / 2, CW];
    const ys = [0, CH / 2, CH];
    (estado.template.camadas || []).forEach((c) => {
      if (c.id === excetoId || c.visivel === false) return;
      const x = c.x || 0, y = c.y || 0, w = c.largura || 0, h = c.altura || 0;
      xs.push(x, x + w / 2, x + w);
      ys.push(y, y + h / 2, y + h);
    });
    return { xs, ys };
  }

  function snapValor(val, alvos) {
    const thr = SNAP;
    let best = val, dist = thr + 1, hit = null;
    alvos.forEach((a) => {
      const d = Math.abs(val - a);
      if (d < dist && d <= thr) { dist = d; best = a; hit = a; }
    });
    return { val: best, hit };
  }

  function aplicarGeomNoDom(c) {
    const el = document.querySelector('.prop-camada[data-id="' + c.id + '"]');
    if (!el) return;
    el.style.left = (c.x || 0) + "px";
    el.style.top = (c.y || 0) + "px";
    el.style.width = (c.largura || 10) + "px";
    el.style.height = (c.altura || 10) + "px";
  }

  /* ---------- lista / props / canvas ---------- */
  function renderListaCamadas() {
    const ul = $("prop-lista-camadas");
    if (!ul || !estado.template) return;
    const cams = (estado.template.camadas || []).slice().sort((a, b) => (b.z || 0) - (a.z || 0));
    ul.innerHTML = cams.map((c) => {
      const ativa = c.id === estado.selecionada ? " ativa" : "";
      return `<li class="${ativa}" data-id="${esc(c.id)}">
        <span class="meta">${esc(c.nome || c.tipo)} <span class="meta-img">(${esc(c.tipo)})</span></span>
        <button type="button" class="botao botao--fantasma" data-z="-1" title="Abaixo">↓</button>
        <button type="button" class="botao botao--fantasma" data-z="1" title="Acima">↑</button>
        <button type="button" class="botao botao--fantasma" data-rm title="Remover">×</button>
      </li>`;
    }).join("");
    ul.querySelectorAll("li").forEach((li) => {
      li.addEventListener("click", (e) => {
        if (e.target.closest("[data-z]") || e.target.closest("[data-rm]")) return;
        selecionar(li.dataset.id);
      });
      li.querySelectorAll("[data-z]").forEach((b) =>
        b.addEventListener("click", (e) => {
          e.stopPropagation();
          const c = (estado.template.camadas || []).find((x) => x.id === li.dataset.id);
          if (c) { c.z = (c.z || 0) + parseInt(b.dataset.z, 10); renderTudo(); }
        })
      );
      const rm = li.querySelector("[data-rm]");
      if (rm) rm.addEventListener("click", (e) => {
        e.stopPropagation();
        estado.template.camadas = (estado.template.camadas || []).filter((c) => c.id !== li.dataset.id);
        if (estado.selecionada === li.dataset.id) estado.selecionada = null;
        renderTudo();
      });
    });
  }

  function selecionar(id) {
    estado.selecionada = id;
    document.querySelectorAll(".prop-camada").forEach((el) => {
      const on = el.dataset.id === id;
      el.classList.toggle("selecionada", on);
      el.querySelectorAll(".handle").forEach((h) => h.remove());
      if (on) {
        ["nw", "ne", "sw", "se"].forEach((pos) => {
          const h = document.createElement("div");
          h.className = "handle handle-" + pos;
          h.dataset.handle = pos;
          el.appendChild(h);
        });
      }
    });
    renderListaCamadas();
    renderProps();
  }

  function renderProps() {
    const bar = $("prop-props-bar");
    const c = (estado.template.camadas || []).find((x) => x.id === estado.selecionada);
    if (!bar) return;
    if (!c) {
      bar.classList.remove("visivel");
      bar.innerHTML = '<span class="prop-props-vazio">Selecione um elemento no canvas para editar</span>';
      return;
    }
    bar.classList.add("visivel");
    const tipo = c.tipo;
    let html = `
      <div class="campo" style="min-width:7rem"><label>Nome</label><input class="prop-input" data-k="nome" value="${esc(c.nome || "")}"></div>
      <div class="campo" style="min-width:3.4rem"><label>X</label><input type="number" class="prop-input" data-k="x" value="${c.x|0}"></div>
      <div class="campo" style="min-width:3.4rem"><label>Y</label><input type="number" class="prop-input" data-k="y" value="${c.y|0}"></div>
      <div class="campo" style="min-width:3.4rem"><label>Larg.</label><input type="number" class="prop-input" data-k="largura" value="${c.largura|0}"></div>
      <div class="campo" style="min-width:3.4rem"><label>Alt.</label><input type="number" class="prop-input" data-k="altura" value="${c.altura|0}"></div>
      <label class="prop-check"><input type="checkbox" data-k="visivel" ${c.visivel !== false ? "checked" : ""}> Visível</label>`;
    if (tipo === "text") {
      html += `<div class="campo" style="min-width:9rem;flex:1"><label>Texto</label><input class="prop-input" data-k="texto" value="${esc(c.texto || "")}"></div>`;
    }
    if (tipo === "text_field") {
      const opts = ((estado.meta && estado.meta.campos_produto) || [])
        .map((f) => `<option value="${esc(f.id)}" ${c.campo === f.id ? "selected" : ""}>${esc(f.rotulo)}</option>`).join("");
      html += `<div class="campo" style="min-width:7.5rem"><label>Campo</label><select class="prop-select" data-k="campo">${opts}</select></div>`;
    }
    if (tipo === "text" || tipo === "text_field") {
      html += `
        <div class="campo" style="min-width:3.6rem"><label>Fonte</label><input type="number" class="prop-input" data-k="fonte_px" value="${c.fonte_px || 16}"></div>
        <div class="campo" style="min-width:2.4rem"><label>Cor</label><input type="color" data-k="cor" value="${esc(c.cor || "#ffffff")}"></div>
        <label class="prop-check"><input type="checkbox" data-k="negrito" ${c.negrito ? "checked" : ""}> Negrito</label>
        <div class="campo" style="min-width:6rem"><label>Alinhamento</label>
          <select class="prop-select" data-k="align">
            <option value="left" ${c.align === "left" ? "selected" : ""}>Esquerda</option>
            <option value="center" ${c.align === "center" ? "selected" : ""}>Centro</option>
            <option value="right" ${c.align === "right" ? "selected" : ""}>Direita</option>
          </select>
        </div>`;
    }
    if (tipo === "rect") {
      html += `
        <div class="campo" style="min-width:2.4rem"><label>Fundo</label><input type="color" data-k="cor_fundo" value="${esc(c.cor_fundo === "transparent" ? "#eeeeee" : c.cor_fundo || "#eeeeee")}"></div>
        <div class="campo" style="min-width:3.4rem"><label>Borda</label><input type="number" class="prop-input" data-k="borda_px" value="${c.borda_px || 0}"></div>
        <div class="campo" style="min-width:2.4rem"><label>Cor borda</label><input type="color" data-k="cor_borda" value="${esc(c.cor_borda === "transparent" ? "#000000" : c.cor_borda || "#000000")}"></div>`;
    }
    if (tipo === "image_product" || tipo === "image_custom") {
      const locked = c.trava_proporcao !== false;
      html += `
        <label class="prop-check"><input type="checkbox" data-k="trava_proporcao" ${locked ? "checked" : ""}> Proporção</label>
        <div class="campo" style="min-width:6.5rem"><label>Encaixe</label>
          <select class="prop-select" data-k="object_fit">
            <option value="contain" ${(c.object_fit || "contain") === "contain" ? "selected" : ""}>Contain</option>
            <option value="cover" ${c.object_fit === "cover" ? "selected" : ""}>Cover</option>
            <option value="fill_width" ${c.object_fit === "fill_width" ? "selected" : ""}>Largura</option>
            <option value="fill_height" ${c.object_fit === "fill_height" ? "selected" : ""}>Altura</option>
          </select>
        </div>`;
      if (tipo === "image_custom") {
        html += `<button type="button" class="botao botao--claro" id="prop-trocar-img" style="height:1.85rem;padding:.2rem .55rem;font-size:.78rem">Trocar imagem…</button>`;
      }
    }
    bar.innerHTML = html;
    bar.querySelectorAll("[data-k]").forEach((el) => {
      const apply = () => {
        const k = el.dataset.k;
        if (el.type === "checkbox") c[k] = el.checked;
        else if (el.type === "number") c[k] = parseInt(el.value, 10) || 0;
        else c[k] = el.value;
        if (k === "visivel" && !el.checked) c.visivel = false;
        if (["x", "y", "largura", "altura"].includes(k)) aplicarGeomNoDom(c);
        else renderCanvas();
        renderListaCamadas();
      };
      el.addEventListener("change", apply);
      el.addEventListener("input", apply);
    });
    const trocar = $("prop-trocar-img");
    if (trocar) trocar.onclick = () => uploadCustom((url) => { c.src = url; renderCanvas(); });
  }

  function renderCanvas() {
    const folha = $("prop-folha");
    if (!folha || !estado.template) return;
    folha.style.background = estado.template.cor_fundo || "#ffffff";
    const oldImgs = {};
    folha.querySelectorAll(".prop-camada-img img").forEach((img) => {
      if (img.dataset.key) oldImgs[img.dataset.key] = img;
    });
    const cams = (estado.template.camadas || []).slice().sort((a, b) => (a.z || 0) - (b.z || 0));
    folha.innerHTML = "";
    cams.forEach((c) => {
      if (c.visivel === false) return;
      const div = document.createElement("div");
      div.className = "prop-camada" + (c.id === estado.selecionada ? " selecionada" : "");
      div.dataset.id = c.id;
      div.style.left = (c.x || 0) + "px";
      div.style.top = (c.y || 0) + "px";
      div.style.width = (c.largura || 10) + "px";
      div.style.height = (c.altura || 10) + "px";
      div.style.zIndex = String(c.z || 0);

      if (c.tipo === "rect") {
        div.style.background = (!c.cor_fundo || c.cor_fundo === "transparent") ? "transparent" : c.cor_fundo;
        const bw = c.borda_px || 0;
        div.style.border = (!c.cor_borda || c.cor_borda === "transparent")
          ? "1px solid transparent"
          : (bw + "px solid " + c.cor_borda);
      } else if (c.tipo === "image_product" || c.tipo === "image_custom") {
        div.classList.add("prop-camada-img");
        const fit = (c.object_fit || "contain").toLowerCase();
        if (fit === "cover" || fit === "fill_height" || fit === "fill_width") div.classList.add("fit-cover");
        const src = srcImagem(c);
        const key = c.tipo === "image_product" ? "prod:" + (estado.codigo || "") : "custom:" + (c.src || c.id);
        if (src) {
          let img = oldImgs[key] ? oldImgs[key].cloneNode(true) : document.createElement("img");
          if (!oldImgs[key]) { img.alt = "img"; img.dataset.key = key; img.src = estado.imgCache[key] || src; }
          if (c.tipo === "image_product" && estado.codigo) prefetchImg(estado.codigo);
          img.onerror = () => {
            img.remove();
            div.textContent = "Sem imagem";
            div.style.color = "#999"; div.style.fontSize = "12px";
            div.style.display = "flex"; div.style.alignItems = "center"; div.style.justifyContent = "center";
          };
          div.appendChild(img);
        } else {
          div.textContent = c.tipo === "image_product" ? "Foto produto" : "Imagem";
          div.style.color = "#999"; div.style.fontSize = "12px";
          div.style.display = "flex"; div.style.alignItems = "center"; div.style.justifyContent = "center";
          div.style.background = "repeating-conic-gradient(#1a2438 0% 25%, #121a2b 0% 50%) 50% / 14px 14px";
        }
      } else {
        div.classList.add("prop-camada-text");
        if (c.tipo === "text_field") div.innerHTML = valorCampoHtml(c.campo || "description");
        else div.textContent = c.texto || "";
        div.style.color = (!c.cor || c.cor === "transparent") ? "transparent" : c.cor;
        div.style.fontWeight = c.negrito ? "700" : "400";
        div.style.fontSize = Math.max(8, c.fonte_px || 16) + "px";
        div.style.alignItems = c.align === "center" ? "center" : c.align === "right" ? "flex-end" : "flex-start";
        div.style.textAlign = c.align || "left";
      }

      if (c.id === estado.selecionada) {
        ["nw", "ne", "sw", "se"].forEach((pos) => {
          const h = document.createElement("div");
          h.className = "handle handle-" + pos;
          h.dataset.handle = pos;
          div.appendChild(h);
        });
      }
      div.addEventListener("mousedown", (ev) => iniciarArraste(ev, c));
      folha.appendChild(div);
    });
  }

  function iniciarArraste(ev, camada) {
    if (ev.button !== 0) return;
    // ignora se clique veio de input da lateral
    if (ev.target.closest && ev.target.closest(".prop-lateral")) return;
    ev.preventDefault();
    ev.stopPropagation();
    selecionar(camada.id);
    const handle = ev.target.dataset.handle || null;
    const isText = camada.tipo === "text" || camada.tipo === "text_field";
    const ratioLocked =
      (camada.tipo === "image_product" || camada.tipo === "image_custom") &&
      camada.trava_proporcao !== false
        ? (camada.largura || 10) / Math.max(1, camada.altura || 10)
        : null;
    estado.arraste = {
      id: camada.id,
      handle,
      startX: ev.clientX,
      startY: ev.clientY,
      origX: camada.x || 0,
      origY: camada.y || 0,
      origW: camada.largura || 10,
      origH: camada.altura || 10,
      origFonte: camada.fonte_px || 16,
      isText,
      ratio: ratioLocked,
    };

    const move = (e) => {
      const a = estado.arraste;
      if (!a) return;
      const dx = e.clientX - a.startX;
      const dy = e.clientY - a.startY;
      const c = (estado.template.camadas || []).find((x) => x.id === a.id);
      if (!c) return;
      const alvos = guiasAlvo(a.id);
      const hitX = [], hitY = [];

      if (a.handle) {
        let x = a.origX, y = a.origY, w = a.origW, h = a.origH;
        if (a.handle.includes("e")) w = a.origW + dx;
        if (a.handle.includes("s")) h = a.origH + dy;
        if (a.handle.includes("w")) { w = a.origW - dx; x = a.origX + dx; }
        if (a.handle.includes("n")) { h = a.origH - dy; y = a.origY + dy; }
        if (a.ratio) {
          if (Math.abs(dx) >= Math.abs(dy)) {
            h = w / a.ratio;
            if (a.handle.includes("n")) y = a.origY + a.origH - h;
          } else {
            w = h * a.ratio;
            if (a.handle.includes("w")) x = a.origX + a.origW - w;
          }
        }
        w = Math.max(8, w); h = Math.max(8, h);
        const sL = snapValor(x, alvos.xs);
        const sT = snapValor(y, alvos.ys);
        const sR = snapValor(x + w, alvos.xs);
        const sB = snapValor(y + h, alvos.ys);
        if (sL.hit != null) { w += x - sL.val; x = sL.val; hitX.push(sL.hit); }
        if (sT.hit != null) { h += y - sT.val; y = sT.val; hitY.push(sT.hit); }
        if (sR.hit != null) { w = sR.val - x; hitX.push(sR.hit); }
        if (sB.hit != null) { h = sB.val - y; hitY.push(sB.hit); }
        c.x = Math.round(x); c.y = Math.round(y);
        c.largura = Math.round(Math.max(8, w));
        c.altura = Math.round(Math.max(8, h));
        if (a.isText && a.origH > 0) {
          const scale = c.altura / a.origH;
          c.fonte_px = Math.round(Math.max(8, a.origFonte * scale));
          const el = document.querySelector('.prop-camada[data-id="' + c.id + '"]');
          if (el) el.style.fontSize = c.fonte_px + "px";
        }
      } else {
        let nx = a.origX + dx, ny = a.origY + dy;
        const w = a.origW, h = a.origH;
        const pageCx = CW / 2, pageCy = CH / 2;
        const thr = SNAP;
        const elCx = nx + w / 2, elCy = ny + h / 2;
        if (Math.abs(elCx - pageCx) <= thr) { nx = pageCx - w / 2; hitX.push(pageCx); }
        else {
          const sL = snapValor(nx, alvos.xs);
          const sC = snapValor(nx + w / 2, alvos.xs);
          const sR = snapValor(nx + w, alvos.xs);
          const candX = [
            { d: Math.abs(sC.val - (nx + w / 2)), v: sC.val - w / 2, hit: sC.hit },
            { d: Math.abs(sL.val - nx), v: sL.val, hit: sL.hit },
            { d: Math.abs(sR.val - (nx + w)), v: sR.val - w, hit: sR.hit },
          ].sort((aa, bb) => aa.d - bb.d)[0];
          if (candX.hit != null && candX.d <= thr) { nx = candX.v; hitX.push(candX.hit); }
        }
        if (Math.abs(elCy - pageCy) <= thr) { ny = pageCy - h / 2; hitY.push(pageCy); }
        else {
          const sT = snapValor(ny, alvos.ys);
          const sM = snapValor(ny + h / 2, alvos.ys);
          const sB = snapValor(ny + h, alvos.ys);
          const candY = [
            { d: Math.abs(sM.val - (ny + h / 2)), v: sM.val - h / 2, hit: sM.hit },
            { d: Math.abs(sT.val - ny), v: sT.val, hit: sT.hit },
            { d: Math.abs(sB.val - (ny + h)), v: sB.val - h, hit: sB.hit },
          ].sort((aa, bb) => aa.d - bb.d)[0];
          if (candY.hit != null && candY.d <= thr) { ny = candY.v; hitY.push(candY.hit); }
        }
        c.x = Math.round(nx); c.y = Math.round(ny);
      }
      aplicarGeomNoDom(c);
      mostrarGuias(hitX, hitY);
      const bar = $("prop-props-bar");
      if (bar && estado.selecionada === c.id) {
        bar.querySelectorAll("[data-k]").forEach((el) => {
          const k = el.dataset.k;
          if (["x", "y", "largura", "altura", "fonte_px"].includes(k) && el.type !== "checkbox") {
            el.value = c[k];
          }
        });
      }
    };

    const up = () => {
      estado.arraste = null;
      limparGuias();
      window.removeEventListener("mousemove", move);
      window.removeEventListener("mouseup", up);
      renderListaCamadas();
      renderProps();
    };
    window.addEventListener("mousemove", move);
    window.addEventListener("mouseup", up);
  }

  function renderTudo() {
    renderListaCamadas();
    renderCanvas();
    renderProps();
  }

  function addCamada(parcial) {
    const cams = estado.template.camadas || (estado.template.camadas = []);
    const z = cams.reduce((m, c) => Math.max(m, c.z || 0), 0) + 1;
    const c = Object.assign({
      id: uid("cam"), nome: parcial.tipo, x: 40, y: 40,
      largura: 160, altura: 40, z, visivel: true, trava_proporcao: true,
    }, parcial);
    cams.push(c);
    estado.selecionada = c.id;
    renderTudo();
  }

  function uploadCustom(cb) {
    $("prop-file-img").onchange = async (e) => {
      const f = e.target.files && e.target.files[0];
      if (!f) return;
      const fd = new FormData();
      fd.append("arquivo", f);
      try {
        const r = await fetch(BASE + "/upload-custom", { method: "POST", body: fd });
        const j = await r.json();
        if (!j.ok) throw new Error(j.detail || "Falha");
        cb(j.url);
      } catch (err) { aviso(err.message, true); }
      e.target.value = "";
    };
    $("prop-file-img").click();
  }

  $("prop-add-text") && ($("prop-add-text").onclick = () =>
    addCamada({ tipo: "text", texto: "Texto", fonte_px: 18, negrito: true, cor: "#ffffff", align: "center", nome: "Texto", altura: 36 }));
  $("prop-add-field") && ($("prop-add-field").onclick = () =>
    addCamada({ tipo: "text_field", campo: "description", fonte_px: 16, negrito: true, cor: "#e8eef8", align: "left", nome: "Campo", altura: 60 }));
  $("prop-add-cosmos") && ($("prop-add-cosmos").onclick = () =>
    addCamada({ tipo: "image_product", largura: 160, altura: 160, object_fit: "contain", nome: "Foto produto" }));
  $("prop-add-rect") && ($("prop-add-rect").onclick = () =>
    addCamada({ tipo: "rect", largura: 200, altura: 50, cor_fundo: "#1d6fe0", cor_borda: "transparent", borda_px: 0, nome: "Retângulo", trava_proporcao: false }));
  $("prop-add-img") && ($("prop-add-img").onclick = () =>
    uploadCustom((url) => addCamada({ tipo: "image_custom", src: url, largura: 160, altura: 120, object_fit: "contain", nome: "Imagem" })));

  $("prop-fundo") && $("prop-fundo").addEventListener("input", () => {
    if (estado.template) { estado.template.cor_fundo = $("prop-fundo").value; renderCanvas(); }
  });

  /* produto prévia — modal igual Gerador de Cartaz */
  let _modalProdOnSelect = null;

  function fecharModalProd() {
    const m = $("prop-modal-prod");
    if (m) m.hidden = true;
    _modalProdOnSelect = null;
  }

  function abrirModalProd(titulo, sub, itens, onSelect) {
    const m = $("prop-modal-prod");
    if (!m) return;
    _modalProdOnSelect = typeof onSelect === "function" ? onSelect : null;
    $("prop-modal-prod-titulo").textContent = titulo;
    $("prop-modal-prod-sub").textContent = sub || "";
    const ul = $("prop-prod-lista-modal");
    ul.innerHTML = (itens || []).map((it, i) => {
      const preco = it.price_1 != null && it.price_1 !== "" ? " · " + formatPrecoBR(parsePreco(it.price_1)) : "";
      const pesoTag = it.venda_peso ? " · peso/kg" : "";
      return `<li data-i="${i}">
        <span>${esc(it.description || "(sem descrição)")}${esc(preco)}${pesoTag}</span>
        <span class="ean">${esc(it.barcode || "")}</span>
      </li>`;
    }).join("") || '<li style="cursor:default;opacity:.7">Nenhum produto encontrado.</li>';
    ul.querySelectorAll("li[data-i]").forEach((li) => {
      li.addEventListener("click", () => {
        const it = itens[Number(li.dataset.i)];
        if (_modalProdOnSelect) _modalProdOnSelect(it);
        else aplicarProduto(it);
      });
    });
    m.hidden = false;
  }

  function urlPreviewProduto(codigo, precoModo, bust) {
    const modo = precoModo === "100g" ? "100g" : "kg";
    let u =
      BASE +
      "/preview-produto?codigo=" +
      encodeURIComponent(codigo || "") +
      "&preco_modo=" +
      encodeURIComponent(modo);
    if (bust) u += "&_=" + Date.now();
    return u;
  }

  function fecharModalPreview() {
    const m = $("prop-modal-preview");
    if (m) m.hidden = true;
  }

  function abrirModalPreview(it) {
    const m = $("prop-modal-preview");
    if (!m || !it || !it.barcode) return;
    const titulo = it.description || it.barcode;
    if ($("prop-modal-preview-titulo")) $("prop-modal-preview-titulo").textContent = "Prévia da propaganda";
    if ($("prop-modal-preview-sub")) {
      const preco =
        it.price_1 != null && it.price_1 !== ""
          ? formatPrecoBR(parsePreco(it.price_1))
          : "";
      $("prop-modal-preview-sub").textContent =
        (titulo || "") + (preco ? " · " + preco : "") + " · " + (it.barcode || "");
    }
    const img = $("prop-preview-img");
    if (img) {
      img.src = urlPreviewProduto(it.barcode, it.preco_modo || "kg");
      img.alt = titulo || it.barcode;
    }
    m.hidden = false;
  }

  async function buscarProduto() {
    const q = ($("prop-codigo") && $("prop-codigo").value || "").trim();
    if (!q) {
      $("prop-prod-status").textContent = "Informe o código (EAN, balança ou interno) ou a descrição.";
      return;
    }
    $("prop-prod-status").textContent = "Consultando…";
    try {
      // match direto
      try {
        const p = await api("/produto?codigo=" + encodeURIComponent(q));
        if (p.ok && p.produto) {
          aplicarProduto(p.produto);
          $("prop-prod-status").textContent = "Produto carregado.";
          return;
        }
      } catch (_) {}
      const r = await api("/buscar?q=" + encodeURIComponent(q) + "&limit=40");
      const itens = r.itens || [];
      if (itens.length === 1) {
        aplicarProduto(itens[0]);
        $("prop-prod-status").textContent = "Produto carregado.";
        return;
      }
      abrirModalProd(
        "Escolha o produto",
        itens.length + " resultado(s) para “" + q + "”",
        itens
      );
      $("prop-prod-status").textContent = itens.length + " resultado(s) — escolha na lista.";
    } catch (e) {
      $("prop-prod-status").textContent = e.message || String(e);
    }
  }

  function aplicarProduto(p) {
    estado.produto = p;
    estado.codigo = p.barcode || "";
    if ($("prop-codigo")) $("prop-codigo").value = estado.codigo;
    const precoTxt = p.price_1 != null ? formatPrecoBR(parsePreco(p.price_1)) : "—";
    $("prop-prod-status").textContent = (p.description || "") + " · " + precoTxt;
    prefetchImg(estado.codigo);
    renderCanvas();
    fecharModalProd();
  }

  $("prop-buscar") && ($("prop-buscar").onclick = buscarProduto);
  $("prop-codigo") && $("prop-codigo").addEventListener("keydown", (e) => { if (e.key === "Enter") buscarProduto(); });
  $("prop-modal-prod-cancel") && ($("prop-modal-prod-cancel").onclick = fecharModalProd);
  const _modalFundo = $("prop-modal-prod");
  if (_modalFundo) _modalFundo.addEventListener("click", (e) => { if (e.target === _modalFundo) fecharModalProd(); });

  $("prop-salvar-tpl") && ($("prop-salvar-tpl").onclick = async () => {
    try {
      await api("/template", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ template: tplAtual() }) });
      aviso("Template salvo.");
    } catch (e) { aviso(e.message, true); }
  });

  $("prop-reset-tpl") && ($("prop-reset-tpl").onclick = async () => {
    if (!confirm("Restaurar o template de exemplo?")) return;
    try {
      const r = await api("/template/reset", { method: "POST" });
      estado.template = r.template; estado.selecionada = null;
      if ($("prop-tpl-nome")) $("prop-tpl-nome").value = estado.template.nome || "";
      if ($("prop-fundo")) $("prop-fundo").value = estado.template.cor_fundo || "#ffffff";
      renderTudo();
      aviso("Template de exemplo restaurado.");
    } catch (e) { aviso(e.message, true); }
  });


  /* ---------- produtos / config ---------- */
  function itemPool(item) {
    if (item && typeof item === "object") {
      let modo = String(item.preco_modo || "kg").toLowerCase();
      if (modo !== "kg" && modo !== "100g") modo = "kg";
      return {
        barcode: String(item.barcode || item.codigo || "").trim(),
        description: String(item.description || item.descricao || "").trim(),
        price_1: item.price_1 != null ? item.price_1 : item.preco1,
        venda_peso: !!(item.venda_peso || item.by_weight),
        preco_modo: modo,
      };
    }
    return {
      barcode: String(item || "").trim(),
      description: "",
      price_1: null,
      venda_peso: false,
      preco_modo: "kg",
    };
  }

  function htmlItemPool(it, i, opts) {
    opts = opts || {};
    const preco =
      it.price_1 != null && it.price_1 !== ""
        ? formatPrecoBR(parsePreco(it.price_1))
        : "—";
    const tag =
      opts.origem === "aleatorio"
        ? '<span class="tag-rand">Aleatório</span>'
        : "";
    const modoBal =
      it.venda_peso && !opts.somenteLeitura
        ? `<div class="modo-balanca">
            <span class="cod">Balança</span>
            <select data-modo="${i}">
              <option value="kg" ${it.preco_modo === "kg" ? "selected" : ""}>O kilo</option>
              <option value="100g" ${it.preco_modo === "100g" ? "selected" : ""}>100 g</option>
            </select>
          </div>`
        : it.venda_peso
          ? `<div class="modo-balanca"><span class="cod">${it.preco_modo === "100g" ? "100 g" : "O kilo"}</span></div>`
          : "";
    const btn = opts.somenteLeitura
      ? ""
      : `<button type="button" class="botao botao--fantasma" data-i="${i}" style="padding:.2rem .45rem">×</button>`;
    const thumb =
      it.barcode
        ? `<img class="pool-thumb" data-prev="${esc(it.barcode)}" data-prev-modo="${esc(it.preco_modo || "kg")}" src="${esc(urlPreviewProduto(it.barcode, it.preco_modo || "kg"))}" alt="" title="Ver propaganda">`
        : "";
    return `<li>
      ${thumb}
      <div class="info">
        <span class="desc">${esc(it.description || "(sem descrição)")}</span>
        <span class="preco">${esc(preco)}</span>
        <span class="cod">${esc(it.barcode || "")}</span>
        ${tag}
        ${modoBal}
      </div>
      ${btn}
    </li>`;
  }

  function renderProdutos() {
    const ul = $("prop-lista-prod");
    if (!ul || !estado.config) return;
    const lista = (estado.config.produtos || []).map(itemPool);
    estado.config.produtos = lista;
    const ultimo = (estado.config.ultimo_pack || []).filter(
      (x) => (x.origem || "fixo") === "aleatorio"
    );
    if ($("prop-pool-count")) {
      const extra = ultimo.length
        ? " · " + ultimo.length + " aleatório(s) no último ciclo"
        : "";
      $("prop-pool-count").textContent = lista.length + " fixo(s) no pool" + extra;
    }
    let html =
      lista.map((it, i) => htmlItemPool(it, i, { origem: "fixo" })).join("") ||
      '<li class="meta-img" style="padding:.65rem">Nenhum produto fixo no pool.</li>';
    if (ultimo.length) {
      html +=
        '<li class="prop-pool-sub" style="list-style:none;border:none;padding:.65rem .7rem .2rem">No último ciclo (aleatórios)</li>' +
        ultimo
          .map((it) =>
            htmlItemPool(itemPool(it), -1, { origem: "aleatorio", somenteLeitura: true })
          )
          .join("");
    }
    ul.innerHTML = html;
    ul.querySelectorAll("[data-i]").forEach((b) => {
      b.onclick = () => {
        const i = parseInt(b.dataset.i, 10);
        if (isNaN(i) || i < 0) return;
        estado.config.produtos.splice(i, 1);
        renderProdutos();
      };
    });
    ul.querySelectorAll("[data-modo]").forEach((sel) => {
      sel.onchange = () => {
        const i = parseInt(sel.dataset.modo, 10);
        if (isNaN(i) || !estado.config.produtos[i]) return;
        estado.config.produtos[i].preco_modo = sel.value === "100g" ? "100g" : "kg";
        const img = sel.closest("li") && sel.closest("li").querySelector(".pool-thumb");
        if (img) {
          img.dataset.prevModo = estado.config.produtos[i].preco_modo;
          img.src = urlPreviewProduto(
            estado.config.produtos[i].barcode,
            estado.config.produtos[i].preco_modo,
            true
          );
        }
      };
    });
    ul.querySelectorAll(".pool-thumb[data-prev]").forEach((img) => {
      img.onclick = () => {
        const bc = img.dataset.prev;
        const modo = img.dataset.prevModo || "kg";
        const base =
          (estado.config.produtos || []).find((x) => itemPool(x).barcode === bc) ||
          (estado.config.ultimo_pack || []).find((x) => itemPool(x).barcode === bc) ||
          { barcode: bc, preco_modo: modo };
        abrirModalPreview(itemPool(Object.assign({}, base, { preco_modo: modo })));
      };
    });
    syncRandUIFromConfig();
    if ($("prop-pack-tam")) $("prop-pack-tam").value = estado.config.pack_tamanho || 10;
    if ($("prop-pack-ordem")) $("prop-pack-ordem").value = estado.config.pack_ordem || "sequencial";
    if ($("prop-tempo-exib")) $("prop-tempo-exib").value = estado.config.tempo_exibicao_s || 8;
    if ($("prop-int-val")) $("prop-int-val").value = estado.config.intervalo_valor || 15;
    if ($("prop-int-unid")) $("prop-int-unid").value = estado.config.intervalo_unidade || "horas";
    if ($("prop-midias-juntas")) $("prop-midias-juntas").checked = estado.config.midias_juntas !== false;
  }

  function syncRandUIFromConfig() {
    const modo = (estado.config && estado.config.modo_aleatorio) || "nenhum";
    const ativo = modo === "ean" || modo === "balanca" || modo === "ambos";
    if ($("prop-rand-ativo")) $("prop-rand-ativo").checked = ativo;
    if ($("prop-rand-opts")) $("prop-rand-opts").hidden = !ativo;
    if ($("prop-modo-rand")) {
      $("prop-modo-rand").value = ativo ? modo : "ambos";
    }
    if ($("prop-qtd-rand")) {
      const q = parseInt(estado.config && estado.config.qtd_aleatorio, 10);
      $("prop-qtd-rand").value = Math.max(1, isNaN(q) || q < 1 ? 5 : q);
    }
  }

  async function resolverProduto(codigo) {
    const p = await api("/produto?codigo=" + encodeURIComponent(codigo));
    if (p && p.ok && p.produto) {
      const it = itemPool(p.produto);
      if (!it.barcode) it.barcode = String(codigo).trim();
      return it;
    }
    return null;
  }

  async function adicionarProdutoAoPool(p) {
    if (!p) return;
    let item = itemPool(p);
    try {
      if (item.barcode) {
        const full = await resolverProduto(item.barcode);
        if (full && (full.description || full.price_1 != null)) {
          item = Object.assign({}, item, full, {
            preco_modo: item.preco_modo || full.preco_modo || "kg",
          });
        }
      }
    } catch (_) {}
    if (!estado.config.produtos) estado.config.produtos = [];
    const ja = estado.config.produtos.some((x) => itemPool(x).barcode === item.barcode);
    if (ja) {
      if ($("prop-pool-status")) $("prop-pool-status").textContent = "Já está no pool: " + (item.barcode || "");
      fecharModalProd();
      return;
    }
    estado.config.produtos.push(item);
    if ($("prop-add-cod")) $("prop-add-cod").value = "";
    if ($("prop-pool-status")) {
      $("prop-pool-status").textContent =
        "Adicionado: " + (item.description || item.barcode || "");
    }
    fecharModalProd();
    renderProdutos();
  }

  async function buscarParaPool() {
    const q = ($("prop-add-cod") && $("prop-add-cod").value || "").trim();
    if (!q) {
      if ($("prop-pool-status")) $("prop-pool-status").textContent = "Informe EAN, PLU ou descrição.";
      return;
    }
    if ($("prop-pool-status")) $("prop-pool-status").textContent = "Buscando…";
    try {
      const r = await api("/buscar?q=" + encodeURIComponent(q) + "&limit=40");
      const itens = r.itens || [];
      if (!itens.length) {
        if ($("prop-pool-status")) $("prop-pool-status").textContent = "Nenhum produto encontrado.";
        abrirModalProd("Adicionar ao pool", "Nenhum resultado para “" + q + "”", [], adicionarProdutoAoPool);
        return;
      }
      abrirModalProd(
        "Adicionar ao pool",
        itens.length + " resultado(s) para “" + q + "” — clique para adicionar",
        itens,
        adicionarProdutoAoPool
      );
      if ($("prop-pool-status")) {
        $("prop-pool-status").textContent = itens.length + " resultado(s) — escolha na lista.";
      }
    } catch (e) {
      if ($("prop-pool-status")) $("prop-pool-status").textContent = e.message || String(e);
    }
  }

  async function enriquecerPool() {
    if (!estado.config || !estado.config.produtos) return;
    let mudou = false;
    for (let i = 0; i < estado.config.produtos.length; i++) {
      const it = itemPool(estado.config.produtos[i]);
      if (!it.barcode) continue;
      if (it.description && it.price_1 != null) continue;
      try {
        const full = await resolverProduto(it.barcode);
        if (full && (full.description || full.price_1 != null)) {
          estado.config.produtos[i] = Object.assign({}, it, full, {
            preco_modo: it.preco_modo || full.preco_modo || "kg",
          });
          mudou = true;
        }
      } catch (_) {}
    }
    if (mudou) renderProdutos();
  }

  $("prop-add-prod") && ($("prop-add-prod").onclick = buscarParaPool);
  $("prop-add-cod") && $("prop-add-cod").addEventListener("keydown", (e) => { if (e.key === "Enter") buscarParaPool(); });
  $("prop-limpar-pool") && ($("prop-limpar-pool").onclick = () => {
    if (!confirm("Limpar todo o pool?")) return;
    estado.config.produtos = [];
    renderProdutos();
  });

  function aplicarRandToggle() {
    if (!estado.config) estado.config = {};
    const ativo = $("prop-rand-ativo") && $("prop-rand-ativo").checked;
    if ($("prop-rand-opts")) $("prop-rand-opts").hidden = !ativo;
    if (!ativo) {
      estado.config.modo_aleatorio = "nenhum";
    } else {
      let modo = ($("prop-modo-rand") && $("prop-modo-rand").value) || "ambos";
      if (modo === "nenhum") modo = "ambos";
      estado.config.modo_aleatorio = modo;
      let q = parseInt($("prop-qtd-rand") && $("prop-qtd-rand").value, 10);
      if (isNaN(q) || q < 1) q = 5;
      estado.config.qtd_aleatorio = Math.min(50, q);
      if ($("prop-qtd-rand")) $("prop-qtd-rand").value = estado.config.qtd_aleatorio;
      if ($("prop-modo-rand")) $("prop-modo-rand").value = modo;
    }
  }
  $("prop-rand-ativo") && ($("prop-rand-ativo").onchange = aplicarRandToggle);
  $("prop-modo-rand") && ($("prop-modo-rand").onchange = () => {
    if ($("prop-rand-ativo") && $("prop-rand-ativo").checked) aplicarRandToggle();
  });
  $("prop-qtd-rand") && $("prop-qtd-rand").addEventListener("change", () => {
    let q = parseInt($("prop-qtd-rand").value, 10);
    if (isNaN(q) || q < 1) q = 1;
    if (q > 50) q = 50;
    $("prop-qtd-rand").value = q;
    if (estado.config) estado.config.qtd_aleatorio = q;
  });

  $("prop-modal-preview-cancel") && ($("prop-modal-preview-cancel").onclick = fecharModalPreview);
  const _modalPrevFundo = $("prop-modal-preview");
  if (_modalPrevFundo) {
    _modalPrevFundo.addEventListener("click", (e) => {
      if (e.target === _modalPrevFundo) fecharModalPreview();
    });
  }

  function syncConfigFromUI() {
    if (!estado.config) estado.config = {};
    aplicarRandToggle();
    if ($("prop-qtd-rand")) {
      let q = parseInt($("prop-qtd-rand").value, 10);
      if (isNaN(q) || q < 1) q = 5;
      estado.config.qtd_aleatorio = Math.min(50, q);
    }
    if ($("prop-pack-tam")) estado.config.pack_tamanho = parseInt($("prop-pack-tam").value, 10) || 10;
    if ($("prop-pack-ordem")) estado.config.pack_ordem = $("prop-pack-ordem").value;
    if ($("prop-tempo-exib")) estado.config.tempo_exibicao_s = parseInt($("prop-tempo-exib").value, 10) || 8;
    if ($("prop-tempo-padrao")) estado.config.tempo_midia_padrao_s = parseInt($("prop-tempo-padrao").value, 10) || 5;
    if ($("prop-int-val")) estado.config.intervalo_valor = parseInt($("prop-int-val").value, 10) || 1;
    if ($("prop-int-unid")) estado.config.intervalo_unidade = $("prop-int-unid").value;
    estado.config.inspecionar_precos = true;
    if ($("prop-midias-juntas")) estado.config.midias_juntas = $("prop-midias-juntas").checked;
    estado.config.ativo = true;
    const checks = document.querySelectorAll("#prop-peers-lista input[type=checkbox]");
    if (checks.length) {
      estado.config.peers = Array.from(checks).filter((c) => c.checked).map((c) => c.value);
    }
  }

  /* ---- progresso de geração/envio ---- */
  let _pollProg = null;

  function mostrarProgresso(titulo) {
    const box = $("prop-progresso");
    if (!box) return;
    box.hidden = false;
    if ($("prop-progresso-titulo")) $("prop-progresso-titulo").textContent = titulo || "Gerando propagandas";
    if ($("prop-progresso-msg")) $("prop-progresso-msg").textContent = "Preparando…";
    if ($("prop-progresso-fill")) $("prop-progresso-fill").style.width = "0%";
    if ($("prop-progresso-pct")) $("prop-progresso-pct").textContent = "0%";
  }

  function atualizarProgressoUI(p) {
    if (!p) return;
    if ($("prop-progresso-msg")) $("prop-progresso-msg").textContent = p.msg || "…";
    const pct = Math.max(0, Math.min(100, parseInt(p.pct, 10) || 0));
    if ($("prop-progresso-fill")) $("prop-progresso-fill").style.width = pct + "%";
    if ($("prop-progresso-pct")) {
      const tot = p.total || 0;
      const cur = p.atual || 0;
      $("prop-progresso-pct").textContent = tot ? pct + "% · " + cur + "/" + tot : pct + "%";
    }
  }

  function esconderProgresso() {
    if (_pollProg) {
      clearInterval(_pollProg);
      _pollProg = null;
    }
    const box = $("prop-progresso");
    if (box) box.hidden = true;
  }

  function iniciarPollingProgresso() {
    if (_pollProg) clearInterval(_pollProg);
    _pollProg = setInterval(async () => {
      try {
        const r = await api("/progresso");
        atualizarProgressoUI(r);
        if (!r.em_andamento && (r.fase === "concluido" || r.fase === "erro" || r.fase === "idle")) {
          // mantém um instante para o usuário ver 100%
        }
      } catch (_) {}
    }, 400);
  }

  async function salvarConfig(opts) {
    opts = opts || {};
    syncConfigFromUI();
    const enviar = !!opts.enviar;
    if (enviar) {
      mostrarProgresso("Salvando e enviando");
      iniciarPollingProgresso();
    }
    try {
      const body = Object.assign({}, estado.config);
      if (enviar) body.enviar = true;
      const r = await api("/config", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      estado.config = r.config;
      renderProdutos();
      if (enviar && r.deploy) {
        atualizarProgressoUI({
          msg: r.deploy.detail || r.detail || "Concluído",
          pct: 100,
          atual: 1,
          total: 1,
          em_andamento: false,
          fase: r.deploy.ok ? "concluido" : "erro",
        });
      }
      const msg = r.detail || (enviar ? "Salvo e enviado." : "Configuração salva.");
      aviso(msg, r.deploy && r.deploy.ok === false);
      if (enviar) {
        carregarGeradas();
        carregarStatus();
        setTimeout(esconderProgresso, 700);
      }
      return true;
    } catch (e) {
      if (enviar) {
        atualizarProgressoUI({ msg: e.message, pct: 0, fase: "erro" });
        setTimeout(esconderProgresso, 1200);
      }
      aviso(e.message, true);
      return false;
    }
  }

  $("prop-salvar-prod") && ($("prop-salvar-prod").onclick = () => salvarConfig());
  $("prop-salvar-config") && ($("prop-salvar-config").onclick = () => {
    if (!confirm(
      "Isso apagará TODA a mídia nos terminais selecionados e enviará as novas propagandas.\n\n" +
      "Deseja continuar?"
    )) return;
    salvarConfig({ enviar: true });
  });

  async function gerarAgora() {
    if (!confirm(
      "Isso apagará TODA a mídia nos terminais selecionados e enviará as novas propagandas.\n\n" +
      "Deseja continuar?"
    )) return;
    await salvarConfig();
    const st = $("prop-gerar-status") || $("prop-status-box");
    mostrarProgresso("Gerando e enviando");
    iniciarPollingProgresso();
    if (st) st.textContent = "Gerando e enviando…";
    try {
      const r = await api("/gerar", { method: "POST" });
      atualizarProgressoUI({
        msg: r.detail || (r.ok ? "Concluído" : "Falha"),
        pct: 100,
        atual: 1,
        total: 1,
        fase: r.ok ? "concluido" : "erro",
      });
      if (st) st.textContent = r.detail || (r.ok ? "OK" : "Falha");
      aviso(r.detail || (r.ok ? "Gerado." : "Falha"), !r.ok);
      if (r.ultimo_pack) {
        estado.config = estado.config || {};
        estado.config.ultimo_pack = r.ultimo_pack;
        renderProdutos();
      }
      carregarGeradas();
      carregarStatus();
    } catch (e) {
      atualizarProgressoUI({ msg: e.message, pct: 0, fase: "erro" });
      if (st) st.textContent = e.message;
      aviso(e.message, true);
    } finally {
      setTimeout(esconderProgresso, 800);
    }
  }
  $("prop-gerar-agora") && ($("prop-gerar-agora").onclick = gerarAgora);

  async function carregarGeradas() {
    const box = $("prop-geradas");
    if (!box) return;
    try {
      const r = await api("/geradas");
      box.innerHTML = (r.arquivos || []).map((a) =>
        `<div class="prop-midia-card">
          <img src="${BASE}/geradas/${encodeURIComponent(a.nome)}" alt="">
          <div class="nome">${esc(a.nome)}</div>
        </div>`
      ).join("");
    } catch (_) {}
  }

  async function carregarPeers() {
    try {
      const r = await api("/peers");
      estado.peers = r.peers || [];
      const box = $("prop-peers-lista");
      if (!box) return;
      const sel = new Set(estado.config && estado.config.peers ? estado.config.peers : []);
      const todos = !sel.size;
      box.innerHTML = estado.peers.map((p) => {
        // id estável = MAC quando disponível (sobrevive a troca de IP)
        const id = p.id || p.mac || p.peer || "";
        const marcado =
          todos ||
          sel.has(id) ||
          sel.has(p.peer) ||
          (p.mac && sel.has(p.mac));
        const macTxt = p.mac ? p.mac : "(MAC pendente)";
        const modelo = p.nome_aparelho || p.model || "";
        return `<label class="prop-peer-item">
          <input type="checkbox" value="${esc(id)}" ${marcado ? "checked" : ""}>
          <span>
            <strong>${esc(macTxt)}</strong>
            <span class="meta-img">${esc(p.peer || "")}</span>
            ${modelo ? `<span class="meta-img">· ${esc(modelo)}</span>` : ""}
          </span>
        </label>`;
      }).join("") || '<p class="meta-img">Nenhum TC-506 conectado no SC504.</p>';
    } catch (e) { aviso(e.message, true); }
  }
  $("prop-peers-refresh") && ($("prop-peers-refresh").onclick = carregarPeers);
  $("prop-peers-todos") && ($("prop-peers-todos").onclick = () => {
    document.querySelectorAll("#prop-peers-lista input").forEach((c) => (c.checked = true));
  });
  $("prop-peers-nenhum") && ($("prop-peers-nenhum").onclick = () => {
    document.querySelectorAll("#prop-peers-lista input").forEach((c) => (c.checked = false));
  });

  async function carregarMidias() {
    try {
      const r = await api("/midias-padrao");
      estado.midias = r.midias || [];
      const box = $("prop-midia-lista");
      if (!box) return;
      const ativas = new Set(estado.config && estado.config.midias_padrao ? estado.config.midias_padrao : []);
      box.innerHTML = estado.midias.map((m) =>
        `<div class="prop-midia-card" data-nome="${esc(m.nome)}">
          <img src="${BASE}/midias-padrao/${encodeURIComponent(m.nome)}" alt="">
          <div class="nome">${esc(m.nome)}</div>
          <label class="prop-check" style="justify-content:center;font-size:.72rem">
            <input type="checkbox" data-mid="${esc(m.nome)}" ${ativas.has(m.nome) ? "checked" : ""}> usar
          </label>
          <button type="button" class="botao botao--fantasma" data-del="${esc(m.nome)}" style="width:100%;padding:.2rem;font-size:.72rem">Apagar</button>
        </div>`
      ).join("");
      box.querySelectorAll("[data-mid]").forEach((cb) => {
        cb.onchange = () => {
          if (!estado.config.midias_padrao) estado.config.midias_padrao = [];
          if (cb.checked) {
            if (!estado.config.midias_padrao.includes(cb.dataset.mid))
              estado.config.midias_padrao.push(cb.dataset.mid);
          } else {
            estado.config.midias_padrao = estado.config.midias_padrao.filter((x) => x !== cb.dataset.mid);
          }
        };
      });
      box.querySelectorAll("[data-del]").forEach((b) => {
        b.onclick = async () => {
          if (!confirm("Apagar " + b.dataset.del + "?")) return;
          await api("/midias-padrao/" + encodeURIComponent(b.dataset.del), { method: "DELETE" });
          carregarMidias();
        };
      });
    } catch (_) {}
  }

  function setupUpload() {
    const zone = $("prop-up-zone");
    const input = $("prop-up-file");
    if (!zone || !input) return;
    zone.onclick = () => input.click();
    zone.addEventListener("dragover", (e) => { e.preventDefault(); zone.classList.add("is-over"); });
    zone.addEventListener("dragleave", () => zone.classList.remove("is-over"));
    zone.addEventListener("drop", (e) => {
      e.preventDefault(); zone.classList.remove("is-over");
      if (e.dataTransfer.files[0]) enviarMidia(e.dataTransfer.files[0]);
    });
    input.onchange = () => { if (input.files[0]) enviarMidia(input.files[0]); input.value = ""; };
  }

  async function enviarMidia(file) {
    const fd = new FormData();
    fd.append("arquivo", file);
    try {
      const r = await fetch(BASE + "/midias-padrao/upload", { method: "POST", body: fd });
      const j = await r.json();
      if (!j.ok) throw new Error(j.detail || "Falha");
      if (!estado.config.midias_padrao) estado.config.midias_padrao = [];
      if (!estado.config.midias_padrao.includes(j.nome)) estado.config.midias_padrao.push(j.nome);
      aviso("Mídia enviada: " + j.nome);
      carregarMidias();
    } catch (e) { aviso(e.message, true); }
  }

  async function carregarStatus() {
    try {
      const r = await api("/status");
      const box = $("prop-status-box");
      if (!box) return;
      const un = r.intervalo_unidade || "horas";
      const iv = r.intervalo_valor != null ? r.intervalo_valor : Math.round((r.intervalo_s || 0) / 3600);
      box.innerHTML = `
        <div><strong>Ciclo automático:</strong> sempre ativo</div>
        <div><strong>Troca de pacote:</strong> a cada ${esc(String(iv))} ${esc(un)}</div>
        <div><strong>Inspeção de preço:</strong> ${r.inspecionar_precos !== false ? "ligada" : "desligada"}</div>
        <div><strong>Mídias juntas:</strong> ${r.midias_juntas !== false ? "sim" : "não"}</div>
        <div><strong>Última geração:</strong> ${esc(r.ultima_geracao || "—")}</div>
        <div><strong>Última atualização por preço:</strong> ${esc(r.ultima_inspecao_preco || "—")}</div>
        <div><strong>Peers conectados:</strong> ${r.peers_conectados}</div>
        <div><strong>Peers alvo:</strong> ${(r.peers_alvo || []).map(esc).join(", ") || "(todos / nenhum conectado)"}</div>
        <div style="margin-top:.35rem"><strong>Status:</strong> ${esc(r.ultimo_status || "—")}</div>`;
    } catch (e) {
      const box = $("prop-status-box");
      if (box) box.textContent = e.message;
    }
  }

  async function init() {
    try {
      estado.meta = await api("/meta");
      const t = await api("/template");
      estado.template = t.template || (estado.meta && estado.meta.template_padrao);
      if ($("prop-tpl-nome")) $("prop-tpl-nome").value = estado.template.nome || "";
      if ($("prop-fundo")) $("prop-fundo").value = estado.template.cor_fundo || "#ffffff";
      renderTudo();

      const cfg = await api("/config");
      estado.config = cfg.config || {};
      if ($("prop-tempo-padrao")) $("prop-tempo-padrao").value = estado.config.tempo_midia_padrao_s || 5;
      renderProdutos();
      enriquecerPool();
      setupUpload();
      carregarMidias();
      carregarGeradas();
      carregarStatus();
      renderProps();
    } catch (e) {
      console.error(e);
      aviso("Falha ao iniciar plugin: " + e.message, true);
    }
  }

  init();
})();
