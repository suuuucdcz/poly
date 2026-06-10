// PolyQuant — Weather Edge — Frontend Controller

// PWA : enregistre le service worker (rend l'app installable sur téléphone)
if ("serviceWorker" in navigator) {
    window.addEventListener("load", () => {
        navigator.serviceWorker.register("/sw.js").catch(() => {});
    });
}

document.addEventListener("DOMContentLoaded", () => {
    // ===================== ÉTAT GLOBAL =====================
    let equityChart = null, equityAreaSeries = null, equityCashSeries = null;
    let largeChart = null, largeAreaSeries = null, largeCashSeries = null;
    let recentLogsText = "";
    let recentPositionsStr = "";
    let recentTradesStr = "";
    let recentEquityStr = "";
    let recentWeatherStr = "";
    let cachedEquityHistory = [];
    let lastWeather = [];        // derniers signaux météo (/api/signals)
    let lastWeatherUpdated = 0;
    let lastPositions = [];      // dernières positions (/api/portfolio)

    // ===================== DOM =====================
    const valTotalEquity = document.getElementById("val-total-equity");
    const valRoi = document.getElementById("val-roi");
    const valCashBalance = document.getElementById("val-cash-balance");
    const valWinRate = document.getElementById("val-win-rate");
    const valTrades = document.getElementById("val-trades");

    const botStatusDot = document.getElementById("bot-status-dot");
    const botStatusText = document.getElementById("bot-status-text");
    const btnStartBot = document.getElementById("btn-start-bot");
    const btnStopBot = document.getElementById("btn-stop-bot");

    const inputInterval = document.getElementById("input-interval");
    const btnSaveConfig = document.getElementById("btn-save-config");
    const inputResetBudget = document.getElementById("input-reset-budget");
    const btnResetWallet = document.getElementById("btn-reset-wallet");

    const positionsBody = document.getElementById("positions-body");
    const consoleOutput = document.getElementById("console-output");
    const historyContainer = document.getElementById("history-container");
    const weatherContainer = document.getElementById("weather-container");

    const btnExpandChart = document.getElementById("btn-expand-chart");
    const chartModal = document.getElementById("chart-modal");
    const chartModalClose = document.getElementById("chart-modal-close");

    // ===================== INIT =====================
    initChart();
    fetchPortfolio();
    fetchBotStatus();
    fetchLogs();
    fetchTrades();
    fetchEquityHistory();
    fetchSignals();

    // Le bot météo tourne sur un tick de ~60s : pas besoin de marteler l'API.
    setInterval(pollFastData, 2000);
    setInterval(fetchSignals, 10000);

    // ===================== NAVIGATION ONGLETS =====================
    document.querySelectorAll(".tab-btn").forEach(btn => {
        btn.addEventListener("click", () => {
            const tab = btn.getAttribute("data-tab");
            document.querySelectorAll(".tab-btn").forEach(b => b.classList.toggle("active", b === btn));
            document.querySelectorAll(".tab-panel").forEach(p => p.classList.toggle("active", p.id === "tab-" + tab));
            window.dispatchEvent(new Event("resize"));
        });
    });

    // ===================== CONTRÔLES BOT =====================
    btnStartBot.addEventListener("click", async () => {
        try {
            const res = await fetch("/api/bot/start", { method: "POST" });
            const data = await res.json();
            if (data.status === "success" || data.status === "already_running") {
                updateBotStatusUI(true);
                addConsoleLine("Robot DÉMARRÉ.", "success");
            }
        } catch (e) {
            addConsoleLine("Erreur au démarrage: " + e.message, "error");
        }
    });

    btnStopBot.addEventListener("click", async () => {
        try {
            const res = await fetch("/api/bot/stop", { method: "POST" });
            const data = await res.json();
            if (data.status === "success" || data.status === "already_stopped") {
                updateBotStatusUI(false);
                addConsoleLine("Robot ARRÊTÉ.", "warning");
            }
        } catch (e) {
            addConsoleLine("Erreur à l'arrêt: " + e.message, "error");
        }
    });

    btnSaveConfig.addEventListener("click", async () => {
        const payload = {
            tick_interval: Math.max(10, parseInt(inputInterval.value, 10) || 60)
        };
        try {
            const res = await fetch("/api/bot/configure", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify(payload)
            });
            if (res.ok) {
                addConsoleLine(`Réglages appliqués (analyse toutes les ${payload.tick_interval}s).`, "info");
            } else {
                const err = await res.json();
                addConsoleLine("Échec de la configuration: " + err.detail, "error");
            }
        } catch (e) {
            addConsoleLine("Erreur de communication: " + e.message, "error");
        }
    });

    btnResetWallet.addEventListener("click", async () => {
        const budget = parseFloat(inputResetBudget.value);
        if (isNaN(budget) || budget <= 0) {
            alert("Veuillez saisir un budget valide.");
            return;
        }
        if (!confirm(`Réinitialiser le portefeuille à ${budget.toFixed(2)} USDC ? Positions et historique seront effacés.`)) {
            return;
        }
        try {
            const res = await fetch("/api/portfolio/reset", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ budget })
            });
            if (res.ok) {
                addConsoleLine(`Portefeuille réinitialisé à ${budget.toFixed(2)} USDC.`, "info");
                recentEquityStr = ""; recentPositionsStr = ""; recentTradesStr = "";
                fetchPortfolio();
                fetchEquityHistory();
                fetchTrades();
            }
        } catch (e) {
            alert("Erreur de réinitialisation: " + e.message);
        }
    });

    // ===================== MODALE GRAPHE =====================
    btnExpandChart.addEventListener("click", () => {
        chartModal.style.display = "block";
        if (!largeChart) initLargeChart();
        renderLargeChartData();
    });
    chartModalClose.addEventListener("click", () => { chartModal.style.display = "none"; });
    window.addEventListener("click", (e) => {
        if (e.target === chartModal) chartModal.style.display = "none";
    });

    // ===================== POLLING =====================
    function pollFastData() {
        fetchPortfolio();
        fetchBotStatus();
        fetchLogs();
        fetchTrades();
        fetchEquityHistory();
    }

    // ===================== PORTEFEUILLE & POSITIONS =====================
    async function fetchPortfolio() {
        try {
            const res = await fetch("/api/portfolio");
            if (!res.ok) return;
            const data = await res.json();

            valTotalEquity.innerText = formatUSD(data.total_valuation);
            valCashBalance.innerText = formatUSD(data.balance);

            valRoi.innerText = (data.roi >= 0 ? "+" : "") + data.roi.toFixed(2) + " %";
            valRoi.className = "stat-v mono " + (data.roi >= 0 ? "positive" : "negative");

            valWinRate.innerText = (data.win_rate == null) ? "—" : data.win_rate.toFixed(1) + " %";

            if (valTrades) {
                valTrades.innerText = (data.settled_trade_count || 0) + " / " + (data.trade_count || 0);
            }

            renderPositions(data.positions);
            lastPositions = data.positions || [];
            renderWeatherTab();
        } catch (e) {
            console.error("Error fetching portfolio:", e);
        }
    }

    function renderPositions(positions) {
        const positionsStr = JSON.stringify(positions);
        if (positionsStr === recentPositionsStr) return;
        recentPositionsStr = positionsStr;

        if (!positions || positions.length === 0) {
            positionsBody.innerHTML = `<tr><td colspan="9" class="text-center text-muted">Aucune position ouverte actuellement.</td></tr>`;
            return;
        }

        let html = "";
        positions.forEach(pos => {
            const value = pos.shares * pos.current_price;
            const cost = pos.shares * pos.avg_price;
            const pnl = value - cost;
            const pnlPercent = cost > 0 ? (pnl / cost) * 100 : 0.0;
            const pnlClass = pnl >= 0 ? "pnl-positive" : "pnl-negative";
            const pnlSign = pnl >= 0 ? "+" : "";

            html += `
                <tr>
                    <td class="col-question" title="${escapeHTML(pos.question)}"><strong>${escapeHTML(pos.question)}</strong></td>
                    <td><span class="history-action buy">${escapeHTML(pos.outcome)}</span></td>
                    <td>${pos.shares.toFixed(1)}</td>
                    <td>${pos.avg_price.toFixed(2)}&nbsp;$</td>
                    <td>${pos.current_price.toFixed(2)}&nbsp;$</td>
                    <td><strong>${value.toFixed(2)}&nbsp;$</strong></td>
                    <td class="${pnlClass}"><strong>${pnlSign}${pnl.toFixed(2)}&nbsp;$ (${pnlSign}${pnlPercent.toFixed(1)}%)</strong></td>
                    <td class="end-date-cell">${formatDateTime(pos.end_date)}</td>
                    <td><button class="btn btn-small btn-danger btn-close-pos" data-token="${pos.token_id}" data-outcome="${pos.outcome}">Vendre tout</button></td>
                </tr>
            `;
        });
        positionsBody.innerHTML = html;

        document.querySelectorAll(".btn-close-pos").forEach(btn => {
            btn.addEventListener("click", async () => {
                const tokenId = btn.getAttribute("data-token");
                const outcomeName = btn.getAttribute("data-outcome");
                if (!confirm(`Vendre toute la position '${outcomeName}' au meilleur prix du carnet ?`)) return;
                btn.disabled = true;
                btn.innerText = "Vente...";
                try {
                    const res = await fetch("/api/positions/sell", {
                        method: "POST",
                        headers: { "Content-Type": "application/json" },
                        body: JSON.stringify({ token_id: tokenId })
                    });
                    const data = await res.json();
                    if (res.ok) {
                        addConsoleLine(`Vente : ${data.data.shares} parts @ ${data.data.price} — PnL ${data.data.pnl >= 0 ? "+" : ""}${data.data.pnl.toFixed(2)} USDC.`, "success");
                        recentPositionsStr = "";
                        fetchPortfolio();
                        fetchTrades();
                    } else {
                        alert("Erreur de vente: " + data.detail);
                    }
                } catch (err) {
                    alert("Erreur réseau: " + err.message);
                }
            });
        });
    }

    // ===================== STATUT BOT =====================
    async function fetchBotStatus() {
        try {
            const res = await fetch("/api/bot/status");
            if (!res.ok) return;
            const data = await res.json();
            updateBotStatusUI(data.is_running);
            if (document.activeElement !== inputInterval) inputInterval.value = data.tick_interval;
        } catch (e) {
            console.error("Error fetching bot status:", e);
        }
    }

    function updateBotStatusUI(isRunning) {
        if (isRunning) {
            botStatusDot.className = "status-dot active";
            botStatusText.innerText = "Actif";
            btnStartBot.disabled = true;
            btnStopBot.disabled = false;
        } else {
            botStatusDot.className = "status-dot";
            botStatusText.innerText = "Arrêté";
            btnStartBot.disabled = false;
            btnStopBot.disabled = true;
        }
    }

    // ===================== SIGNAUX MÉTÉO =====================
    async function fetchSignals() {
        try {
            const res = await fetch("/api/signals");
            if (!res.ok) return;
            const data = await res.json();
            lastWeather = data.weather || [];
            lastWeatherUpdated = data.updated_at || 0;
            renderLearning(data.learning);
            renderWeatherTab();
        } catch (e) {
            // silencieux : le panneau se remplira au prochain tick
        }
    }

    function renderLearning(l) {
        const el = document.getElementById("weather-learn");
        if (!el) return;
        if (!l) { el.innerText = ""; return; }
        const winrate = l.samples ? Math.round(100 * l.wins / l.samples) + "% win" : "—";
        const pnl = (l.pnl >= 0 ? "+" : "") + (l.pnl || 0).toFixed(2) + " $";
        const mode = l.calibrated ? "calibration ACTIVE" : "calibration en attente de données";
        el.classList.toggle("on", !!l.calibrated);
        el.innerText = `Apprentissage : ${l.samples} paris réglés · ${winrate} · PnL ${pnl} · ${mode}`;
    }

    // ---------- Helpers visuels météo ----------

    // Ordre numérique des tranches (les « or below » avant, « or higher » après)
    function bucketKey(label) {
        const m = String(label).match(/-?\d+/);
        if (!m) return 0;
        const v = parseInt(m[0], 10);
        const low = String(label).toLowerCase();
        if (low.includes("below") || low.includes("lower")) return v - 0.25;
        if (low.includes("higher") || low.includes("above")) return v + 0.25;
        return v;
    }

    // Libellé court pour la jauge ("88-89°F" -> "88-89", "98°F or higher" -> "98+")
    function shortLabel(label) {
        return String(label).replace(/°[CF]/g, "")
            .replace(/ or higher| or above/i, "+")
            .replace(/ or below| or lower/i, "−").trim();
    }

    // Bornes numériques d'une tranche (règle de troncature : "88-89" couvre [88,90[)
    function bucketRange(label) {
        const nums = String(label).match(/-?\d+/g);
        if (!nums) return null;
        const low = String(label).toLowerCase();
        const a = parseInt(nums[0], 10);
        if (low.includes("below") || low.includes("lower")) return [-Infinity, a + 0.999];
        if (low.includes("higher") || low.includes("above")) return [a, Infinity];
        if (nums.length >= 2) {
            const b = parseInt(nums[1], 10);
            return [Math.min(a, b), Math.max(a, b) + 0.999];
        }
        return [a, a + 0.999];
    }

    // Jauge : mini-histogramme de la distribution, tranche pariée en vert,
    // ▼ = prévision médiane, ● = relevé du jour
    function buildGauge(ev, betLabel) {
        const buckets = (ev.buckets || []).filter(b => b.p !== null && b.p !== undefined)
            .slice().sort((a, b) => bucketKey(a.label) - bucketKey(b.label));
        if (!buckets.length) return "";
        const maxP = Math.max(...buckets.map(b => b.p), 0.01);
        let segs = "";
        buckets.forEach(b => {
            const h = Math.max(3, Math.round((b.p / maxP) * 44));
            const isBet = betLabel && b.label === betLabel;
            const rng = bucketRange(b.label);
            const hasMed = rng && ev.median !== null && ev.median >= rng[0] && ev.median <= rng[1];
            const hasReal = rng && ev.realized !== null && ev.realized !== undefined && ev.realized >= rng[0] && ev.realized <= rng[1];
            segs += `<div class="gauge-seg${isBet ? " bet" : ""}">
                <span class="gauge-mark">${hasMed ? "▼" : (hasReal ? "●" : "")}</span>
                <div class="gauge-bar" style="height:${h}px"></div>
                <span class="gauge-cap">${escapeHTML(shortLabel(b.label))}</span>
            </div>`;
        });
        return `<div class="gauge">${segs}</div>
                <div class="gauge-legend mono">▼ prévision${ev.realized != null ? " · ● relevé" : ""} · <span class="gauge-bet-dot"></span> ton pari</div>`;
    }

    // Phrase de statut, en clair
    function betStatusLine(ev, label) {
        const rng = bucketRange(label);
        if (!rng || ev.median === null) return "";
        if (ev.realized != null && ev.realized > rng[1]) {
            return `<div class="bet-status bad">❌ Le relevé (${ev.realized}°) a déjà dépassé ta tranche</div>`;
        }
        if (ev.median >= rng[0] && ev.median <= rng[1]) {
            const extra = (ev.realized != null && ev.realized >= rng[0]) ? " — le relevé y est aussi 🔥" : "";
            return `<div class="bet-status ok">✅ La prévision (${ev.median}°) est dans ta tranche${extra}</div>`;
        }
        const d = ev.median < rng[0] ? (rng[0] - ev.median) : (ev.median - rng[1]);
        return `<div class="bet-status warn">⚠️ La prévision (${ev.median}°) est à ~${d.toFixed(1)}° de ta tranche</div>`;
    }

    function parseCityDate(question) {
        const m = String(question).match(/in (.+?) on ([A-Za-z]+ \d+)/);
        return { city: m ? m[1] : "?", date: m ? m[2] : "" };
    }

    // ---------- Rendu de l'onglet Météo (visuel + pro) ----------
    function renderWeatherTab() {
        if (!weatherContainer) return;
        const hash = JSON.stringify([lastWeather, lastPositions.map(p => [p.token_id, p.shares, p.current_price])]);
        if (hash === recentWeatherStr) return;
        recentWeatherStr = hash;

        let html = "";
        html += renderMyBets();
        html += renderTargets();
        html += renderProSection();
        if (!html) {
            html = `<div class="text-center text-muted padded">Aucun marché température exploitable pour l'instant — le bot scanne en continu (les villes du lendemain apparaissent au fil de la journée).</div>`;
        }
        weatherContainer.innerHTML = html;
    }

    // === SECTION VISUELLE 1 : mes paris ===
    function renderMyBets() {
        if (!lastPositions.length) {
            return `<div class="section-h"><h2>🎯 Mes paris</h2></div>
                    <div class="panel text-center text-muted padded">Aucun pari en cours — le bot achète dès qu'une tranche sous-évaluée apparaît.</div>`;
        }
        let cards = "";
        let totalCost = 0, totalPnl = 0;
        lastPositions.forEach(pos => {
            // retrouver le marché + la tranche dans les signaux
            const ev = lastWeather.find(e => pos.question.startsWith(e.title));
            const label = ev ? pos.question.slice(ev.title.length).trim()
                             : (pos.question.split("?")[1] || "").trim();
            const bucket = ev ? (ev.buckets || []).find(b => b.label === label) : null;
            const pd = ev ? { city: ev.city, date: ev.date } : parseCityDate(pos.question);

            const px = bucket ? bucket.price : pos.current_price;
            const cost = pos.shares * pos.avg_price;
            const value = pos.shares * px;
            const pnl = value - cost;
            const winGain = pos.shares * 1.0 - cost;
            totalCost += cost; totalPnl += pnl;
            const pnlCls = pnl >= 0 ? "pnl-positive" : "pnl-negative";

            cards += `<div class="bet-card panel">
                <div class="bet-head">
                    <div>
                        <div class="bet-city">${escapeHTML(cap(pd.city))}</div>
                        <div class="bet-date mono">${escapeHTML(pd.date || "")}${ev && ev.is_today ? ` · <span class="chip chip-today">AUJOURD'HUI</span>` : ""}</div>
                    </div>
                    <span class="bet-label-chip">${escapeHTML(label || pos.outcome)}</span>
                </div>
                ${ev ? buildGauge(ev, label) : `<div class="bet-status wait">⏳ Marché clos — en attente du résultat officiel</div>`}
                ${ev ? betStatusLine(ev, label) : ""}
                <div class="bet-money mono">
                    <div><span class="bm-k">Mise</span><span class="bm-v">${cost.toFixed(2)} $</span></div>
                    <div><span class="bm-k">Valeur</span><span class="bm-v">${value.toFixed(2)} $</span></div>
                    <div><span class="bm-k">Si gagné</span><span class="bm-v pnl-positive">+${winGain.toFixed(2)} $</span></div>
                    <div><span class="bm-k">PnL latent</span><span class="bm-v ${pnlCls}">${pnl >= 0 ? "+" : ""}${pnl.toFixed(2)} $</span></div>
                </div>
            </div>`;
        });
        const totCls = totalPnl >= 0 ? "pnl-positive" : "pnl-negative";
        return `<div class="section-h"><h2>🎯 Mes paris (${lastPositions.length})</h2>
                <span class="mono section-sub">misé ${totalCost.toFixed(2)} $ · PnL latent <span class="${totCls}">${totalPnl >= 0 ? "+" : ""}${totalPnl.toFixed(2)} $</span></span></div>
                <div class="bets-grid">${cards}</div>`;
    }

    // === SECTION VISUELLE 2 : prochaines cibles ===
    function renderTargets() {
        const opps = [];
        lastWeather.forEach(ev => (ev.buckets || []).forEach(b => {
            if (!b.held_shares && b.edge !== null && b.edge !== undefined
                && b.edge >= 0.05 && b.price > 0.02 && b.price < 0.60) {
                opps.push({ ev, b });
            }
        }));
        opps.sort((a, b) => b.b.edge - a.b.edge);
        let inner;
        if (!opps.length) {
            inner = `<span class="text-muted">Rien d'assez juteux pour l'instant — le bot attend la prochaine occasion.</span>`;
        } else {
            inner = opps.slice(0, 8).map(({ ev, b }) =>
                `<span class="opp-chip"><strong>${escapeHTML(cap(ev.city))}</strong> · ${escapeHTML(b.label)} — ${Math.round(b.price * 100)}¢ <span class="pnl-positive">edge +${Math.round(b.edge * 100)}</span></span>`
            ).join("");
        }
        return `<div class="section-h"><h2>🔭 Prochaines cibles</h2>
                <span class="mono section-sub">tranches sous-évaluées détectées au dernier scan</span></div>
                <div class="panel targets">${inner}</div>`;
    }

    // === SECTION PRO : tous les marchés en détail ===
    function renderProSection() {
        const events = lastWeather;
        if (!events.length) return "";
        let html = `<div class="section-h"><h2>🔬 Analyse pro — ${events.length} marchés scannés</h2>
                    <span class="mono section-sub">${lastWeatherUpdated ? "maj " + new Date(lastWeatherUpdated * 1000).toLocaleTimeString() : ""}</span></div>`;
        events.forEach(ev => {
            const u = "°" + (ev.unit || "C");
            const spread = ev.spread ? `${ev.spread[0]}${u} → ${ev.spread[1]}${u}` : "";
            const todayChip = ev.is_today ? `<span class="chip chip-today">AUJOURD'HUI</span>` : `<span class="chip">${escapeHTML(ev.date || "")}</span>`;
            const srcTag = ev.realized_src === "station" ? " 📡 station officielle" : (ev.realized_src === "grille" ? " (grille)" : "");
            const realizedTxt = (ev.realized != null)
                ? `réalisé <strong class="mono">${ev.realized}${u}</strong>${srcTag}`
                : `<span class="text-muted">pas encore de relevé du jour</span>`;
            const biasTag = (ev.bias != null && ev.bias !== 0)
                ? ` · biais appris <strong class="mono">${ev.bias > 0 ? "+" : ""}${ev.bias}°</strong>` : "";

            let rows = "";
            (ev.buckets || []).forEach(b => {
                if (b.p === null || b.p === undefined) return;
                const pct = Math.round(b.p * 100);
                const pbar = `<div class="pbar"><div class="pbar-fill" style="width:${pct}%"></div><span class="pbar-label">${pct}%</span></div>`;
                const edgeVal = (b.edge !== null && b.edge !== undefined) ? b.edge : null;
                const edgeCls = edgeVal === null ? "text-muted" : (edgeVal > 0.05 ? "pnl-positive" : (edgeVal > 0 ? "" : "pnl-negative"));
                const edgeTxt = edgeVal === null ? "—" : (edgeVal >= 0 ? "+" : "") + Math.round(edgeVal * 100);
                const held = b.held_shares > 0;
                const betCell = held
                    ? `<span class="badge-bet">PARIÉ ${b.held_shares.toFixed(1)} @ ${(b.held_avg || 0).toFixed(2)}</span>`
                    : "—";
                rows += `<tr class="${held ? "bet-row" : ""}">
                    <td>${escapeHTML(b.label)}</td>
                    <td>${pbar}</td>
                    <td class="num mono">${Math.round(b.price * 100)}¢</td>
                    <td class="num mono ${edgeCls}"><strong>${edgeTxt}</strong></td>
                    <td>${betCell}</td>
                </tr>`;
            });

            html += `<div class="panel weather-card">
                <div class="panel-head">
                    <h2>${escapeHTML(cap(ev.city))} ${todayChip}</h2>
                    <span class="mono text-muted weather-meta">prévision méd <strong>${ev.median}${u}</strong> (${spread}) · ${realizedTxt}${biasTag} · ${ev.n} scénarios</span>
                </div>
                <div class="table-wrapper"><table class="data-table">
                    <thead><tr><th>Tranche</th><th>Proba modèle</th><th class="num">Prix marché</th><th class="num">Edge ×100</th><th>Pari</th></tr></thead>
                    <tbody>${rows}</tbody>
                </table></div>
            </div>`;
        });

        return html;
    }

    // ===================== CONSOLE =====================
    async function fetchLogs() {
        try {
            const res = await fetch("/api/logs");
            if (!res.ok) return;
            const data = await res.json();

            let logsHtml = "";
            data.logs.forEach(log => {
                let lvlClass = "system";
                if (log.level === "SUCCESS") lvlClass = "success";
                else if (log.level === "ERROR") lvlClass = "error";
                else if (log.level === "WARNING") lvlClass = "warning";
                else if (log.level === "INFO") lvlClass = "info";
                logsHtml += `<div class="console-line ${lvlClass}">[${log.time}] [${log.level}] ${escapeHTML(log.message)}</div>`;
            });

            if (logsHtml !== recentLogsText) {
                const shouldScroll = consoleOutput.scrollHeight - consoleOutput.scrollTop - consoleOutput.clientHeight < 40;
                consoleOutput.innerHTML = logsHtml;
                recentLogsText = logsHtml;
                if (shouldScroll) consoleOutput.scrollTop = consoleOutput.scrollHeight;
            }
        } catch (e) {
            console.error("Error fetching logs:", e);
        }
    }

    function addConsoleLine(text, level = "info") {
        const time = new Date().toLocaleTimeString();
        const line = document.createElement("div");
        line.className = `console-line ${level}`;
        line.innerText = `[${time}] [MANUAL] ${text}`;
        consoleOutput.appendChild(line);
        consoleOutput.scrollTop = consoleOutput.scrollHeight;
    }

    // ===================== HISTORIQUE =====================
    async function fetchTrades() {
        try {
            const res = await fetch("/api/trades");
            if (!res.ok) return;
            const data = await res.json();

            const tradesStr = JSON.stringify(data.trades);
            if (tradesStr === recentTradesStr) return;
            recentTradesStr = tradesStr;

            if (!data.trades || data.trades.length === 0) {
                historyContainer.innerHTML = `<div class="text-center text-muted padded">Aucun trade historique enregistré.</div>`;
                return;
            }

            let html = "";
            data.trades.forEach(t => {
                const date = new Date(t.timestamp + "Z").toLocaleString("fr-FR", { day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" });
                const cost = t.shares * t.price;
                const actionClass = t.action === "RESOLVE" ? "resolve" : t.action.toLowerCase();

                let pnlHtml = "";
                if (t.pnl !== null) {
                    const sign = t.pnl >= 0 ? "+" : "";
                    const pnlClass = t.pnl >= 0 ? "pnl-positive" : "pnl-negative";
                    pnlHtml = `<span class="history-pnl ${pnlClass}">PnL: ${sign}${t.pnl.toFixed(2)} $</span>`;
                }

                html += `
                    <div class="history-item">
                        <div class="history-header">
                            <span class="history-action ${actionClass}">${t.action} ${escapeHTML(t.outcome)}</span>
                            <span class="history-time">${date}</span>
                        </div>
                        <div class="history-body">${escapeHTML(t.question)}</div>
                        <div class="history-footer">
                            <span>${t.shares.toFixed(1)} parts @ ${t.price.toFixed(2)} $</span>
                            <span>${pnlHtml || `Coût: ${cost.toFixed(2)} $`}</span>
                        </div>
                    </div>
                `;
            });
            historyContainer.innerHTML = html;
        } catch (e) {
            console.error("Error fetching trades:", e);
        }
    }

    // ===================== COURBE D'EQUITY (Lightweight Charts) =====================
    function buildSeriesData(history) {
        const totalMap = new Map();
        const cashMap = new Map();
        (history || []).forEach(h => {
            const t = Math.floor(new Date(h.timestamp + "Z").getTime() / 1000);
            totalMap.set(t, h.portfolio_value);
            cashMap.set(t, h.balance);
        });
        const toSeries = (m) => [...m.entries()].sort((a, b) => a[0] - b[0]).map(([time, value]) => ({ time, value }));
        return { total: toSeries(totalMap), cash: toSeries(cashMap) };
    }

    function chartBaseOptions(el, fallbackHeight) {
        return {
            width: el.clientWidth,
            height: el.clientHeight || fallbackHeight,
            layout: {
                background: { type: "solid", color: "transparent" },
                textColor: "#6b7a90",
                fontFamily: "'Inter', sans-serif",
                fontSize: 11
            },
            grid: {
                vertLines: { color: "rgba(26,36,54,0.4)" },
                horzLines: { color: "rgba(26,36,54,0.4)" }
            },
            timeScale: { timeVisible: true, secondsVisible: false, borderColor: "#1a2436" },
            rightPriceScale: { borderColor: "#1a2436" },
            crosshair: { mode: 0 },
            localization: { priceFormatter: (p) => p.toFixed(2) + " $" }
        };
    }

    function addEquitySeries(chart) {
        const area = chart.addAreaSeries({
            lineColor: "#38bdf8",
            topColor: "rgba(56,189,248,0.28)",
            bottomColor: "rgba(56,189,248,0.02)",
            lineWidth: 2,
            priceLineVisible: false
        });
        const cash = chart.addLineSeries({
            color: "#7c8aa3",
            lineWidth: 1,
            lineStyle: 2,
            priceLineVisible: false,
            lastValueVisible: false
        });
        return { area, cash };
    }

    async function fetchEquityHistory() {
        try {
            const res = await fetch("/api/equity-history");
            if (!res.ok) return;
            const data = await res.json();

            const equityStr = JSON.stringify(data.history);
            if (equityStr === recentEquityStr) return;
            recentEquityStr = equityStr;

            cachedEquityHistory = data.history || [];
            const { total, cash } = buildSeriesData(cachedEquityHistory);
            if (equityAreaSeries) equityAreaSeries.setData(total);
            if (equityCashSeries) equityCashSeries.setData(cash);

            if (largeChart && chartModal.style.display === "block") renderLargeChartData();
        } catch (e) {
            console.error("Error fetching equity history:", e);
        }
    }

    function initChart() {
        const el = document.getElementById("equityChart");
        if (!el || !window.LightweightCharts) return;
        equityChart = LightweightCharts.createChart(el, chartBaseOptions(el, 250));
        const s = addEquitySeries(equityChart);
        equityAreaSeries = s.area;
        equityCashSeries = s.cash;
        new ResizeObserver(() => {
            if (el.clientWidth) equityChart.applyOptions({ width: el.clientWidth, height: el.clientHeight || 250 });
        }).observe(el);
    }

    function initLargeChart() {
        const el = document.getElementById("largeEquityChart");
        if (!el || !window.LightweightCharts) return;
        largeChart = LightweightCharts.createChart(el, chartBaseOptions(el, 450));
        const s = addEquitySeries(largeChart);
        largeAreaSeries = s.area;
        largeCashSeries = s.cash;
        new ResizeObserver(() => {
            if (el.clientWidth) largeChart.applyOptions({ width: el.clientWidth, height: el.clientHeight || 450 });
        }).observe(el);
    }

    function renderLargeChartData() {
        if (!largeAreaSeries || !cachedEquityHistory) return;
        const { total, cash } = buildSeriesData(cachedEquityHistory);
        largeAreaSeries.setData(total);
        largeCashSeries.setData(cash);
        largeChart.timeScale().fitContent();
    }

    // ===================== UTILS =====================
    function formatUSD(num) {
        return num.toLocaleString("fr-FR", { minimumFractionDigits: 2, maximumFractionDigits: 2 }) + " $";
    }

    function cap(s) {
        return (s || "").replace(/\b\w/g, c => c.toUpperCase());
    }

    function escapeHTML(str) {
        return String(str).replace(/[&<>'"]/g,
            tag => ({
                '&': '&amp;',
                '<': '&lt;',
                '>': '&gt;',
                "'": '&#39;',
                '"': '&quot;'
            }[tag] || tag)
        );
    }

    function formatDateTime(isoStr) {
        if (!isoStr) return "—";
        try {
            const d = new Date(isoStr);
            if (isNaN(d.getTime())) return "—";
            return d.toLocaleDateString("fr-FR", { day: "2-digit", month: "2-digit" }) + " " +
                   d.toLocaleTimeString("fr-FR", { hour: "2-digit", minute: "2-digit" });
        } catch (e) {
            return "—";
        }
    }
});
