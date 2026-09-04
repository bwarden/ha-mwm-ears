/**
 * mwm-ears-card: Lovelace custom card for the `mwm_ears` Home Assistant
 * integration.
 *
* Shows the three ear-color palettes (Left / Both / Right) with every
 * representable color, plus an effects picker, and drives the integration:
 *
 *   - a palette swatch pick calls the integration's `select_color` action
 *     with the exact catalog selector (`simple:0x61`, `palette:4`), so
 *     near-identical shades that are distinct protocol commands (simple
 *     0x61 blue vs palette 0x04 pure blue) are sent distinct -- the color
 *     wheel's RGB round-trip cannot tell them apart, but the action can;
 *   - the on/off buttons and the effects picker call `light.turn_on` /
 *     `light.turn_off` on the Both entity -- effect programs are room-wide,
 *     not per-ear.
 *
 * The swatches are read from the entity's `color_palette` attribute
 * (rendered by python/custom_components/mwm_ears/_mwm/palette.py::
 * color_palette), so the card always shows exactly what the integration can
 * represent and needs no palette copy of its own. The active swatch is
 * matched against the entity's `color_identity` attribute (kind plus
 * code/index), never by RGB.
 *
 * Usage:
 *   - Register this file as a Lovelace resource (see python/README.md).
 *  - Configure with the "ears" (Both) light entity as the minimum:
 *      { "type": "custom:mwm-ears-card", "entity": "light.ears" }
 *  left_entity / right_entity are optional: the card auto-detects them as
 *  the `side`-stamped lights sharing the Both entity's Home Assistant
 *  device. Set them only to override the auto-detection. A side whose
 *  entity is neither configured nor detected renders its palette read-only.
 *  Set "mode": "batch" to start in batch/queued mode (default is live).
 */
const MWM_CARD_TAG = "mwm-ears-card";
const MWM_EDITOR_TAG = "mwm-ears-card-editor";

class MwmEarsCard extends HTMLElement {
  static getConfigElement() {
    return document.createElement(MWM_EDITOR_TAG);
  }
  static getStubConfig() {
    return { entity: "" };
  }

  setConfig(config) {
    if (!config || !config.entity) {
      throw new Error("You need to define 'entity' (the Both-ear light entity).");
    }
    this._config = { ...config, title: config.title || "MWM Ears" };
    this._lastKey = null;
    this._sides = null;
    // Live mode transmits each pick immediately; batch mode queues colors and
    // one effect until the user hits Transmit (which sends effect-then-color
    // in a single set_state). `mode` survives redraws via the config.
    this._batch = config.mode === "batch";
    if (!this._pending) this._pending = { left: null, right: null, both: null, effect: null };
    this._reconcile();
  }

  set hass(hass) {
    this._hass = hass;
    this._resolveSides();
    const key = this._stateKey(hass);
    if (key !== this._lastKey) {
      this._lastKey = key;
      this._reconcile();
    }
  }

  getCardSize() {
    return 6;
  }

  connectedCallback() {
    if (!this.shadowRoot) this.attachShadow({ mode: "open" });
    if (this._hass) this._reconcile();
  }

  // Rebuild the DOM only when the underlying state actually changed, to keep
  // slider/focus state and avoid churn on unrelated hass updates.
  _stateKey(hass) {
    const ids = [(this._config && this._config.entity),
                 this._sides && this._sides.left,
                 this._sides && this._sides.right].filter(Boolean);
    return ids.map((id) => {
      const s = hass && hass.states && hass.states[id];
      if (!s) return "none";
      const a = s.attributes || {};
      // color_identity is a {kind, code|index} OBJECT: template coercion
      // would collapse every identity to the same "[object Object]" and miss
      // a palette->simple switch whose RGB round-trips identically (pure
      // green palette 25 vs simple green 0x62 both come back 0,255,0).
      const identity = a.color_identity ? JSON.stringify(a.color_identity) : "";
      const rgb = Array.isArray(a.rgb_color) ? a.rgb_color.join(",") : "";
      return `${s.state}:${rgb}:${identity}:${a.effect}`;
    }).join("|");
  }

  // Section key -> entity id. "entity" is the Both config entity; the side
  // sections key into _sides (left/right), which holds the configured
  // entity or the auto-detected sibling. _sides does NOT use the
  // "left_entity"/"right_entity" config names, so lookups must translate.
  _entityOf(key) {
    if (key === "entity") return (this._config && this._config.entity) || "";
    const side = key === "left_entity" ? "left"
      : key === "right_entity" ? "right" : key;
    return (this._sides && this._sides[side]) || "";
  }

  _stateOf(key) {
    const id = this._entityOf(key);
    return id && this._hass ? this._hass.states[id] : undefined;
  }

  // Left/right entities for this card: explicit config wins, otherwise the
  // side entities derived from the Both entity.
  _resolveSides() {
    const sides = {
      both: (this._config && this._config.entity) || "",
      left: (this._config && this._config.left_entity) || "",
      right: (this._config && this._config.right_entity) || "",
    };
    const derived = this._deriveSides();
    if (!sides.left && derived.left) sides.left = derived.left;
    if (!sides.right && derived.right) sides.right = derived.right;
    this._sides = sides;
  }

  // The integration registers the left/both/right lights on ONE HA device per
  // room (light.py: DeviceInfo identifiers {(DOMAIN, f"mwm_ears_<entry>")})
  // and stamps each entity with a `side` attribute. We find the siblings
  // through the entity registry's device_id rather than by name, so detection
  // survives any entity rename and needs no name conventions.
  _deriveSides() {
    const out = { left: "", right: "" };
    const hass = this._hass;
    if (!hass || !this._config || !this._config.entity) return out;
    const reg = hass.entities;
    if (!reg) return out;
    const bothReg = reg[this._config.entity];
    const deviceId = bothReg && bothReg.device_id;
    if (!deviceId) return out;
    for (const s of Object.values(hass.states)) {
      const a = s.attributes || {};
      const side = a.side;
      if (s.entity_id && s.entity_id.startsWith("light.") &&
          (side === "left" || side === "right")) {
        const ent = reg[s.entity_id];
        if (ent && ent.device_id === deviceId) out[side] = s.entity_id;
      }
    }
    return out;
  }

  _call(entityId, domain, service, data) {
    if (!entityId || !this._hass) return;
    this._hass.callService(domain, service, { entity_id: entityId, ...data });
  }

  // Batch mode: queue a color pick for a section (queued, not sent). Side
  // keys "left_entity"/"right_entity" map straight into the pending object;
  // "off" means turn that side off in the batch.
  _queueColor(sideKey, color) {
    const slot = sideKey === "left_entity" ? "left"
      : sideKey === "right_entity" ? "right" : "both";
    this._pending[slot] = color;
    this._reconcile();
  }

  _queueEffect(effectName) {
    this._pending.effect = effectName;
    this._reconcile();
  }

  // Human summary of a queued pick (color spec or "off").
  _pickLabel(v) {
    if (v == null) return "";
    if (v === "off") return "off";
    return v.replace(/^simple:/, "").replace(/^palette:/, "P:");
  }

  _pendingSummary() {
    const p = this._pending || {};
    const parts = [];
    if (p.left) parts.push("L=" + this._pickLabel(p.left));
    if (p.right) parts.push("R=" + this._pickLabel(p.right));
    if (p.both) parts.push("B=" + this._pickLabel(p.both));
    if (p.effect) parts.push("fx=" + p.effect);
    return parts.length ? parts.join(" ") : "nothing queued";
  }

  // Send the whole batch as one set_state: effect first, then colors
  // (server applies effect-then-color so the running effect adopts them).
  _transmitBatch() {
    const p = this._pending || {};
    const both = p.both;
    const data = {};
    if (p.left) data.left_color = p.left === "off" ? "off" : p.left;
    if (p.right) data.right_color = p.right === "off" ? "off" : p.right;
    if (both) data.color = both === "off" ? "off" : both;
    if (p.effect) data.effect = p.effect;
    if (Object.keys(data).length) {
      this._call(this._config.entity, "mwm_ears", "set_state", data);
    }
    this._pending = { left: null, right: null, both: null, effect: null };
    this._reconcile();
  }

  _clearBatch() {
    this._pending = { left: null, right: null, both: null, effect: null };
    this._reconcile();
  }

  _rgbOf(state) {
    if (!state) return null;
    const a = state.attributes || {};
    if (Array.isArray(a.rgb_color)) return a.rgb_color;
    if (Array.isArray(a.hs_color) && a.hs_color.length >= 2) {
      return hslToRgb(a.hs_color[0], a.hs_color[1], 0.5);
    }
    return null;
  }

  _palette(state, fallback) {
    const a = state && state.attributes;
    if (a && Array.isArray(a.color_palette) && a.color_palette.length) return a.color_palette;
    const b = fallback && fallback.attributes;
    if (b && Array.isArray(b.color_palette)) return b.color_palette;
    return [];
  }

  _swatchActive(c, state) {
    // Match by catalog identity (kind + code/index) when the integration
    // exposes it -- near-identical shades are distinct commands and RGB
    // cannot tell a 0xFE from a 0xFF through the HS round-trip. Fall back
    // to exact RGB equality only for older integration versions.
    const a = state && state.attributes;
    const id = a && a.color_identity;
    if (id) {
      return (
        id.kind === c.kind &&
        (c.kind === "simple" ? id.code === c.code : id.index === c.index)
      );
    }
    const current = this._rgbOf(state);
    if (!current || !c.rgb) return false;
    return (
      current[0] === c.rgb[0] &&
      current[1] === c.rgb[1] &&
      current[2] === c.rgb[2]
    );
  }

  _buildPaletteSection(sideKey, title, subtitle, bothState, { enabled = true } = {}) {
    const section = document.createElement("div");
    section.className = "palette";

    const heading = document.createElement("div");
    heading.className = "heading";
    heading.textContent = title;
    section.appendChild(heading);

    if (!enabled) {
      const n = document.createElement("div");
      n.className = "note";
      n.textContent = `No entity for this ear. It is auto-detected from the Both entity's device; set "${sideKey}" to override.`;
      section.appendChild(n);
      return section;
    }

    const state = this._stateOf(sideKey);
    const catalog = this._palette(state, bothState);
    const entityId = this._entityOf(sideKey);

    const grid = document.createElement("div");
    grid.className = "swatches";

    for (const c of catalog) {
      const rgb = c.rgb || [0, 0, 0];
      const btn = document.createElement("button");
      btn.className = "swatch" + (this._swatchActive(c, state) ? " active" : "");
      btn.style.background = `rgb(${rgb.join(",")})`;
      btn.title = c.name;
      const label = document.createElement("span");
      label.textContent = c.name;
      btn.appendChild(label);
      btn.addEventListener("click", () => {
        const color = c.kind === "simple"
          ? `simple:0x${c.code.toString(16)}`
          : `palette:${c.index}`;
        if (this._batch) this._queueColor(sideKey, color);
        else this._call(entityId, "mwm_ears", "select_color", { color });
      });
      grid.appendChild(btn);
    }

    // Per-ear sections get an "Off" circle: turns JUST this ear off via the
    // light entity's turn_off (server: a left-only/form or both+restore
    // composition, leaving the other ear untouched), highlighted when the
    // ear is off. The Both section leaves off to the global On/Off row.
    if (sideKey !== "entity") {
      const offBtn = document.createElement("button");
      offBtn.className = "swatch off-ear" + (state && state.state === "off" ? " active" : "");
      offBtn.title = "Turn this ear off";
      const offSpan = document.createElement("span");
      offSpan.textContent = "Off";
      offBtn.appendChild(offSpan);
      offBtn.addEventListener("click", () => {
        if (this._batch) this._queueColor(sideKey, "off");
        else this._call(entityId, "light", "turn_off", {});
      });
      grid.appendChild(offBtn);
    }

    section.appendChild(grid);
    if (subtitle) {
      const n = document.createElement("div");
      n.className = "note";
      n.textContent = subtitle;
      section.appendChild(n);
    }
    return section;
  }

  _buildOnOff() {
    const section = document.createElement("div");
    section.className = "onoff";
    const grid = document.createElement("div");
    grid.className = "swatches";

    const make = (label, cls, fn) => {
      const btn = document.createElement("button");
      btn.className = "swatch " + cls;
      const span = document.createElement("span");
      span.textContent = label;
      btn.appendChild(span);
      btn.addEventListener("click", fn);
      grid.appendChild(btn);
      return btn;
    };

    make("On", "on", () => this._call(this._config.entity, "light", "turn_on", {}));
    make("Off", "off", () => this._call(this._config.entity, "light", "turn_off", {}));

    section.appendChild(grid);
    return section;
  }

  _buildEffects(bothState) {
    const section = document.createElement("div");
    section.className = "effects";

    const heading = document.createElement("div");
    heading.className = "heading";
    heading.textContent = "Effects";
    section.appendChild(heading);

    const effects =
      bothState && Array.isArray(bothState.attributes.effect_list)
        ? bothState.attributes.effect_list
        : [];

    const row = document.createElement("div");
    row.className = "effects-row";

    const label = document.createElement("span");
    label.textContent = "Run effect:";

    const select = document.createElement("select");
    const current = (bothState && bothState.attributes.effect) || "";
    const placeholder = document.createElement("option");
    placeholder.value = "";
    placeholder.textContent = current ? "Now: " + current : "Select an effect…";
    select.appendChild(placeholder);
    for (const e of effects) {
      const opt = document.createElement("option");
      opt.value = e;
      opt.textContent = e;
      if (e === current) opt.selected = true;
      select.appendChild(opt);
    }
    select.addEventListener("change", () => {
      if (select.value) {
        if (this._batch) this._queueEffect(select.value);
        else this._call(this._config.entity, "light", "turn_on", { effect: select.value });
        select.value = "";
      }
    });

    row.appendChild(label);
    row.appendChild(select);
    section.appendChild(row);

    // "Restore color" clears a running effect and re-issues the remembered
    // color (a bare turn_on restores it); it does NOT turn the ears off.
    const restore = document.createElement("button");
    restore.textContent = "Restore color";
    restore.addEventListener("click", () => {
      this._call(this._config.entity, "light", "turn_on", {});
    });
    section.appendChild(restore);

    if (effects.length === 0) {
      const n = document.createElement("div");
      n.className = "note";
      n.textContent = "No effect list on the Both entity. Point 'entity' at the 'Ears' (Both) light.";
      section.appendChild(n);
    }
    return section;
  }

  _buildModeRow() {
    const row = document.createElement("div");
    row.className = "mode-row";

    const label = document.createElement("span");
    label.className = "mode-label";
    label.textContent = this._batch ? "Batch mode" : "Live mode";
    row.appendChild(label);

    const toggle = document.createElement("button");
    toggle.className = "mode-toggle";
    toggle.textContent = this._batch ? "Switch to live" : "Queue picks (batch)";
    toggle.addEventListener("click", () => {
      this._batch = !this._batch;
      if (this._batch && !this._pending) this._pending = { left: null, right: null, both: null, effect: null };
      // Persist the mode so redraws/restarts keep it.
      const cfg = { ...this._config };
      if (this._batch) cfg.mode = "batch";
      else { delete cfg.mode; this._pending = null; }
      this._config = cfg;
      this._reconcile();
    });
    row.appendChild(toggle);

    return row;
  }

  _buildBatchBar() {
    const bar = document.createElement("div");
    bar.className = "batch-bar";

    const summary = document.createElement("span");
    summary.className = "batch-summary";
    summary.textContent = this._pendingSummary();
    bar.appendChild(summary);

    const transmit = document.createElement("button");
    transmit.className = "batch-transmit";
    transmit.textContent = "Transmit";
    const p = this._pending || {};
    const ready = (p.left || p.right || p.both || p.effect);
    transmit.disabled = !ready;
    transmit.addEventListener("click", () => this._transmitBatch());
    bar.appendChild(transmit);

    const clear = document.createElement("button");
    clear.className = "batch-clear";
    clear.textContent = "Clear";
    clear.disabled = !ready;
    clear.addEventListener("click", () => this._clearBatch());
    bar.appendChild(clear);

    return bar;
  }

  _reconcile() {
    if (!this.shadowRoot) return;
    if (!this._sides) this._resolveSides();
    const root = this.shadowRoot;
    root.innerHTML = "";

    const style = document.createElement("style");
    style.textContent = `
      :host { --mwm-accent: var(--primary-color, #03a9f4); }
      .card { font-family: var(--paper-font-body1_-_font-family, inherit); }
      .title { font-size: 1.1rem; font-weight: 600; margin: 0 0 12px; }
      .palette, .effects { margin: 0 0 18px; }
      .heading { font-size: .85rem; font-weight: 600; color: var(--secondary-text-color,#777);
                 text-transform: uppercase; letter-spacing: .03em; margin: 0 0 8px; }
      .swatches { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; }
      .swatch { position: relative; width: 46px; height: 46px; border-radius: 50%; margin: 24px 2px 2px;
                border: 2px solid rgba(0,0,0,.15); cursor: pointer; padding: 0;
                box-shadow: 0 1px 3px rgba(0,0,0,.25); transition: transform .08s ease; }
      .swatch:hover { transform: scale(1.08); }
      .swatch.active { outline: 3px solid var(--mwm-accent); outline-offset: 2px; }
      .swatch span { position: absolute; left: 50%; top: calc(100% + 2px); transform: translateX(-50%);
                     font-size: .55rem; color: var(--secondary-text-color,#777); white-space: nowrap;
                     text-shadow: none; }
      .swatch.on { background: linear-gradient(135deg,#ffe259,#ffa751); margin-top: 2px; }
      .swatch.on span, .swatch.off span { color: #333; }
      .swatch.off { background: #222; }
      .swatch.off span { color: #fff; }
      .swatch.off-ear { background: radial-gradient(circle at 50% 42%, #666, #171717 72%); }
      .swatch.off-ear span { color: #eee; }
      .note { font-size: .75rem; color: var(--secondary-text-color,#777); margin-top: 6px; }
      .effects-row { display: flex; align-items: center; gap: 8px; margin-bottom: 10px; }
      .effects select { flex: 1; }
      .effects button { margin-top: 4px; }
      .mode-row { display: flex; align-items: center; gap: 8px; margin: 0 0 12px; }
      .mode-label { font-size: .75rem; font-weight: 600; text-transform: uppercase;
                    letter-spacing: .03em; color: var(--secondary-text-color,#777); }
      .mode-toggle, .batch-transmit, .batch-clear { font-size: .8rem; cursor: pointer;
                    border: 1px solid var(--divider-color,rgba(0,0,0,.2));
                    background: var(--card-background-color,#fff);
                    color: var(--primary-text-color,#212121); border-radius: 4px;
                    padding: 3px 10px; }
      .mode-toggle:hover, .batch-transmit:hover, .batch-clear:hover { background: var(--ha-card-background,#f5f5f5); }
      .batch-transmit:disabled, .batch-clear:disabled { opacity: .5; cursor: default; }
      .batch-bar { display: flex; align-items: center; gap: 8px; margin: 0 0 12px; }
      .batch-summary { flex: 1; font-size: .75rem; color: var(--secondary-text-color,#777);
                    font-family: var(--paper-font-code_-_font-family,monospace); }
    `;
    root.appendChild(style);

    const card = document.createElement("div");
    card.className = "card";
    root.appendChild(card);

    const title = document.createElement("div");
    title.className = "title";
    title.textContent = this._config.title;
    card.appendChild(title);

    card.appendChild(this._buildModeRow());

    if (!this._hass) {
      const waiting = document.createElement("div");
      waiting.textContent = "Waiting for Home Assistant…";
      card.appendChild(waiting);
      return;
    }

    const bothState = this._stateOf("entity");
    if (!bothState) {
      const warn = document.createElement("div");
      warn.className = "note";
      warn.textContent = `Entity not found: ${this._config.entity}`;
      card.appendChild(warn);
      return;
    }

    card.appendChild(this._buildOnOff());
    card.appendChild(this._buildPaletteSection("left_entity", "Left Ear",
      "Left picks use the verified left-only form directly.",
      bothState,
      { enabled: !!this._sides.left }));
    card.appendChild(this._buildPaletteSection("entity", "Both Ears",
      "Sets both ears to the chosen shade (the protocol's native form).", bothState));
    card.appendChild(this._buildPaletteSection("right_entity", "Right Ear",
      "Right picks compose a fused both+restore frame so the left ear keeps its color.", bothState,
      { enabled: !!this._sides.right }));
    card.appendChild(this._buildEffects(bothState));
    if (this._batch) card.appendChild(this._buildBatchBar());
  }
}

function hslToRgb(h, s, l) {
  h = ((h % 360) + 360) % 360;
  s = Math.max(0, Math.min(1, s));
  l = Math.max(0, Math.min(1, l));
  const c = (1 - Math.abs(2 * l - 1)) * s;
  const x = c * (1 - Math.abs(((h / 60) % 2) - 1));
  const m = l - c / 2;
  let r = 0, g = 0, b = 0;
  if (h < 60) [r, g, b] = [c, x, 0];
  else if (h < 120) [r, g, b] = [x, c, 0];
  else if (h < 180) [r, g, b] = [0, c, x];
  else if (h < 240) [r, g, b] = [0, x, c];
  else if (h < 300) [r, g, b] = [x, 0, c];
  else [r, g, b] = [c, 0, x];
  return [
    Math.round((r + m) * 255),
    Math.round((g + m) * 255),
    Math.round((b + m) * 255),
  ];
}

/**
 * Minimal YAML-config editor so the card is configurable from the Lovelace
 * visual editor. Dispatches `config-changed`, the event Lovelace listens for.
 */
class MwmEarsCardEditor extends HTMLElement {
  setConfig(config) {
    this._config = { ...config };
    this._render();
  }

  set hass(hass) {
    this._hass = hass;
    this._renderEntities();
  }

  _dispatch() {
    this.dispatchEvent(new CustomEvent("config-changed", { detail: { config: this._config } }));
  }

  _field(key, label, placeholder) {
    const wrap = document.createElement("div");
    wrap.className = "field";
    const lab = document.createElement("label");
    lab.textContent = label;
    const input = document.createElement("input");
    input.value = this._config[key] || "";
    input.placeholder = placeholder || "";
    input.addEventListener("change", () => {
      const v = input.value.trim();
      if (v) this._config[key] = v;
      else delete this._config[key];
      this._dispatch();
    });
    wrap.appendChild(lab);
    wrap.appendChild(input);
    return wrap;
  }

  _renderEntities() {
    const el = this.shadowRoot && this.shadowRoot.querySelector("#entities");
    if (!el || !this._hass) return;
    const lights = Object.values(this._hass.states)
      .filter((s) => s.entity_id && s.entity_id.startsWith("light.") && s.attributes && s.attributes.color_palette);
    for (const input of el.querySelectorAll("input")) {
      const key = input.dataset.key;
      if (!key) continue;
      if (this._config[key]) continue; // keep user-chosen values
      const state = this._hass.states[this._config[key] || ""];
      if (state) continue;
      const list = input.list;
      if (!list) continue;
      list.replaceChildren();
      for (const ls of lights) {
        const o = document.createElement("option");
        o.value = ls.entity_id;
        o.textContent = ls.attributes.friendly_name || ls.entity_id;
        list.appendChild(o);
      }
      input.value = this._config[key] || "";
    }
  }

  _render() {
    if (!this.shadowRoot) this.attachShadow({ mode: "open" });
    const root = this.shadowRoot;
    root.innerHTML = "";

    const style = document.createElement("style");
    style.textContent = `
      :host { --mdc-theme-primary: var(--primary-color); display: block; }
      .field { display: flex; flex-direction: column; margin: 8px 0; }
      label { font-size: 0.8rem; color: var(--secondary-text-color,#777); margin-bottom: 4px; }
      input { width: 100%; }
      .hint { font-size: 0.75rem; color: var(--secondary-text-color,#777); margin-top: 10px; }
    `;
    root.appendChild(style);

    const title = document.createElement("div");
    title.textContent = "MWM Ears";
    title.style.fontWeight = "600";
    root.appendChild(title);

    const titleField = this._field("title", "Title", "MWM Ears");
    root.appendChild(titleField);

    const entities = document.createElement("div");
    entities.id = "entities";
    const fBoth = this._field("entity", "Both entity (required)", "light.ears");
    fBoth.querySelector("input").dataset.key = "entity";
    fBoth.querySelector("input").setAttribute("list", "mwm-lights");
    const fLeft = this._field("left_entity", "Left entity (optional)", "light.left_ear");
    fLeft.querySelector("input").dataset.key = "left_entity";
    fLeft.querySelector("input").setAttribute("list", "mwm-lights");
    const fRight = this._field("right_entity", "Right entity (optional)", "light.right_ear");
    fRight.querySelector("input").dataset.key = "right_entity";
    fRight.querySelector("input").setAttribute("list", "mwm-lights");
    entities.appendChild(fBoth);
    entities.appendChild(fLeft);
    entities.appendChild(fRight);
    const datalist = document.createElement("datalist");
    datalist.id = "mwm-lights";
    entities.appendChild(datalist);
    root.appendChild(entities);

    const hint = document.createElement("div");
    hint.className = "hint";
    hint.textContent = "The 'both' entity is required. Left/right ears are auto-detected from the Both entity's device; set them only to override.";
    root.appendChild(hint);

    this._renderEntities();
  }
}

if (!customElements.get(MWM_CARD_TAG)) {
  customElements.define(MWM_CARD_TAG, MwmEarsCard);
}
if (!customElements.get(MWM_EDITOR_TAG)) {
  customElements.define(MWM_EDITOR_TAG, MwmEarsCardEditor);
}

window.customCards = window.customCards || [];
window.customCards.push({
  type: MWM_CARD_TAG,
  name: "MWM Ears",
  description: "Control Disney Made-With-Magic ears: left/both/right color palettes and an effects picker.",
});
