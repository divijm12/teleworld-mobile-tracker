// Phone-picker dashboard: cascading dropdowns -- pick a phone, then a color
// (options narrow to what that phone actually has), then storage (narrowed
// to that phone+color), then RAM (narrowed to that phone+color+storage) --
// landing on the exact tracked variant's price/stock/offers. All data
// (VARIANTS) is embedded server-side (see index.html) -- no server
// round-trip needed at any step. No framework.

function escapeHtml(value) {
  const div = document.createElement("div");
  div.textContent = value == null ? "" : String(value);
  return div.innerHTML;
}

function formatPrice(price) {
  return price ? `&#8377;${price.toLocaleString("en-IN")}` : '<span class="muted">unknown</span>';
}

function offerRowHtml(offer) {
  const flagged = !offer.is_offer || offer.confidence === "low";
  let badge = "";
  if (!offer.is_offer) {
    badge = '<span class="badge badge-error" title="Flagged as not a real offer">not an offer</span>';
  } else if (offer.confidence === "low") {
    badge = '<span class="badge badge-warn" title="Low-confidence parse">low confidence</span>';
  }
  return `<li class="${flagged ? "offer-flagged" : ""}">${escapeHtml(offer.summary)}${badge}</li>`;
}

function variantRowHtml(v) {
  let timestampBadges = "";
  if (v.fetch_error) {
    timestampBadges += `<span class="badge badge-error" title="${escapeHtml(v.fetch_error)}">fetch error</span>`;
  }
  if (v.match_warning) {
    timestampBadges += `<span class="badge badge-warn" title="${escapeHtml(v.match_warning)}">label mismatch</span>`;
  }

  const offersHtml = v.offers && v.offers.length
    ? `<ul>${v.offers.map(offerRowHtml).join("")}</ul>`
    : '<span class="muted">no offers found</span>';

  // v.in_stock is false (confirmed out of stock), true (confirmed in
  // stock), or null/undefined (couldn't be determined) -- only the
  // explicit false case gets flagged, not "unknown".
  const outOfStock = v.in_stock === false;
  const outOfStockBadge = outOfStock
    ? '<span class="badge badge-error" title="Not currently buyable on Flipkart -- price/offers below are from the last time it was in stock">Out of Stock</span>'
    : "";

  const bestOfferHtml = v.best_offer_summary
    ? escapeHtml(v.best_offer_summary)
    : '<span class="muted">no usable offer</span>';
  const effectivePriceHtml = v.effective_price != null
    ? formatPrice(v.effective_price)
    : '<span class="muted">—</span>';

  // is_new_low is strictly "today's effective price is at or below every
  // past effective price for this exact variant" (server-computed against
  // the variant_historical_low DB view) -- null means there wasn't enough
  // data (no price, or no history yet) to say either way.
  let priceSignalHtml = '<span class="muted">—</span>';
  if (v.is_new_low === true) {
    priceSignalHtml = '<span class="badge badge-ok" title="Today\'s effective price is the lowest ever recorded for this variant">Best price yet</span>';
  } else if (v.is_new_low === false) {
    priceSignalHtml = `<span class="badge badge-warn" title="Lowest ever recorded: ${escapeHtml(v.historical_low_at_display)}">Seen cheaper: ${formatPrice(v.historical_low_price)} on ${escapeHtml(v.historical_low_at_display)}</span>`;
  }

  // alert_reason is only set when the CURRENT snapshot is what triggered
  // the alert (see dashboard.py) -- not a stale alert from a since-changed
  // price. No delivery channel exists yet (no email/Slack/push), so this
  // badge is currently the only place the decision is surfaced.
  const ALERT_BADGE_LABELS = {
    new_low: "New low",
    price_drop: "Price drop",
    back_in_stock: "Back in stock",
    back_in_stock_new_low: "Back in stock + new low",
    back_in_stock_price_drop: "Back in stock + price drop",
  };
  // Not escapeHtml()'d: alertSummaryText() already returns safe HTML (its
  // dynamic parts are all formatPrice()/toFixed() numeric output, no raw
  // strings), and title attributes decode entities the same as text
  // content -- same "don't double-escape" reasoning as the panel below.
  const alertLabel = ALERT_BADGE_LABELS[v.alert_reason];
  const alertHtml = alertLabel
    ? `<span class="badge badge-alert" title="${alertSummaryText(v)}">&#128276; ${alertLabel}</span>`
    : "";
  if (alertHtml) priceSignalHtml = `${alertHtml} ${priceSignalHtml}`;

  return `
    <tr class="variant-row${v.has_flagged_offer ? " flagged" : ""}${outOfStock ? " out-of-stock" : ""}">
      <td>${v.ram ? escapeHtml(v.ram) : '<span class="muted">unknown</span>'}</td>
      <td>${escapeHtml(v.storage)}</td>
      <td>${escapeHtml(v.color)}</td>
      <td class="price">${formatPrice(v.price)} ${outOfStockBadge}</td>
      <td class="best-offer">${bestOfferHtml}</td>
      <td class="price effective-price">${effectivePriceHtml}</td>
      <td class="price-signal">${priceSignalHtml}</td>
      <td class="timestamp">${escapeHtml(v.fetched_at_display)} ${timestampBadges}</td>
      <td class="offers">${offersHtml}</td>
    </tr>`;
}

function renderEmptyState(message) {
  document.getElementById("results").innerHTML = `<p class="empty-state">${escapeHtml(message)}</p>`;
}

function renderResults(matches) {
  const results = document.getElementById("results");
  if (!matches.length) {
    results.innerHTML = '<p class="empty-state">No variant found for this exact combination.</p>';
    return;
  }
  results.innerHTML = `
    <table>
      <thead>
        <tr>
          <th>RAM</th>
          <th>Storage</th>
          <th>Color</th>
          <th>Price</th>
          <th>Best Offer</th>
          <th>Effective Price</th>
          <th>Price Signal</th>
          <th>Last checked</th>
          <th>Offers</th>
        </tr>
      </thead>
      <tbody>${matches.map(variantRowHtml).join("")}</tbody>
    </table>`;
}

// Sorts "64 GB"/"128 GB"/"12 GB" etc. by their leading number, not
// alphabetically (alphabetical would put "128 GB" before "64 GB").
function sortByLeadingNumber(values) {
  return [...values].sort((a, b) => (parseInt(a, 10) || 0) - (parseInt(b, 10) || 0));
}

function populateSelect(select, values, placeholder) {
  select.innerHTML = `<option value="">${placeholder}</option>`;
  for (const v of values) {
    const option = document.createElement("option");
    option.value = v;
    option.textContent = v;
    select.appendChild(option);
  }
}

function hideStep(barId, selectId) {
  document.getElementById(barId).hidden = true;
  document.getElementById(selectId).innerHTML = "";
}

// If a step only has one real option, pick it automatically and cascade
// straight to populating the next step -- most phones don't actually vary
// on every axis (e.g. one color only comes in one storage), so this avoids
// forcing a click through steps that were never really a choice, while the
// dropdown is still there and changeable if the user wants to double-check.
function maybeAutoAdvance(select, values, onChange) {
  if (values.length === 1) {
    select.value = values[0];
    onChange();
  }
}

function onPhoneChange() {
  const model = document.getElementById("phone-select").value;
  hideStep("storage-bar", "storage-select");
  hideStep("ram-bar", "ram-select");

  if (!model) {
    hideStep("color-bar", "color-select");
    renderEmptyState("Select a phone above to begin.");
    return;
  }

  const colors = [...new Set(VARIANTS.filter((v) => v.model === model).map((v) => v.color))].sort((a, b) =>
    a.localeCompare(b)
  );
  const colorSelect = document.getElementById("color-select");
  populateSelect(colorSelect, colors, "-- choose a color --");
  document.getElementById("color-bar").hidden = false;
  renderEmptyState("Select a color to continue.");

  maybeAutoAdvance(colorSelect, colors, onColorChange);
}

function onColorChange() {
  const model = document.getElementById("phone-select").value;
  const color = document.getElementById("color-select").value;
  hideStep("ram-bar", "ram-select");

  if (!color) {
    hideStep("storage-bar", "storage-select");
    renderEmptyState("Select a color to continue.");
    return;
  }

  const storages = sortByLeadingNumber([
    ...new Set(VARIANTS.filter((v) => v.model === model && v.color === color).map((v) => v.storage)),
  ]);
  const storageSelect = document.getElementById("storage-select");
  populateSelect(storageSelect, storages, "-- choose storage --");
  document.getElementById("storage-bar").hidden = false;
  renderEmptyState("Select storage to continue.");

  maybeAutoAdvance(storageSelect, storages, onStorageChange);
}

function onStorageChange() {
  const model = document.getElementById("phone-select").value;
  const color = document.getElementById("color-select").value;
  const storage = document.getElementById("storage-select").value;

  if (!storage) {
    hideStep("ram-bar", "ram-select");
    renderEmptyState("Select storage to continue.");
    return;
  }

  const rams = sortByLeadingNumber([
    ...new Set(
      VARIANTS.filter((v) => v.model === model && v.color === color && v.storage === storage).map(
        (v) => v.ram || "Unknown"
      )
    ),
  ]);
  const ramSelect = document.getElementById("ram-select");
  populateSelect(ramSelect, rams, "-- choose RAM --");
  document.getElementById("ram-bar").hidden = false;
  renderEmptyState("Select RAM to see pricing and offers.");

  maybeAutoAdvance(ramSelect, rams, onRamChange);
}

function onRamChange() {
  const model = document.getElementById("phone-select").value;
  const color = document.getElementById("color-select").value;
  const storage = document.getElementById("storage-select").value;
  const ram = document.getElementById("ram-select").value;

  if (!ram) {
    renderEmptyState("Select RAM to see pricing and offers.");
    return;
  }

  const matches = VARIANTS.filter(
    (v) => v.model === model && v.color === color && v.storage === storage && (v.ram || "Unknown") === ram
  );
  renderResults(matches);
}

function populatePhoneSelect() {
  const select = document.getElementById("phone-select");

  const byBrand = new Map();
  for (const v of VARIANTS) {
    const brand = v.brand || "Other";
    if (!byBrand.has(brand)) byBrand.set(brand, new Set());
    byBrand.get(brand).add(v.model);
  }

  const brands = [...byBrand.keys()].sort((a, b) => a.localeCompare(b));
  for (const brand of brands) {
    const group = document.createElement("optgroup");
    group.label = brand;
    const models = [...byBrand.get(brand)].sort((a, b) => a.localeCompare(b));
    for (const model of models) {
      const option = document.createElement("option");
      option.value = model;
      option.textContent = model;
      group.appendChild(option);
    }
    select.appendChild(group);
  }
}

populatePhoneSelect();
renderEmptyState("Select a phone above to begin.");

document.getElementById("phone-select").addEventListener("change", onPhoneChange);
document.getElementById("color-select").addEventListener("change", onColorChange);
document.getElementById("storage-select").addEventListener("change", onStorageChange);
document.getElementById("ram-select").addEventListener("change", onRamChange);

// Alert panel: same variants the header count is built from (alert_reason
// set means the variant's *current* snapshot triggered an alert -- see
// dashboard.py), not a full historical alert log.
const ALERTS = VARIANTS.filter((v) => v.alert_reason);

function priceDropPhrase(v) {
  const drop = v.alert_reference_price - v.effective_price;
  const pct = (drop / v.alert_reference_price) * 100;
  return `${pct.toFixed(1)}% (${formatPrice(drop)}) cheaper than the ${formatPrice(v.alert_reference_price)} you were last alerted about`;
}

function alertSummaryText(v) {
  switch (v.alert_reason) {
    case "new_low":
      return `New all-time low: ${formatPrice(v.effective_price)}`;
    case "price_drop":
      return v.alert_reference_price != null
        ? `Dropped ${priceDropPhrase(v)}, now ${formatPrice(v.effective_price)}`
        : "Price update";
    case "back_in_stock":
      return `Back in stock at ${formatPrice(v.effective_price)} (unchanged from before it went out of stock)`;
    case "back_in_stock_new_low":
      return `Back in stock + new all-time low: ${formatPrice(v.effective_price)}`;
    case "back_in_stock_price_drop":
      return v.alert_reference_price != null
        ? `Back in stock + price drop: ${priceDropPhrase(v)}, now ${formatPrice(v.effective_price)}`
        : `Back in stock at ${formatPrice(v.effective_price)}`;
    default:
      return "Price update";
  }
}

function renderAlertPanel() {
  const panel = document.getElementById("alert-panel");
  if (!panel) return;
  if (!ALERTS.length) {
    panel.innerHTML = '<p class="empty-state">No active alerts.</p>';
    return;
  }
  // alertSummaryText() embeds formatPrice()'s output, which is already
  // HTML (a "&#8377;..." entity) -- escaping the whole string a second
  // time here would turn that back into literal "&amp;#8377;" text, same
  // as bestOfferHtml/effectivePriceHtml deliberately skip escaping for
  // the same reason elsewhere in this file.
  panel.innerHTML = ALERTS.map((v, i) => `
    <button type="button" class="alert-row" data-index="${i}">
      <div class="alert-row-title">${escapeHtml(v.model)} &middot; ${escapeHtml(v.storage)} &middot; ${escapeHtml(v.color)}${v.ram ? " &middot; " + escapeHtml(v.ram) : ""}</div>
      <div class="alert-row-summary">${alertSummaryText(v)}</div>
      <div class="alert-row-time">${escapeHtml(v.alert_created_at_display || "")}</div>
    </button>`).join("");
}

// Jumps the cascading dropdowns straight to the alerted variant so its
// full price/offer detail is visible, rather than making the user
// re-select phone/color/storage/RAM by hand after reading the summary.
function selectVariant(v) {
  const phoneSelect = document.getElementById("phone-select");
  phoneSelect.value = v.model;
  onPhoneChange();

  const colorSelect = document.getElementById("color-select");
  colorSelect.value = v.color;
  onColorChange();

  const storageSelect = document.getElementById("storage-select");
  storageSelect.value = v.storage;
  onStorageChange();

  const ramSelect = document.getElementById("ram-select");
  ramSelect.value = v.ram || "Unknown";
  onRamChange();

  document.getElementById("results").scrollIntoView({ behavior: "smooth" });
}

// Out-of-stock panel: same dropdown pattern as the alert panel above --
// same wrapper markup, same .alert-panel/.alert-row classes (styling is
// identical, only the content differs), same click-through-to-variant
// behavior.
const OUT_OF_STOCK = VARIANTS.filter((v) => v.in_stock === false);

function renderStockPanel() {
  const panel = document.getElementById("stock-panel");
  if (!panel) return;
  if (!OUT_OF_STOCK.length) {
    panel.innerHTML = '<p class="empty-state">Nothing currently out of stock.</p>';
    return;
  }
  panel.innerHTML = OUT_OF_STOCK.map((v, i) => `
    <button type="button" class="alert-row" data-index="${i}">
      <div class="alert-row-title">${escapeHtml(v.model)} &middot; ${escapeHtml(v.storage)} &middot; ${escapeHtml(v.color)}${v.ram ? " &middot; " + escapeHtml(v.ram) : ""}</div>
      <div class="alert-row-summary stock-row-summary">Last checked ${formatPrice(v.price)} &middot; ${escapeHtml(v.fetched_at_display)}</div>
    </button>`).join("");
}

function setupDropdownPanel(btnId, panelId, items, onSelectItem) {
  const btn = document.getElementById(btnId);
  const panel = document.getElementById(panelId);
  if (!btn || !panel) return;

  btn.addEventListener("click", (e) => {
    e.stopPropagation();
    const opening = panel.hidden;
    // Only one dropdown open at a time.
    document.querySelectorAll(".alert-panel").forEach((p) => { p.hidden = true; });
    panel.hidden = !opening;
  });

  panel.addEventListener("click", (e) => {
    const row = e.target.closest(".alert-row");
    if (!row) return;
    onSelectItem(items[Number(row.dataset.index)]);
    panel.hidden = true;
  });

  document.addEventListener("click", (e) => {
    if (!panel.hidden && !panel.contains(e.target) && e.target !== btn) {
      panel.hidden = true;
    }
  });
}

renderAlertPanel();
setupDropdownPanel("alert-count-btn", "alert-panel", ALERTS, selectVariant);

renderStockPanel();
setupDropdownPanel("stock-count-btn", "stock-panel", OUT_OF_STOCK, selectVariant);
