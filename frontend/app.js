// Polymarket Paper Trader - Frontend Controller

// PWA : enregistre le service worker (rend l'app installable sur téléphone)
if ("serviceWorker" in navigator) {
    window.addEventListener("load", () => {
        navigator.serviceWorker.register("/sw.js").catch(() => {});
    });
}

document.addEventListener("DOMContentLoaded", () => {
    // Global State
    let equityChart = null, equityAreaSeries = null, equityCashSeries = null;
    let largeChart = null, largeAreaSeries = null, largeCashSeries = null;
    let cachedMarkets = [];
    let currentModalData = null; // Store data for manual trade modal
    let recentLogsText = "";     // Store concatenated logs to avoid excessive DOM re-renders
    let recentPositionsStr = ""; // Prevent redundant positions rendering
    let recentTradesStr = "";    // Prevent redundant trades rendering
    let recentEquityStr = "";    // Prevent redundant equity chart updates
    let recentMarketsStr = "";   // Prevent redundant markets rendering
    let cachedEquityHistory = []; // Store raw equity history data

    // DOM Elements
    const valTotalEquity = document.getElementById("val-total-equity");
    const valRoi = document.getElementById("val-roi");
    const valCashBalance = document.getElementById("val-cash-balance");
    const valWinRate = document.getElementById("val-win-rate");
    const valTrades = document.getElementById("val-trades");

    const botStatusDot = document.getElementById("bot-status-dot");
    const botStatusText = document.getElementById("bot-status-text");
    const btnStartBot = document.getElementById("btn-start-bot");
    const btnStopBot = document.getElementById("btn-stop-bot");

    const selectStrategy = document.getElementById("select-strategy");
    const inputInterval = document.getElementById("input-interval");
    const inputScanLimit = document.getElementById("input-scan-limit");
    const btnSaveConfig = document.getElementById("btn-save-config");

    const inputResetBudget = document.getElementById("input-reset-budget");
    const btnResetWallet = document.getElementById("btn-reset-wallet");

    const positionsBody = document.getElementById("positions-body");
    const marketsContainer = document.getElementById("markets-container");
    const btnRefreshMarkets = document.getElementById("btn-refresh-markets");
    const inputMarketSearch = document.getElementById("input-market-search");

    const consoleOutput = document.getElementById("console-output");
    const historyContainer = document.getElementById("history-container");

    // Modal DOM Elements
    const tradeModal = document.getElementById("trade-modal");
    const modalClose = document.getElementById("modal-close");
    const modalMarketTitle = document.getElementById("modal-market-title");
    const modalOutcomeName = document.getElementById("modal-outcome-name");

    // Large Chart Modal DOM Elements
    const btnExpandChart = document.getElementById("btn-expand-chart");
    const chartModal = document.getElementById("chart-modal");
    const chartModalClose = document.getElementById("chart-modal-close");
    const modalOutcomePrice = document.getElementById("modal-outcome-price");
    const inputTradeAmount = document.getElementById("input-trade-amount");
    const modalUserBalance = document.getElementById("modal-user-balance");
    const btnConfirmTrade = document.getElementById("btn-confirm-trade");
    const btnCancelTrade = document.getElementById("btn-cancel-trade");

    // --- INITIALIZATION ---
    initChart();
    fetchPortfolio();
    fetchBotStatus();
    fetchMarkets();
    fetchLogs();
    fetchTrades();
    fetchEquityHistory();
    fetchCryptoSignals();

    // Start Polling Intervals (500ms for constant dashboard feedback)
    setInterval(pollFastData, 500);
    // Fetch markets list frequently since the backend serves it from a fast 5s memory cache (every 1 second)
    setInterval(fetchMarkets, 1000);
    // Crypto Up/Down signals (countdown moves every second)
    setInterval(fetchCryptoSignals, 1000);

    // --- EVENT LISTENERS ---

    // Bot Control
    btnStartBot.addEventListener("click", async () => {
        try {
            const res = await fetch("/api/bot/start", { method: "POST" });
            const data = await res.json();
            if (data.status === "success" || data.status === "already_running") {
                updateBotStatusUI(true);
                addConsoleLine("Robot DÉMARRÉ avec succès.", "success");
            }
        } catch (e) {
            addConsoleLine("Erreur lors du démarrage du bot: " + e.message, "error");
        }
    });

    btnStopBot.addEventListener("click", async () => {
        try {
            const res = await fetch("/api/bot/stop", { method: "POST" });
            const data = await res.json();
            if (data.status === "success" || data.status === "already_stopped") {
                updateBotStatusUI(false);
                addConsoleLine("Robot ARRÊTÉ avec succès.", "warning");
            }
        } catch (e) {
            addConsoleLine("Erreur lors de l'arrêt du bot: " + e.message, "error");
        }
    });

    // Save Bot Config
    btnSaveConfig.addEventListener("click", async () => {
        const payload = {
            strategy: selectStrategy.value,
            tick_interval: parseInt(inputInterval.value, 10),
            max_markets_to_scan: parseInt(inputScanLimit.value, 10)
        };
        try {
            const res = await fetch("/api/bot/configure", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify(payload)
            });
            if (res.ok) {
                addConsoleLine(`Configuration appliquée: ${payload.strategy} | Ticks: ${payload.tick_interval}s`, "info");
            } else {
                const err = await res.json();
                addConsoleLine("Échec de la configuration: " + err.detail, "error");
            }
        } catch (e) {
            addConsoleLine("Erreur de communication: " + e.message, "error");
        }
    });

    // Reset Portfolio
    btnResetWallet.addEventListener("click", async () => {
        const budget = parseFloat(inputResetBudget.value);
        if (isNaN(budget) || budget <= 0) {
            alert("Veuillez saisir un budget valide.");
            return;
        }
        if (!confirm(`Voulez-vous réinitialiser le portefeuille avec ${budget.toFixed(2)} USDC ? Toutes les positions et historiques seront effacés.`)) {
            return;
        }
        try {
            const res = await fetch("/api/portfolio/reset", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ budget })
            });
            if (res.ok) {
                addConsoleLine(`Portefeuille réinitialisé avec ${budget.toFixed(2)} USDC.`, "info");
                fetchPortfolio();
                fetchEquityHistory();
                fetchTrades();
            }
        } catch (e) {
            alert("Erreur de réinitialisation: " + e.message);
        }
    });

    // Refresh Markets button
    btnRefreshMarkets.addEventListener("click", () => {
        fetchMarkets();
    });

    // Search filter
    inputMarketSearch.addEventListener("input", () => {
        renderMarketsList();
    });

    // Close Modal Events
    modalClose.addEventListener("click", closeModal);
    btnCancelTrade.addEventListener("click", closeModal);
    window.addEventListener("click", (e) => {
        if (e.target === tradeModal) closeModal();
        if (e.target === chartModal) {
            chartModal.style.display = "none";
        }
    });

    // Expand Chart Modal Events
    btnExpandChart.addEventListener("click", () => {
        chartModal.style.display = "block";
        if (!largeChart) {
            initLargeChart();
        }
        renderLargeChartData();
    });

    chartModalClose.addEventListener("click", () => {
        chartModal.style.display = "none";
    });

    // Confirm Trade Event
    btnConfirmTrade.addEventListener("click", async () => {
        if (!currentModalData) return;
        const amount = parseFloat(inputTradeAmount.value);
        if (isNaN(amount) || amount < 5) {
            alert("Le montant minimum d'investissement est de 5 USDC.");
            return;
        }
        
        btnConfirmTrade.disabled = true;
        btnConfirmTrade.innerText = "Traitement...";
        
        const payload = {
            market_id: currentModalData.marketId,
            token_id: currentModalData.tokenId,
            action: "BUY",
            outcome: currentModalData.outcomeName,
            amount_usdc: amount
        };

        try {
            const res = await fetch("/api/trade", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify(payload)
            });
            const data = await res.json();
            
            if (res.ok) {
                addConsoleLine(`Achat manuel réussi : ${data.data.shares} parts de ${payload.outcome} à ${data.data.price} USDC.`, "success");
                closeModal();
                fetchPortfolio();
                fetchTrades();
                fetchEquityHistory();
            } else {
                alert("Erreur : " + data.detail);
                addConsoleLine("Échec de l'ordre manuel: " + data.detail, "error");
            }
        } catch (e) {
            alert("Erreur réseau: " + e.message);
        } finally {
            btnConfirmTrade.disabled = false;
            btnConfirmTrade.innerText = "Confirmer l'achat";
        }
    });

    // Tab navigation
    document.querySelectorAll(".tab-btn").forEach(btn => {
        btn.addEventListener("click", () => {
            const tab = btn.getAttribute("data-tab");
            document.querySelectorAll(".tab-btn").forEach(b => b.classList.toggle("active", b === btn));
            document.querySelectorAll(".tab-panel").forEach(p => p.classList.toggle("active", p.id === "tab-" + tab));
            // Charts inside a freshly-shown panel need a resize to fit
            window.dispatchEvent(new Event("resize"));
        });
    });

    // "Activer ce mode" shortcut on the Crypto tab → switch strategy to crypto_direction
    const btnActivateCrypto = document.getElementById("btn-activate-crypto");
    if (btnActivateCrypto) {
        btnActivateCrypto.addEventListener("click", async () => {
            const payload = {
                strategy: "crypto_direction",
                tick_interval: Math.max(2, parseInt(inputInterval.value, 10) || 2),
                max_markets_to_scan: parseInt(inputScanLimit.value, 10) || 8
            };
            try {
                const res = await fetch("/api/bot/configure", {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify(payload)
                });
                if (res.ok) {
                    selectStrategy.value = "crypto_direction";
                    if (parseInt(inputInterval.value, 10) > 5) inputInterval.value = payload.tick_interval;
                    addConsoleLine("Mode Crypto Direction activé (intervalle " + payload.tick_interval + "s).", "success");
                } else {
                    const err = await res.json();
                    addConsoleLine("Échec activation crypto: " + err.detail, "error");
                }
            } catch (e) {
                addConsoleLine("Erreur activation crypto: " + e.message, "error");
            }
        });
    }

    // --- FUNCTIONS ---

    // Polling fast changing data (1.5 seconds)
    function pollFastData() {
        fetchPortfolio();
        fetchBotStatus();
        fetchLogs();
        fetchTrades();
        fetchEquityHistory();
    }

    // Fetch and render portfolio metrics + positions table
    async function fetchPortfolio() {
        try {
            const res = await fetch("/api/portfolio");
            if (!res.ok) return;
            const data = await res.json();

            // Set Header metrics
            valTotalEquity.innerText = formatUSD(data.total_valuation);
            valCashBalance.innerText = formatUSD(data.balance);
            
            const roiText = (data.roi >= 0 ? "+" : "") + data.roi.toFixed(2) + " %";
            valRoi.innerText = roiText;
            valRoi.className = "metric-value " + (data.roi >= 0 ? "positive" : "negative");
            
            if (data.win_rate === null || data.win_rate === undefined) {
                valWinRate.innerText = "—";
            } else {
                valWinRate.innerText = data.win_rate.toFixed(1) + " %";
            }
            
            if (valTrades) {
                const settled = (data.settled_trade_count != null) ? data.settled_trade_count : 0;
                const total = (data.trade_count != null) ? data.trade_count : 0;
                valTrades.innerText = settled + " / " + total;
            }

            // Render Positions Table
            renderPositions(data.positions);
        } catch (e) {
            console.error("Error fetching portfolio:", e);
        }
    }

    // Render active positions in table
    function renderPositions(positions) {
        const positionsStr = JSON.stringify(positions);
        if (positionsStr === recentPositionsStr) return;
        recentPositionsStr = positionsStr;

        if (!positions || positions.length === 0) {
            positionsBody.innerHTML = `
                <tr>
                    <td colspan="9" class="text-center text-muted">Aucune position ouverte actuellement.</td>
                </tr>
            `;
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
                    <td><span class="history-action ${pos.outcome.toLowerCase() === 'yes' ? 'buy' : 'sell'}">${pos.outcome}</span></td>
                    <td>${pos.shares.toFixed(1)}</td>
                    <td>${pos.avg_price.toFixed(2)}&nbsp;$</td>
                    <td>${pos.current_price.toFixed(2)}&nbsp;$</td>
                    <td><strong>${value.toFixed(2)}&nbsp;$</strong></td>
                    <td class="${pnlClass}"><strong>${pnlSign}${pnl.toFixed(2)}&nbsp;$ (${pnlSign}${pnlPercent.toFixed(1)}%)</strong></td>
                    <td class="end-date-cell">${formatDateTime(pos.end_date)}</td>
                    <td>
                        <button class="btn btn-small btn-danger btn-close-pos" data-token="${pos.token_id}" data-outcome="${pos.outcome}">Vendre tout</button>
                    </td>
                </tr>
            `;
        });
        positionsBody.innerHTML = html;

        // Add event listeners to "Close Position" buttons
        document.querySelectorAll(".btn-close-pos").forEach(btn => {
            btn.addEventListener("click", async (e) => {
                const tokenId = btn.getAttribute("data-token");
                const outcomeName = btn.getAttribute("data-outcome");
                if (!confirm(`Voulez-vous vendre la totalité de votre position sur '${outcomeName}' au prix du marché (meilleur bid) ?`)) {
                    return;
                }
                
                btn.disabled = true;
                btn.innerText = "Vente...";
                
                try {
                    const res = await fetch("/api/trade", {
                        method: "POST",
                        headers: { "Content-Type": "application/json" },
                        body: JSON.stringify({
                            market_id: "_", // Will be inferred by token ID on backend
                            token_id: tokenId,
                            action: "SELL",
                            outcome: outcomeName,
                            amount_usdc: 0.0
                        })
                    });
                    const data = await res.json();
                    if (res.ok) {
                        addConsoleLine(`Vente de position réussie : ${data.data.shares} parts de ${outcomeName} vendues à ${data.data.price} USDC. PnL: ${data.data.pnl >= 0 ? '+' : ''}${data.data.pnl.toFixed(2)} USDC.`, "success");
                        fetchPortfolio();
                        fetchTrades();
                        fetchEquityHistory();
                    } else {
                        alert("Erreur de vente: " + data.detail);
                    }
                } catch (err) {
                    alert("Erreur réseau: " + err.message);
                }
            });
        });
    }

    // Fetch and update Bot Status controls
    async function fetchBotStatus() {
        try {
            const res = await fetch("/api/bot/status");
            if (!res.ok) return;
            const data = await res.json();

            updateBotStatusUI(data.is_running);

            // Sync values if form is not active/dirty
            if (document.activeElement !== selectStrategy) selectStrategy.value = data.strategy;
            if (document.activeElement !== inputInterval) inputInterval.value = data.tick_interval;
            if (document.activeElement !== inputScanLimit) inputScanLimit.value = data.max_markets_to_scan;

            // "Activer ce mode" reflects current strategy: badge when already active,
            // real action only when another strategy is selected.
            const abtn = document.getElementById("btn-activate-crypto");
            if (abtn) {
                if (data.strategy === "crypto_direction") {
                    abtn.disabled = true;
                    abtn.textContent = "✓ Mode actif";
                    abtn.classList.add("btn-active-on");
                } else {
                    abtn.disabled = false;
                    abtn.textContent = "Activer ce mode";
                    abtn.classList.remove("btn-active-on");
                }
            }

            // "Scan max" only affects Momentum/Value scanning — hide it in crypto mode
            const cryptoMode = data.strategy === "crypto_direction";
            const gScan = document.getElementById("group-scan");
            const fRow = document.getElementById("config-form-row");
            if (gScan) gScan.style.display = cryptoMode ? "none" : "";
            if (fRow) fRow.classList.toggle("single", cryptoMode);
        } catch (e) {
            console.error("Error fetching bot status:", e);
        }
    }

    function updateBotStatusUI(isRunning) {
        if (isRunning) {
            botStatusDot.className = "status-dot active";
            botStatusText.innerText = "Actif (analyse en cours)";
            btnStartBot.disabled = true;
            btnStopBot.disabled = false;
        } else {
            botStatusDot.className = "status-dot";
            botStatusText.innerText = "Arrêté (veille)";
            btnStartBot.disabled = false;
            btnStopBot.disabled = true;
        }
    }

    // Fetch real-time active markets from backend API
    async function fetchMarkets() {
        try {
            const res = await fetch("/api/markets");
            if (!res.ok) return;
            const data = await res.json();
            
            const marketsStr = JSON.stringify(data);
            if (marketsStr === recentMarketsStr) return;
            recentMarketsStr = marketsStr;
            
            cachedMarkets = data;
            renderMarketsList();
        } catch (e) {
            marketsContainer.innerHTML = `<div class="text-center text-muted">Erreur lors de la récupération des marchés.</div>`;
        }
    }

    // Filter and render markets
    function renderMarketsList() {
        if (cachedMarkets.length === 0) {
            marketsContainer.innerHTML = `<div class="text-center text-muted padded">Aucun marché actif disponible.</div>`;
            return;
        }

        const searchQuery = inputMarketSearch.value.toLowerCase().trim();
        const filtered = cachedMarkets.filter(m => {
            return m.question.toLowerCase().includes(searchQuery);
        });

        if (filtered.length === 0) {
            marketsContainer.innerHTML = `<div class="text-center text-muted padded">Aucun marché ne correspond à votre recherche.</div>`;
            return;
        }

        let html = "";
        filtered.forEach(m => {
            let outcomes = ["YES", "NO"];
            let prices = ["0.50", "0.50"];
            let clobTokens = [];
            
            try {
                outcomes = JSON.parse(m.outcomes);
                prices = JSON.parse(m.outcomePrices);
                clobTokens = JSON.parse(m.clobTokenIds);
            } catch (err) {
                // Keep default if parse fails
            }

            const volFormatted = formatNumber(parseFloat(m.volumeNum || m.volume || 0));

            // Generate price boxes dynamically for N outcomes
            let priceBoxesHtml = "";
            for (let i = 0; i < outcomes.length; i++) {
                const outcomeName = outcomes[i];
                const price = prices[i] || "0.00";
                const tokenId = clobTokens[i];
                if (!tokenId) continue;
                
                let colorClass = "yes-color";
                if (outcomeName.toLowerCase() === "no") {
                    colorClass = "no-color";
                } else if (i > 0 && outcomeName.toLowerCase() !== "yes") {
                    colorClass = "no-color";
                }
                
                priceBoxesHtml += `
                    <div class="price-box" data-market-id="${m.id}" data-token-id="${tokenId}" data-outcome-name="${outcomeName}" data-outcome-price="${price}" data-market-title="${m.question.replace(/"/g, '&quot;')}">
                        <span class="price-box-outcome">${escapeHTML(outcomeName)}</span>
                        <span class="price-box-value ${colorClass}">${Math.round(parseFloat(price)*100)}¢</span>
                    </div>
                `;
            }

            html += `
                <div class="market-item">
                    <div class="market-info">
                        <div class="market-question">${escapeHTML(m.question)}</div>
                        <div class="market-meta">
                            <span>Vol: <strong class="market-volume">${volFormatted} $</strong></span>
                            <span>Liquidité: <strong>${formatNumber(parseFloat(m.liquidityNum || m.liquidity || 0))} $</strong></span>
                        </div>
                    </div>
                    <div class="market-pricing">
                        ${priceBoxesHtml}
                    </div>
                </div>
            `;
        });
        
        marketsContainer.innerHTML = html;

        // Event listener for placing a manual order (clicking on price blocks)
        document.querySelectorAll(".price-box").forEach(box => {
            box.addEventListener("click", () => {
                const marketId = box.getAttribute("data-market-id");
                const tokenId = box.getAttribute("data-token-id");
                const outcomeName = box.getAttribute("data-outcome-name");
                const outcomePrice = box.getAttribute("data-outcome-price");
                const marketTitle = box.getAttribute("data-market-title");
                
                openTradeModal(marketId, tokenId, outcomeName, outcomePrice, marketTitle);
            });
        });
    }

    // Modal Control
    async function openTradeModal(marketId, tokenId, outcomeName, outcomePrice, marketTitle) {
        currentModalData = { marketId, tokenId, outcomeName, outcomePrice, marketTitle };
        
        modalMarketTitle.innerText = marketTitle;
        modalOutcomeName.innerText = outcomeName;
        modalOutcomePrice.innerText = parseFloat(outcomePrice).toFixed(2);
        
        // Fetch current cash balance to show limit
        try {
            const res = await fetch("/api/portfolio");
            const data = await res.json();
            modalUserBalance.innerText = data.balance.toFixed(2);
        } catch (e) {}

        tradeModal.style.display = "block";
    }

    function closeModal() {
        tradeModal.style.display = "none";
        currentModalData = null;
    }

    // Fetch bot live console logs
    async function fetchLogs() {
        try {
            const res = await fetch("/api/logs");
            if (!res.ok) return;
            const data = await res.json();
            
            // Build log DOM efficiently
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
                
                // Auto scroll to bottom if user is already near bottom
                if (shouldScroll) {
                    consoleOutput.scrollTop = consoleOutput.scrollHeight;
                }
            }
        } catch (e) {
            console.error("Error fetching logs:", e);
        }
    }

    // Add immediate line to console (for UI interactivity response)
    function addConsoleLine(text, level = "info") {
        const time = new Date().toLocaleTimeString();
        const line = document.createElement("div");
        line.className = `console-line ${level}`;
        line.innerText = `[${time}] [MANUAL] ${text}`;
        consoleOutput.appendChild(line);
        consoleOutput.scrollTop = consoleOutput.scrollHeight;
    }

    // Fetch trade execution history
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
                const date = new Date(t.timestamp + "Z").toLocaleTimeString();
                const cost = t.shares * t.price;
                const actionClass = t.action.toLowerCase();
                
                let pnlHtml = "";
                if (t.pnl !== null) {
                    const sign = t.pnl >= 0 ? "+" : "";
                    const pnlClass = t.pnl >= 0 ? "pnl-positive" : "pnl-negative";
                    pnlHtml = `<span class="history-pnl ${pnlClass}">PnL: ${sign}${t.pnl.toFixed(2)} $</span>`;
                }

                html += `
                    <div class="history-item">
                        <div class="history-header">
                            <span class="history-action ${actionClass}">${t.action} ${t.outcome}</span>
                            <span class="history-time">${date}</span>
                        </div>
                        <div class="history-body">${escapeHTML(t.question)}</div>
                        <div class="history-footer">
                            <span>${t.shares.toFixed(1)} parts @ ${t.price.toFixed(2)} $</span>
                            <span>${pnlHtml || `Valeur: ${cost.toFixed(2)} $`}</span>
                        </div>
                    </div>
                `;
            });
            historyContainer.innerHTML = html;
        } catch (e) {
            console.error("Error fetching trades:", e);
        }
    }

    // Fetch and render Crypto Direction live signals
    let recentCryptoStr = "";
    async function fetchCryptoSignals() {
        try {
            const res = await fetch("/api/crypto/signals");
            if (!res.ok) return;
            const data = await res.json();
            renderCryptoSignals(data.signals || [], data.updated_at || 0);
            renderLearning(data.learning);
        } catch (e) {
            // silent: the panel only matters in crypto_direction mode
        }
    }

    // Learning/calibration status badge
    function renderLearning(l) {
        const el = document.getElementById("crypto-learn");
        if (!el) return;
        if (!l) { el.innerText = ""; return; }
        const winrate = l.samples ? Math.round(100 * l.wins / l.samples) + "% win" : "—";
        const mode = l.calibrated ? "calibration ACTIVE" : "calibration en attente de données";
        el.classList.toggle("on", !!l.calibrated);
        el.innerText = `Apprentissage : ${l.samples} paris réglés · ${winrate} · ${mode}`;
    }

    function renderCryptoSignals(signals, updatedAt) {
        const body = document.getElementById("crypto-signals-body");
        const upd = document.getElementById("crypto-updated");
        if (!body) return;

        // Avoid redundant re-renders (countdown still updates because t_left changes)
        const sigStr = JSON.stringify(signals);
        if (sigStr === recentCryptoStr) return;
        recentCryptoStr = sigStr;

        if (!signals.length) {
            body.innerHTML = `<tr><td colspan="11" class="text-center text-muted">Aucun marché Up/Down dans la fenêtre d'analyse. Activez la stratégie « Crypto Direction » et démarrez le bot.</td></tr>`;
            if (upd) upd.innerText = "";
            return;
        }

        let html = "";
        signals.forEach(s => {
            const deltaClass = s.delta_pct >= 0 ? "pnl-positive" : "pnl-negative";
            const deltaSign = s.delta_pct >= 0 ? "+" : "";

            // Best available edge across both sides
            let edge = null;
            if (s.edge_up !== null && s.edge_up !== undefined) edge = s.edge_up;
            if (s.edge_down !== null && s.edge_down !== undefined) edge = (edge === null) ? s.edge_down : Math.max(edge, s.edge_down);
            const edgeShown = (edge === null) ? "—" : (edge >= 0 ? "+" : "") + edge.toFixed(3);
            const edgeClass = (edge === null) ? "text-muted" : (edge > 0.06 ? "pnl-positive" : (edge > 0 ? "" : "pnl-negative"));

            // Time-left urgency colour
            const tClass = s.t_left <= 30 ? "pnl-negative" : (s.t_left <= 120 ? "" : "text-muted");

            // P(up) as a mini bar
            const pct = Math.round(s.p_up * 100);
            const pbar = `<div class="pbar"><div class="pbar-fill" style="width:${pct}%"></div><span class="pbar-label">${pct}%</span></div>`;

            // Order-flow (Binance aggressor) — the user's "watch buyers vs sellers" idea
            let flowHtml = `<span class="text-muted">—</span>`;
            if (s.flow !== null && s.flow !== undefined) {
                const f = s.flow;
                const fcls = f > 0.1 ? "pnl-positive" : (f < -0.1 ? "pnl-negative" : "text-muted");
                const farrow = f > 0.1 ? "▲" : (f < -0.1 ? "▼" : "→");
                flowHtml = `<span class="${fcls}">${farrow} ${f >= 0 ? "+" : ""}${f.toFixed(2)}</span>`;
            }

            let actionHtml = "—";
            if (s.action) actionHtml = `<span class="history-action buy">${escapeHTML(s.action)}</span>`;
            else if (s.held) actionHtml = `<span class="text-muted">Détenu</span>`;

            html += `
                <tr>
                    <td>${escapeHTML(s.asset)}</td>
                    <td>${escapeHTML(s.window)}</td>
                    <td class="num">${fmtSpot(s.spot)}</td>
                    <td class="num">${fmtSpot(s.open_ref)}</td>
                    <td class="num ${deltaClass}">${deltaSign}${s.delta_pct.toFixed(3)}%</td>
                    <td class="num ${tClass}">${fmtCountdown(s.t_left)}</td>
                    <td>${pbar}</td>
                    <td class="num"><span class="yes-color">${Math.round(s.up_price * 100)}¢</span> / <span class="no-color">${Math.round(s.down_price * 100)}¢</span></td>
                    <td class="num">${flowHtml}</td>
                    <td class="num ${edgeClass}"><strong>${edgeShown}</strong></td>
                    <td>${actionHtml}</td>
                </tr>
            `;
        });
        body.innerHTML = html;
        if (upd && updatedAt) upd.innerText = "maj " + new Date(updatedAt * 1000).toLocaleTimeString();
    }

    // Build Lightweight Charts series from equity history. Keyed by unix-second to
    // guarantee unique, strictly-ascending timestamps (a Lightweight Charts requirement).
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
            lineStyle: 2, // dashed
            priceLineVisible: false,
            lastValueVisible: false
        });
        return { area, cash };
    }

    // Fetch and render equity curve (Lightweight Charts)
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

    // Initialize the main Lightweight Charts instance
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

    // Initialize the large (modal) Lightweight Charts instance
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

    // Render large chart from cached history
    function renderLargeChartData() {
        if (!largeAreaSeries || !cachedEquityHistory) return;
        const { total, cash } = buildSeriesData(cachedEquityHistory);
        largeAreaSeries.setData(total);
        largeCashSeries.setData(cash);
        largeChart.timeScale().fitContent();
    }

    // --- UTILS ---

    // Adaptive price formatting (BTC 63 297 vs DOGE 0.0912)
    function fmtSpot(p) {
        if (p == null) return "—";
        if (p >= 1000) return p.toLocaleString("fr-FR", { maximumFractionDigits: 0 });
        if (p >= 1) return p.toFixed(2);
        return p.toFixed(4);
    }

    // Countdown: m:ss for long windows, Ns when close
    function fmtCountdown(secs) {
        if (secs == null) return "—";
        if (secs >= 90) {
            const m = Math.floor(secs / 60);
            const s = secs % 60;
            return m + ":" + String(s).padStart(2, "0");
        }
        return secs + "s";
    }

    function formatUSD(num) {
        return num.toLocaleString("fr-FR", { minimumFractionDigits: 2, maximumFractionDigits: 2 }) + " $";
    }

    function formatNumber(num) {
        if (num >= 1000000) {
            return (num / 1000000).toFixed(1) + "M";
        }
        if (num >= 1000) {
            return (num / 1000).toFixed(1) + "k";
        }
        return num.toFixed(0);
    }

    function escapeHTML(str) {
        return str.replace(/[&<>'"]/g, 
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
            return d.toLocaleDateString("fr-FR", {
                day: "2-digit",
                month: "2-digit",
                year: "numeric"
            }) + " " + d.toLocaleTimeString("fr-FR", {
                hour: "2-digit",
                minute: "2-digit"
            });
        } catch (e) {
            return "—";
        }
    }
});
