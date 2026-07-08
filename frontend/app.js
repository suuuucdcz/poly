// PolyQuant — dashboard mobile-first (3 moteurs : CONV / ARB / SWEEP)

if ("serviceWorker" in navigator) {
    window.addEventListener("load", () => {
        navigator.serviceWorker.register("/sw.js").catch(() => {});
    });
}

document.addEventListener("DOMContentLoaded", () => {
    const $ = (id) => document.getElementById(id);
    const fmt$ = (v) => (v === null || v === undefined || isNaN(v)) ? "—"
        : v.toLocaleString("fr-FR", { minimumFractionDigits: 2, maximumFractionDigits: 2 }) + " $";
    const fmtSigned = (v) => (v >= 0 ? "+" : "") + fmt$(v);
    const cls = (v) => v >= 0 ? "up" : "down";
    const esc = (s) => String(s ?? "").replace(/[&<>"]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

    // ===================== ÉTAT =====================
    let chart = null, sTotal = null, sCash = null, budgetLine = null;
    let initialBudget = 1000;
    let equityRows = [];          // [{time, value, cash}] — pour les boutons de période
    let chartRange = "all";
    let lastLogsKey = "", lastPosKey = "", lastTradesKey = "", lastEquityKey = "", lastWeatherKey = "";
    let journalFilter = "all";
    let cachedTrades = [], cachedTotals = null;
    let botRunning = null;

    // ===================== GRAPHIQUE =====================
    // Courbe « baseline » ancrée sur le budget initial : VERT au-dessus (gain),
    // ROUGE en dessous (perte) — lisible d'un coup d'œil, surtout sur téléphone.
    function initChart() {
        const el = $("equityChart");
        if (!el || typeof LightweightCharts === "undefined") return;
        chart = LightweightCharts.createChart(el, {
            layout: { background: { color: "transparent" }, textColor: "#5d6880", fontSize: 10 },
            grid: { vertLines: { visible: false }, horzLines: { color: "#182034" } },
            rightPriceScale: { borderVisible: false },
            timeScale: { borderVisible: false, timeVisible: true, secondsVisible: false },
            crosshair: { mode: 1 },
            autoSize: true,
            handleScroll: { mouseWheel: false, pressedMouseMove: true, horzTouchDrag: true, vertTouchDrag: false },
        });
        sTotal = chart.addBaselineSeries({
            baseValue: { type: "price", price: initialBudget },
            topLineColor: "#34d399",
            topFillColor1: "rgba(52,211,153,0.26)",
            topFillColor2: "rgba(52,211,153,0.02)",
            bottomLineColor: "#ff6b6b",
            bottomFillColor1: "rgba(255,107,107,0.02)",
            bottomFillColor2: "rgba(255,107,107,0.24)",
            lineWidth: 2,
            priceLineVisible: false, lastValueVisible: true,
        });
        sCash = chart.addLineSeries({
            color: "#5d6880", lineWidth: 1, lineStyle: 2,
            priceLineVisible: false, lastValueVisible: false,
        });
        budgetLine = sTotal.createPriceLine({
            price: initialBudget, color: "#5d6880", lineWidth: 1,
            lineStyle: LightweightCharts.LineStyle.Dashed, title: "budget",
        });
    }

    function setBudgetAnchor(budget) {
        if (!sTotal || !budget || budget === initialBudget) return;
        initialBudget = budget;
        sTotal.applyOptions({ baseValue: { type: "price", price: budget } });
        if (budgetLine) sTotal.removePriceLine(budgetLine);
        budgetLine = sTotal.createPriceLine({
            price: budget, color: "#5d6880", lineWidth: 1,
            lineStyle: LightweightCharts.LineStyle.Dashed, title: "budget",
        });
    }

    function applyChartRange() {
        if (!chart || !equityRows.length) return;
        if (chartRange === "all") { chart.timeScale().fitContent(); return; }
        const spans = { day: 86400, week: 7 * 86400 };
        const to = equityRows[equityRows.length - 1].time;
        const from = Math.max(equityRows[0].time, to - spans[chartRange]);
        chart.timeScale().setVisibleRange({ from, to });
    }

    // ===================== FETCHERS =====================
    async function jget(url) {
        const r = await fetch(url);
        if (!r.ok) throw new Error(url + " → " + r.status);
        return r.json();
    }

    async function fetchPortfolio() {
        try {
            const d = await jget("/api/portfolio");
            $("val-equity").textContent = fmt$(d.total_valuation);
            const roi = d.roi || 0;
            const roiEl = $("val-roi");
            roiEl.textContent = (roi >= 0 ? "+" : "") + roi.toFixed(2) + " %";
            roiEl.className = "pill mono " + cls(roi);
            $("chip-cash").textContent = fmt$(d.balance);
            const latent = (d.positions || []).reduce((a, p) => a + p.shares * (p.current_price - p.avg_price), 0);
            const latEl = $("chip-latent"); latEl.textContent = fmtSigned(latent); latEl.className = "chip-v mono " + cls(latent);
            if (cachedTotals) {
                const rEl = $("chip-realized");
                rEl.textContent = fmtSigned(cachedTotals.pnl_total);
                rEl.className = "chip-v mono " + cls(cachedTotals.pnl_total);
            }
            $("chip-win").textContent = d.win_rate != null ? d.win_rate.toFixed(0) + " %" : "—";
            setBudgetAnchor(d.initial_budget);
            renderPositions(d.positions || []);
        } catch (e) { /* réseau : on retentera */ }
    }

    async function fetchBotStatus() {
        try {
            const d = await jget("/api/bot/status");
            botRunning = !!d.is_running;
            $("bot-status-dot").className = "status-dot " + (botRunning ? "on" : "off");
            $("btn-toggle-bot").textContent = botRunning ? "■ Stop" : "▶ Start";
            if (d.tick_interval) $("input-interval").value = d.tick_interval;
            // Voyant sauvegarde : visible seulement si ça va mal (désactivée en
            // prod ou dernier essai en erreur -> un déploiement effacerait tout)
            const p = d.persistence || {};
            const badge = $("persist-badge");
            if (badge) {
                const broken = p.enabled === false || !!p.last_error;
                badge.hidden = !broken;
                badge.textContent = p.enabled === false ? "💾 sauvegarde OFF" : "💾 sauvegarde KO";
                badge.title = p.last_error || "Persistance Supabase non configurée : un déploiement remet le portefeuille à zéro.";
            }
        } catch (e) {}
    }

    async function fetchLogs() {
        try {
            const d = await jget("/api/logs");
            const key = JSON.stringify(d.logs?.slice(-3));
            if (key === lastLogsKey) return;
            lastLogsKey = key;
            const box = $("console-output");
            box.innerHTML = (d.logs || []).map(l => {
                const lv = (l.level || "info").toLowerCase();
                return `<div class="console-line ${esc(lv)}">[${esc(l.time?.slice(5, 16) || "")}] ${esc(l.message)}</div>`;
            }).join("") || '<div class="console-line system">Aucun log.</div>';
            box.scrollTop = box.scrollHeight;
        } catch (e) {}
    }

    async function fetchTrades() {
        try {
            const d = await jget("/api/trades");
            cachedTrades = d.trades || [];
            cachedTotals = d.totals || null;
            const key = JSON.stringify([cachedTrades.length, cachedTrades[0]?.id, journalFilter]);
            if (key === lastTradesKey) return;
            lastTradesKey = key;
            renderJournal();
        } catch (e) {}
    }

    async function fetchEquity() {
        try {
            const d = await jget("/api/equity-history");
            const h = d.history || [];
            const key = h.length + "|" + (h[h.length - 1]?.timestamp || "");
            if (key === lastEquityKey || !sTotal) return;
            lastEquityKey = key;
            const seen = new Set();
            const rows = [];
            for (const x of h) {
                const t = Math.floor(new Date(x.timestamp + "Z").getTime() / 1000);
                if (seen.has(t)) continue;
                seen.add(t);
                rows.push({ time: t, value: x.portfolio_value, cash: x.balance });
            }
            rows.sort((a, b) => a.time - b.time);
            equityRows = rows;
            sTotal.setData(rows.map(r => ({ time: r.time, value: r.value })));
            sCash.setData(rows.map(r => ({ time: r.time, value: r.cash })));
            applyChartRange();
        } catch (e) {}
    }

    async function fetchSignals() {
        try {
            const d = await jget("/api/signals");
            renderEngines(d);
            const w = d.weather || [];
            const key = JSON.stringify([d.updated_at, w.length]);
            if (key !== lastWeatherKey) {
                lastWeatherKey = key;
                renderWeather(w, d.updated_at);
            }
        } catch (e) {}
    }

    // ===================== MOTEURS =====================
    function renderEngines(d) {
        const learn = d.learning || {};
        $("engine-conv").textContent =
            `${learn.samples ?? 0} clôturés · ${learn.wins ?? 0} gagnés · P&L ${((learn.pnl ?? 0) >= 0 ? "+" : "") + (learn.pnl ?? 0).toFixed(2)} $`;
        const a = d.arb || {};
        $("engine-arb").textContent = a.ts
            ? `${a.baskets ?? 0} paniers · meilleur coût ${a.best_cost ?? "—"} · ${fmt$(a.open_cost ?? 0)} immobilisés`
            : "—";
        const s = d.sweep || {};
        $("engine-sweep").textContent = s.ts
            ? `${s.candidates ?? 0} candidats · ${s.bought ?? 0} raflés · ${fmt$(s.open_cost ?? 0)} immobilisés`
            : "—";
    }

    // ===================== POSITIONS (groupées) =====================
    function groupPositions(positions) {
        const baskets = new Map();
        const singles = [];
        for (const p of positions) {
            const q = p.question || "";
            const m = q.match(/^\[(ARB|SWEEP)\]\s*(.*?\?)\s*(.*)$/);
            if (m) {
                const key = m[1] + "|" + m[2];
                if (!baskets.has(key)) baskets.set(key, { tag: m[1], title: m[2], legs: [], cost: 0, value: 0, sets: 0 });
                const b = baskets.get(key);
                b.legs.push({ bucket: m[3] || "?", ...p });
                b.cost += p.shares * p.avg_price;
                b.value += p.shares * p.current_price;
                b.sets = Math.max(b.sets, p.shares);
            } else {
                singles.push(p);
            }
        }
        return { baskets: [...baskets.values()], singles };
    }

    function renderPositions(positions) {
        const key = JSON.stringify(positions.map(p => [p.token_id, p.shares, p.current_price]));
        if (key === lastPosKey) return;
        lastPosKey = key;

        const { baskets, singles } = groupPositions(positions);
        $("count-positions").textContent = (baskets.length + singles.length) || "";

        // --- paniers ARB / SWEEP ---
        const bc = $("baskets-container");
        if (!baskets.length) {
            bc.innerHTML = "";
        } else {
            bc.innerHTML = '<div class="section-label">Paniers tenus jusqu\'à la résolution</div>' +
                baskets.map(b => {
                    const isArb = b.tag === "ARB";
                    // ARB : payout garanti = sets × 1 $. SWEEP : payout attendu = parts × 1 $.
                    const payout = isArb ? b.sets : b.legs.reduce((a, l) => a + l.shares, 0);
                    const locked = payout - b.cost;
                    const legsHtml = b.legs
                        .slice().sort((x, y) => y.current_price - x.current_price)
                        .map(l => `<div class="leg-row ${l.current_price > 0.9 ? "winner" : ""}">
                            <span>${esc(l.bucket)}</span>
                            <span>${l.shares} × ${l.avg_price.toFixed(3)} → ${l.current_price.toFixed(3)}</span>
                        </div>`).join("");
                    return `<div class="card">
                        <div class="card-top">
                            <div>
                                <span class="tag tag-${isArb ? "arb" : "sweep"}">${b.tag}</span>
                                <div class="card-title">${esc(b.title)}</div>
                            </div>
                            <div class="card-pnl ${cls(locked)}">${fmtSigned(locked)}</div>
                        </div>
                        <div class="card-meta">
                            <span>Coût <b>${fmt$(b.cost)}</b></span>
                            <span>Payout ${isArb ? "garanti" : "visé"} <b>${fmt$(payout)}</b></span>
                            <span>${isArb ? "Profit verrouillé, résolution automatique" : "Gagnant déjà connu au capteur"}</span>
                        </div>
                        <details class="basket-legs">
                            <summary>▸ ${b.legs.length} tranches</summary>
                            ${legsHtml}
                        </details>
                    </div>`;
                }).join("");
        }

        // --- positions convergence ---
        const pc = $("positions-container");
        if (!singles.length) {
            pc.innerHTML = baskets.length ? "" : '<div class="empty">Aucune position ouverte.</div>';
            return;
        }
        pc.innerHTML = '<div class="section-label">Positions convergence (gérées par le bot)</div>' +
            singles.map(p => {
                const latent = p.shares * (p.current_price - p.avg_price);
                return `<div class="card">
                    <div class="card-top">
                        <div>
                            <span class="tag tag-conv">CONV</span>
                            <div class="card-title">${esc(p.question)}</div>
                        </div>
                        <div class="card-pnl ${cls(latent)}">${fmtSigned(latent)}</div>
                    </div>
                    <div class="card-meta">
                        <span>Parts <b>${p.shares}</b></span>
                        <span>PRU <b>${p.avg_price.toFixed(3)}</b></span>
                        <span>Prix <b>${p.current_price.toFixed(3)}</b></span>
                        <span>Valeur <b>${fmt$(p.shares * p.current_price)}</b></span>
                    </div>
                    <div class="card-actions">
                        <button class="btn btn-sm btn-danger-ghost" data-sell="${esc(p.token_id)}">Vendre au bid</button>
                    </div>
                </div>`;
            }).join("");

        pc.querySelectorAll("[data-sell]").forEach(btn => btn.addEventListener("click", async () => {
            if (!confirm("Vendre toute cette position au meilleur bid ?")) return;
            btn.disabled = true;
            try {
                const r = await fetch("/api/positions/sell", {
                    method: "POST", headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({ token_id: btn.getAttribute("data-sell") }),
                });
                const d = await r.json();
                if (!r.ok) alert(d.detail || "Vente impossible");
                lastPosKey = ""; fetchPortfolio(); fetchTrades();
            } catch (e) { alert("Erreur réseau"); btn.disabled = false; }
        }));
    }

    // ===================== MÉTÉO =====================
    function renderWeather(signals, updatedAt) {
        $("weather-updated").textContent = updatedAt
            ? "maj " + new Date(updatedAt * 1000).toLocaleTimeString("fr-FR", { hour: "2-digit", minute: "2-digit" }) : "";
        const box = $("weather-container");
        if (!signals.length) { box.innerHTML = '<div class="empty">Aucun signal pour l\'instant.</div>'; return; }
        box.innerHTML = signals.slice(0, 30).map(s => {
            const badges = [];
            if (s.realized != null) badges.push(`<span class="wbadge ${s.realized_src === "metar" ? "good" : ""}">R ${s.realized}°${esc(s.unit)} · ${esc(s.realized_src || "?")}</span>`);
            if (s.local_hour != null) badges.push(`<span class="wbadge">${String(Math.floor(s.local_hour)).padStart(2, "0")}h${String(Math.round((s.local_hour % 1) * 60)).padStart(2, "0")} locale</span>`);
            if (s.is_today) badges.push(`<span class="wbadge ${s.stable ? "good" : "warn"}">${s.stable ? "pic confirmé" : "en chauffe"}</span>`);
            if (s.action) badges.push(`<span class="wbadge good">${esc(s.action)}</span>`);
            const rows = (s.buckets || []).slice(0, 4).map(b => {
                const p = b.p != null ? b.p : 0;
                const edge = b.edge;
                return `<div class="bucket-row">
                    <span class="lab ${b.held_shares > 0 ? "held" : ""}">${b.held_shares > 0 ? "◆ " : ""}${esc(b.label)}</span>
                    <span class="num">${b.p != null ? (p * 100).toFixed(0) + "%" : "—"}</span>
                    <span class="num">${(b.price * 100).toFixed(0)}¢</span>
                    <span class="num ${edge != null && edge > 0 ? "edge-pos" : "edge-neg"}">${edge != null ? (edge >= 0 ? "+" : "") + (edge * 100).toFixed(0) : "—"}</span>
                    <span class="bar"><i style="width:${Math.min(100, p * 100)}%"></i></span>
                </div>`;
            }).join("");
            return `<div class="card">
                <div class="wx-head">
                    <span class="wx-city">${esc(s.city)}</span>
                    <span class="wbadge">${esc(s.date || "")}</span>
                </div>
                <div class="wx-badges">${badges.join("")}</div>
                <div class="bucket-head" style="margin-top:8px"><span>Tranche</span><span style="text-align:right">Modèle</span><span style="text-align:right">Marché</span><span style="text-align:right">Edge</span><span></span></div>
                ${rows}
            </div>`;
        }).join("");
    }

    // ===================== JOURNAL =====================
    function renderJournal() {
        const t = cachedTotals;
        if (t) {
            $("journal-totals").innerHTML = `
                <div class="tot"><div class="k">P&L réalisé</div><div class="v ${cls(t.pnl_total)}">${fmtSigned(t.pnl_total)}</div></div>
                <div class="tot"><div class="k">Ventes</div><div class="v ${cls(t.pnl_sells)}">${fmtSigned(t.pnl_sells)}</div></div>
                <div class="tot"><div class="k">Résolutions</div><div class="v ${cls(t.pnl_resolves)}">${fmtSigned(t.pnl_resolves)}</div></div>
                <div class="tot"><div class="k">Gagnés / clôturés</div><div class="v">${t.wins} / ${t.closed}</div></div>`;
        }
        const box = $("journal-container");
        let rows = cachedTrades;
        if (journalFilter === "closed") rows = rows.filter(x => x.action !== "BUY");
        if (journalFilter === "buy") rows = rows.filter(x => x.action === "BUY");
        if (!rows.length) { box.innerHTML = '<div class="empty">Aucun trade.</div>'; return; }
        box.innerHTML = rows.slice(0, 80).map(x => {
            const q = String(x.question || "").replace(/^\[(ARB|SWEEP)\]\s*/, m => m.trim() + " ");
            const pnl = x.pnl != null
                ? `<div class="trade-pnl ${cls(x.pnl)}">${(x.pnl >= 0 ? "+" : "") + x.pnl.toFixed(2)} $</div>` : "";
            return `<div class="trade-row">
                <span class="trade-act act-${esc(x.action)}">${esc(x.action)}</span>
                <span class="trade-q">${esc(q)}</span>
                <div class="trade-nums">
                    <div class="trade-px">${x.shares} × ${x.price.toFixed(3)}</div>
                    ${pnl}
                </div>
                <span class="trade-time">${esc((x.timestamp || "").slice(5, 16).replace("T", " "))}</span>
            </div>`;
        }).join("");
    }

    // ===================== NAVIGATION / ACTIONS =====================
    document.querySelectorAll(".tab-btn").forEach(btn => btn.addEventListener("click", () => {
        document.querySelectorAll(".tab-btn").forEach(b => b.classList.toggle("active", b === btn));
        const tab = btn.getAttribute("data-tab");
        document.querySelectorAll(".tab-panel").forEach(p => p.classList.toggle("active", p.id === "tab-" + tab));
        window.dispatchEvent(new Event("resize"));
    }));

    document.querySelectorAll("#journal-filters .fchip").forEach(chip => chip.addEventListener("click", () => {
        document.querySelectorAll("#journal-filters .fchip").forEach(c => c.classList.toggle("active", c === chip));
        journalFilter = chip.getAttribute("data-f");
        lastTradesKey = "";
        renderJournal();
    }));

    document.querySelectorAll("#chart-ranges .fchip").forEach(chip => chip.addEventListener("click", () => {
        document.querySelectorAll("#chart-ranges .fchip").forEach(c => c.classList.toggle("active", c === chip));
        chartRange = chip.getAttribute("data-range");
        applyChartRange();
    }));

    $("btn-toggle-bot").addEventListener("click", async () => {
        const url = botRunning ? "/api/bot/stop" : "/api/bot/start";
        try { await fetch(url, { method: "POST" }); } catch (e) {}
        fetchBotStatus();
    });

    $("btn-save-config").addEventListener("click", async () => {
        const v = Math.max(10, parseInt($("input-interval").value, 10) || 60);
        try {
            await fetch("/api/bot/configure", {
                method: "POST", headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ tick_interval: v }),
            });
        } catch (e) {}
    });

    $("btn-reset-wallet").addEventListener("click", async () => {
        const budget = parseFloat($("input-reset-budget").value);
        if (isNaN(budget) || budget <= 0) return alert("Budget invalide.");
        if (!confirm(`Réinitialiser à ${budget.toFixed(2)} $ ? Positions ET historique seront effacés.`)) return;
        try {
            await fetch("/api/portfolio/reset", {
                method: "POST", headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ budget }),
            });
            lastPosKey = lastTradesKey = lastEquityKey = "";
            fetchPortfolio(); fetchTrades(); fetchEquity();
        } catch (e) {}
    });

    // ===================== BOUCLES =====================
    initChart();
    fetchPortfolio(); fetchBotStatus(); fetchLogs(); fetchTrades(); fetchEquity(); fetchSignals();
    setInterval(() => { fetchPortfolio(); fetchLogs(); fetchBotStatus(); }, 4000);
    setInterval(() => { fetchSignals(); fetchTrades(); fetchEquity(); }, 12000);
});
