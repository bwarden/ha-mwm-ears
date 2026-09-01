/**
 * mwm-ears-card: Lovelace custom card for the `mwm_ears` Home Assistant
 * integration.
 *
 * Shows the three ear-colour palettes (Left / Both / Right) with every
 * representable colour, plus an effects picker, and drives the integration's
 * `light.*` entities:
 *
 *   - a palette swatch pick calls `light.turn_on` with that RGB on the
 *     matching side entity, so the integration's colour snapping selects the
 *     nearest verified ear shade and emits the correct IR frame (simple
 *     right-only, composed both+restore for left, canonical both for both);
 *   - the effects picker calls `light.turn_on` (effect = name) on the Both
 *     entity -- effect programs are room-wide, not per-ear.
 *
 * The swatches are read from the entity's `color_palette` attribute
 * (rendered by python/custom_components/mwm_ears/_mwm/palette.py::
 * color_palette), so the card always shows exactly what the integration can
 * represent and needs no palette copy of its own.
 *
 * Usage:
 *   - Register this file as a Lovelace resource (see python/README.md).
 *   - Configure with the "ears" (Both) light entity as the minimum:
 *     {
 *       "type": "custom:mwm-ears-card",
 *       "entity": "light.ears",
 *       "left_entity": "light.left_ear",
 *       "right_entity": "light.right_ear"
 *     }
 *   left_entity / right_entity are optional; without them that side's palette
 *   is displayed using the Both entity's palette but disabled.
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
    this._reconcile();
  }

  set hass(hass) {
    this._hass = hass;
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
    const ids = [this._config && this._config.entity,
                 this._config && this._config.left_entity,
                 this._config && this._config.right_entity].filter(Boolean);
    return ids.map((id) => {
      const s = hass && hass.states && hass.states[id];
      return s ? `${s.state}:${(s.attributes || {}).rgb_color}:${(s.attributes || {}).effect}` : "none";
    }).join("|");
  }

  _stateOf(key) {
    const id = this._config && this._config[key];
    return id && this._hass ? this._hass.states[id] : undefined;
  }

  _call(entityId, service, data) {
    if (!entityId || !this._hass) return;
    this._hass.callService("light", service, { entity_id: entityId, ...data });
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

  _swatchMatch(rgb, current) {
    if (!current) return false;
    return (
      Math.max(Math.abs(rgb[0] - current[0]),
               Math.abs(rgb[1] - current[1]),
               Math.abs(rgb[2] - current[2])) <= 8
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
      n.textContent = `Configure "${sideKey}" to control this ear independently.`;
      section.appendChild(n);
      return section;
    }

    const state = this._stateOf(sideKey);
    const catalog = this._palette(state, bothState);
    const current = this._rgbOf(state);
    const entityId = this._config[sideKey];

    const grid = document.createElement("div");
    grid.className = "swatches";

    for (const c of catalog) {
      const rgb = c.rgb || [0, 0, 0];
      const btn = document.createElement("button");
      btn.className = "swatch" + (this._swatchMatch(rgb, current) ? " active" : "");
      btn.style.background = `rgb(${rgb.join(",")})`;
      btn.title = c.name;
      const label = document.createElement("span");
      label.textContent = c.name;
      btn.appendChild(label);
      btn.addEventListener("click", () => {
        this._call(entityId, "turn_on", { rgb_color: rgb });
      });
      grid.appendChild(btn);
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

    make("On", "on", () => this._call(this._config.entity, "turn_on", {}));
    make("Off", "off", () => this._call(this._config.entity, "turn_off", {}));

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
        this._call(this._config.entity, "turn_on", { effect: select.value });
        select.value = "";
      }
    });

    row.appendChild(label);
    row.appendChild(select);
    section.appendChild(row);

    // "Restore colour" clears a running effect and re-issues the remembered
    // colour (a bare turn_on restores it); it does NOT turn the ears off.
    const restore = document.createElement("button");
    restore.textContent = "Restore colour";
    restore.addEventListener("click", () => {
      this._call(this._config.entity, "turn_on", {});
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

  _reconcile() {
    if (!this.shadowRoot) return;
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
      .note { font-size: .75rem; color: var(--secondary-text-color,#777); margin-top: 6px; }
      .effects-row { display: flex; align-items: center; gap: 8px; margin-bottom: 10px; }
      .effects select { flex: 1; }
      .effects button { margin-top: 4px; }
    `;
    root.appendChild(style);

    const card = document.createElement("div");
    card.className = "card";
    root.appendChild(card);

    const title = document.createElement("div");
    title.className = "title";
    title.textContent = this._config.title;
    card.appendChild(title);

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
      "Left picks compose a fused both+restore frame so the right ear keeps its colour.",
      bothState,
      { enabled: !!this._config.left_entity }));
    card.appendChild(this._buildPaletteSection("entity", "Both Ears",
      "Sets both ears to the chosen shade (the protocol's native form).", bothState));
    card.appendChild(this._buildPaletteSection("right_entity", "Right Ear",
      "Right picks use the verified right-only form directly.", bothState,
      { enabled: !!this._config.right_entity }));
    card.appendChild(this._buildEffects(bothState));
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
      const options = input.list && input.list.options;
      if (options) options.length = 0;
      if (input.list && options) {
        const add = (value, text) => {
          const o = document.createElement("option");
          o.value = value;
          o.textContent = text;
          options.add(o);
        };
        for (const ls of lights) {
          add(ls.entity_id, ls.attributes.friendly_name || ls.entity_id);
        }
        input.value = this._config[key] || "";
      }
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
    hint.textContent = "The 'both' entity is required. Left/right entities are optional; a palette without its entity is shown read-only.";
    root.appendChild(hint);

    this._renderEntities();
  }
}

customElements.define(MWM_CARD_TAG, MwmEarsCard);
customElements.define(MWM_EDITOR_TAG, MwmEarsCardEditor);

window.customCards = window.customCards || [];
window.customCards.push({
  type: MWM_CARD_TAG,
  name: "MWM Ears",
  description: "Control Disney Made-With-Magic ears: left/both/right colour palettes and an effects picker.",
});
