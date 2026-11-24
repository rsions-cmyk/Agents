// Bedieningspagina voor de thermostaat. ESPHome serveert dit bestand als /0.js
// en praat met de pagina via de eigen webserver: /events voor de standen,
// POST-verzoeken om iets te wijzigen.
//
// Geschreven voor Safari vanaf iOS 12: geen optional chaining, geen ??, geen flex-gap.

(function () {
  "use strict";

  var UIT_NA_BEWEGING_MS = 30 * 1000;  // scherm zwart 30 seconden nadat de bewegingssensor niets meer ziet
  var UIT_NA_AANRAKING_MS = 60 * 1000; // zonder bewegingssensor: zwart een minuut na de laatste aanraking
  var VERZEND_NA_MS = 700;    // wachttijd na de laatste tik op plus of min

  // ---------------------------------------------------------------------------
  // Opmaak
  // ---------------------------------------------------------------------------

  var css = [
    ":root{--bg:#101418;--paneel:#1b2128;--rand:#2a323b;--tekst:#eef2f5;--zacht:#93a1ad;",
    "--warm:#ff8a3d;--koel:#5fb0ff;--fout:#ff5d5d;--let:#ffc04d;--knop:#252d36}",
    "*{box-sizing:border-box;-webkit-tap-highlight-color:transparent}",
    "html,body{margin:0;height:100%;background:var(--bg);color:var(--tekst);",
    "font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;",
    "-webkit-user-select:none;user-select:none;overflow:hidden}",
    "esp-app{display:none}",
    "#app{display:flex;flex-direction:column;height:100%;padding:12px 16px;",
    "padding-top:calc(12px + env(safe-area-inset-top));padding-bottom:calc(12px + env(safe-area-inset-bottom))}",
    ".balk{display:flex;justify-content:space-between;align-items:center;color:var(--zacht);font-size:15px}",
    ".verb{display:flex;align-items:center}",
    ".stip{width:8px;height:8px;border-radius:50%;background:var(--zacht);margin-right:6px}",
    ".verb.ok .stip{background:#4cd17a}.verb.weg .stip{background:var(--fout)}",
    ".midden{flex:1;display:flex;flex-direction:column;justify-content:center;align-items:center;text-align:center}",
    ".nu{font-size:112px;font-weight:200;line-height:1;letter-spacing:-3px;font-variant-numeric:tabular-nums}",
    ".nu small{font-size:48px;vertical-align:top;margin-left:2px;letter-spacing:0}",
    ".label{color:var(--zacht);font-size:15px;margin-top:6px}",
    ".status{margin-top:18px;font-size:17px;display:flex;align-items:center;min-height:24px}",
    ".vlam{width:12px;height:12px;border-radius:50%;margin-right:8px;background:var(--zacht)}",
    ".status.brandt .vlam{background:var(--warm);box-shadow:0 0 12px var(--warm)}",
    ".doel{display:flex;align-items:center;justify-content:center;margin-top:26px}",
    ".rond{width:72px;height:72px;border-radius:50%;border:1px solid var(--rand);background:var(--knop);",
    "color:var(--tekst);font-size:34px;line-height:1;padding:0;display:flex;align-items:center;justify-content:center}",
    ".rond:active{background:var(--rand)}",
    ".doelwaarde{width:130px;font-size:40px;font-weight:300;font-variant-numeric:tabular-nums}",
    ".doelwaarde.uit{color:var(--zacht);font-size:28px}",
    ".meldingen{width:100%}",
    ".melding{border-radius:12px;padding:10px 14px;margin-top:8px;font-size:15px;text-align:left}",
    ".melding.fout{background:rgba(255,93,93,.15);color:#ffb3b3;border:1px solid rgba(255,93,93,.4)}",
    ".melding.let{background:rgba(255,192,77,.12);color:#ffe0a3;border:1px solid rgba(255,192,77,.35)}",
    ".info{display:flex;justify-content:space-around;color:var(--zacht);font-size:14px;margin:10px 0 12px}",
    ".info div{text-align:center}.info b{display:block;color:var(--tekst);font-size:17px;font-weight:500}",
    ".knoppen{display:flex}",
    ".knoppen button{flex:1;height:52px;border-radius:14px;border:1px solid var(--rand);background:var(--knop);",
    "color:var(--tekst);font-size:16px;margin-left:8px}",
    ".knoppen button:first-child{margin-left:0}",
    ".knoppen button.aan{border-color:var(--warm);color:var(--warm)}",
    // Instellingen
    "#blad{position:fixed;left:0;right:0;bottom:0;top:0;background:rgba(0,0,0,.55);display:none}",
    "#blad.open{display:block}",
    ".vel{position:absolute;left:0;right:0;bottom:0;background:var(--paneel);border-radius:18px 18px 0 0;",
    "padding:18px 16px;padding-bottom:calc(18px + env(safe-area-inset-bottom));max-height:92%;overflow-y:auto;",
    "-webkit-overflow-scrolling:touch}",
    ".vel h2{margin:0 0 4px;font-size:20px;font-weight:600}",
    ".vel p{margin:0 0 14px;color:var(--zacht);font-size:14px;line-height:1.4}",
    ".rij{display:flex;justify-content:space-between;align-items:center;padding:10px 0;border-top:1px solid var(--rand)}",
    ".rij span{font-size:16px}",
    ".stap{display:flex;align-items:center}",
    ".stap button{width:44px;height:44px;border-radius:50%;border:1px solid var(--rand);background:var(--knop);",
    "color:var(--tekst);font-size:24px;padding:0}",
    ".stap output{width:76px;text-align:center;font-size:18px;font-variant-numeric:tabular-nums}",
    ".rij input[type=time]{background:var(--knop);color:var(--tekst);border:1px solid var(--rand);border-radius:10px;",
    "font-size:17px;padding:8px 10px;min-width:110px;-webkit-appearance:none;font-family:inherit}",
    ".kop{margin-top:14px;color:var(--zacht);font-size:13px;text-transform:uppercase;letter-spacing:.5px}",
    ".sluit{width:100%;height:50px;margin-top:16px;border-radius:14px;border:0;background:var(--warm);",
    "color:#1b1b1b;font-size:17px;font-weight:600}",
    // Rustscherm: helemaal zwart. Op een OLED-scherm staan de pixels dan uit.
    "#rust{position:fixed;left:0;top:0;right:0;bottom:0;background:#000;display:none}",
    "#rust.aan{display:block}",
    "@media (orientation:landscape) and (max-height:500px){",
    ".nu{font-size:84px}.doel{margin-top:12px}.status{margin-top:10px}.info{margin:6px 0}}"
  ].join("");

  var html = [
    '<div id="app">',
    '  <div class="balk"><div class="verb" id="verb"><div class="stip"></div><span id="verbtekst">Verbinden...</span></div>',
    '  <div id="klok"></div></div>',
    '  <div class="midden">',
    '    <div class="nu" id="nu">--</div>',
    '    <div class="label">in de kamer</div>',
    '    <div class="status" id="status"><div class="vlam"></div><span id="statustekst"></span></div>',
    '    <div class="doel">',
    '      <button class="rond" id="min" aria-label="Kouder">&minus;</button>',
    '      <div class="doelwaarde" id="doel">--</div>',
    '      <button class="rond" id="plus" aria-label="Warmer">+</button>',
    '    </div>',
    '    <div class="label" id="doellabel">ingesteld</div>',
    '    <div class="meldingen" id="meldingen"></div>',
    '  </div>',
    '  <div class="info">',
    '    <div><b id="aanvoer">--</b>aanvoer</div>',
    '    <div><b id="druk">--</b>waterdruk</div>',
    '    <div><b id="vocht">--</b>vocht</div>',
    '  </div>',
    '  <div class="knoppen">',
    '    <button id="aanuit">Cv</button>',
    '    <button id="schemaknop">Schema</button>',
    '    <button id="instellen">Instellen</button>',
    '  </div>',
    '</div>',
    '<div id="blad"><div class="vel">',
    '  <h2>Weekschema</h2>',
    '  <p>Tussen ochtend en avond geldt de comforttemperatuur, daarbuiten de ecotemperatuur. ',
    '  Zet je de temperatuur met de hand, dan blijft die staan tot het volgende schakelmoment.</p>',
    '  <div class="kop">Temperaturen</div>',
    '  <div class="rij"><span>Comfort</span><div class="stap"><button data-stap="Comfort" data-d="-0.5">&minus;</button>',
    '    <output id="comfort">--</output><button data-stap="Comfort" data-d="0.5">+</button></div></div>',
    '  <div class="rij"><span>Eco</span><div class="stap"><button data-stap="Eco" data-d="-0.5">&minus;</button>',
    '    <output id="eco">--</output><button data-stap="Eco" data-d="0.5">+</button></div></div>',
    '  <div class="kop">Maandag tot en met vrijdag</div>',
    '  <div class="rij"><span>Comfort vanaf</span><input type="time" data-tijd="Ochtend"></div>',
    '  <div class="rij"><span>Eco vanaf</span><input type="time" data-tijd="Avond"></div>',
    '  <div class="kop">Zaterdag en zondag</div>',
    '  <div class="rij"><span>Comfort vanaf</span><input type="time" data-tijd="Weekendochtend"></div>',
    '  <div class="rij"><span>Eco vanaf</span><input type="time" data-tijd="Weekendavond"></div>',
    '  <button class="sluit" id="sluit">Klaar</button>',
    '</div></div>',
    '<div id="rust"></div>'
  ].join("\n");

  // ---------------------------------------------------------------------------
  // Pagina opbouwen
  // ---------------------------------------------------------------------------

  function voegMetaToe(naam, inhoud) {
    var m = document.createElement("meta");
    m.setAttribute("name", naam);
    m.setAttribute("content", inhoud);
    document.head.appendChild(m);
  }
  voegMetaToe("viewport", "width=device-width,initial-scale=1,maximum-scale=1,user-scalable=no,viewport-fit=cover");
  voegMetaToe("apple-mobile-web-app-capable", "yes");
  voegMetaToe("mobile-web-app-capable", "yes");
  voegMetaToe("apple-mobile-web-app-status-bar-style", "black-translucent");
  voegMetaToe("apple-mobile-web-app-title", "Thermostaat");
  voegMetaToe("theme-color", "#101418");
  document.title = "Thermostaat";
  document.documentElement.setAttribute("lang", "nl");

  var stijl = document.createElement("style");
  stijl.textContent = css;
  document.head.appendChild(stijl);

  var houder = document.createElement("div");
  houder.innerHTML = html;
  while (houder.firstChild) document.body.appendChild(houder.firstChild);

  function $(id) { return document.getElementById(id); }

  // ---------------------------------------------------------------------------
  // Toestand
  // ---------------------------------------------------------------------------

  var st = {};            // laatste waarde per entiteit, bijvoorbeeld st["sensor/Aanvoer"]
  var lokaalDoel = null;  // doeltemperatuur die de gebruiker net heeft ingetikt
  var lokaalTot = 0;      // tot dit tijdstip negeren we binnenkomende doelwaarden
  var verzendTimer = null;

  function getal(v) {
    if (v === null || v === undefined || v === "NA" || v === "NaN") return NaN;
    var n = typeof v === "number" ? v : parseFloat(v);
    return isNaN(n) ? NaN : n;
  }
  function komma(n, dec) {
    return n.toFixed(dec).replace(".", ",");
  }
  function waarde(id) {
    var e = st[id];
    return e ? e.value : undefined;
  }

  // ---------------------------------------------------------------------------
  // Tekenen
  // ---------------------------------------------------------------------------

  function teken() {
    var c = st["climate/Thermostaat"] || {};
    var aan = c.mode === "HEAT";
    var nu = getal(c.current_temperature);
    if (isNaN(nu)) nu = getal(waarde("sensor/Kamertemperatuur"));
    var doel = Date.now() < lokaalTot && lokaalDoel !== null ? lokaalDoel : getal(c.target_temperature);

    $("nu").innerHTML = isNaN(nu) ? "--" : komma(nu, 1) + "<small>°</small>";

    var doelEl = $("doel");
    if (!aan) {
      doelEl.textContent = "uit";
      doelEl.className = "doelwaarde uit";
      $("doellabel").textContent = "verwarming staat uit";
    } else {
      doelEl.textContent = isNaN(doel) ? "--" : komma(doel, 1) + "°";
      doelEl.className = "doelwaarde";
      $("doellabel").textContent = "ingesteld";
    }

    var brandt = waarde("binary_sensor/Brander") === true;
    var warmwater = waarde("binary_sensor/Warmwater") === true;
    var statusTekst;
    if (brandt && warmwater) statusTekst = "Ketel maakt warm water";
    else if (brandt) statusTekst = "Ketel verwarmt";
    else if (aan && c.action === "HEATING") statusTekst = "Warmte gevraagd";
    else if (aan) statusTekst = "Op temperatuur";
    else statusTekst = "Verwarming uit";
    $("statustekst").textContent = statusTekst;
    $("status").className = brandt ? "status brandt" : "status";

    var aanvoer = getal(waarde("sensor/Aanvoer"));
    var druk = getal(waarde("sensor/Waterdruk"));
    var vocht = getal(waarde("sensor/Luchtvochtigheid"));
    $("aanvoer").textContent = isNaN(aanvoer) ? "--" : Math.round(aanvoer) + " °C";
    $("druk").textContent = isNaN(druk) ? "--" : komma(druk, 1) + " bar";
    $("vocht").textContent = isNaN(vocht) ? "--" : Math.round(vocht) + "%";

    var m = [];
    if (waarde("binary_sensor/Sensorstoring") === true) {
      m.push(["fout", "Kamersensor geeft geen waarde. De ketel draait in de noodstand op een vaste, lage temperatuur."]);
    }
    if (waarde("binary_sensor/Storing") === true) {
      m.push(["fout", "De ketel meldt een storing. Kijk op het display van de ketel."]);
    }
    if (!isNaN(druk) && druk > 0 && druk < 1.0) {
      m.push(["let", "Waterdruk is laag (" + komma(druk, 1) + " bar). Vul de installatie bij tot 1,5 à 2 bar."]);
    }
    var mh = "";
    for (var i = 0; i < m.length; i++) mh += '<div class="melding ' + m[i][0] + '">' + m[i][1] + "</div>";
    $("meldingen").innerHTML = mh;

    $("aanuit").className = aan ? "aan" : "";
    $("aanuit").textContent = aan ? "Cv aan" : "Cv uit";
    var schema = waarde("switch/Schema") === true;
    $("schemaknop").className = schema ? "aan" : "";
    $("schemaknop").textContent = schema ? "Schema aan" : "Schema uit";
    $("min").disabled = !aan;
    $("plus").disabled = !aan;
    $("min").style.opacity = aan ? "1" : ".35";
    $("plus").style.opacity = aan ? "1" : ".35";

    var comfort = getal(waarde("number/Comfort"));
    var eco = getal(waarde("number/Eco"));
    $("comfort").textContent = isNaN(comfort) ? "--" : komma(comfort, 1) + "°";
    $("eco").textContent = isNaN(eco) ? "--" : komma(eco, 1) + "°";
    var tijden = document.querySelectorAll("input[data-tijd]");
    for (var j = 0; j < tijden.length; j++) {
      var inp = tijden[j];
      if (document.activeElement === inp) continue;
      var t = waarde("time/" + inp.getAttribute("data-tijd"));
      if (typeof t === "string" && t.length >= 5) inp.value = t.substr(0, 5);
    }
  }

  function klok() {
    var d = new Date();
    var u = d.getHours(), mi = d.getMinutes();
    $("klok").textContent = (u < 10 ? "0" : "") + u + ":" + (mi < 10 ? "0" : "") + mi;
  }

  // ---------------------------------------------------------------------------
  // Verbinding met de ESP32
  // ---------------------------------------------------------------------------

  function zetVerbinding(ok, tekst) {
    $("verb").className = "verb " + (ok ? "ok" : "weg");
    $("verbtekst").textContent = tekst;
  }

  function stuur(pad) {
    return fetch(pad, { method: "POST" }).then(function (r) {
      if (!r.ok) throw new Error("HTTP " + r.status);
    }).catch(function () {
      zetVerbinding(false, "Wijziging niet aangekomen");
    });
  }

  function verbind() {
    var bron = new EventSource("/events");
    bron.addEventListener("open", function () { zetVerbinding(true, "Verbonden"); });
    bron.addEventListener("error", function () {
      if (bron.readyState !== 1) zetVerbinding(false, "Geen verbinding");
    });
    bron.addEventListener("state", function (e) {
      var d;
      try { d = JSON.parse(e.data); } catch (fout) { return; }
      if (!d || !d.id) return;
      var oud = st[d.id] || {};
      for (var k in d) if (Object.prototype.hasOwnProperty.call(d, k)) oud[k] = d[k];
      st[d.id] = oud;
      if (d.id === "binary_sensor/Beweging" && d.value === true) wordWakker();
      teken();
    });
  }

  // ---------------------------------------------------------------------------
  // Bediening
  // ---------------------------------------------------------------------------

  function verander(delta) {
    var c = st["climate/Thermostaat"] || {};
    if (c.mode !== "HEAT") return;
    var basis = Date.now() < lokaalTot && lokaalDoel !== null ? lokaalDoel : getal(c.target_temperature);
    if (isNaN(basis)) return;
    var min = getal(c.min_temp), max = getal(c.max_temp);
    if (isNaN(min)) min = 10;
    if (isNaN(max)) max = 25;
    lokaalDoel = Math.min(max, Math.max(min, Math.round((basis + delta) * 2) / 2));
    lokaalTot = Date.now() + 4000;
    teken();
    clearTimeout(verzendTimer);
    verzendTimer = setTimeout(function () {
      lokaalTot = Date.now() + 3000;
      stuur("/climate/Thermostaat/set?target_temperature=" + lokaalDoel);
    }, VERZEND_NA_MS);
  }

  function klik(id, f) { $(id).addEventListener("click", f); }

  klik("min", function () { verander(-0.5); });
  klik("plus", function () { verander(0.5); });
  klik("aanuit", function () {
    var c = st["climate/Thermostaat"] || {};
    var nieuw = c.mode === "HEAT" ? "OFF" : "HEAT";
    c.mode = nieuw;
    st["climate/Thermostaat"] = c;
    teken();
    stuur("/climate/Thermostaat/set?mode=" + nieuw);
  });
  klik("schemaknop", function () {
    var aan = waarde("switch/Schema") === true;
    st["switch/Schema"] = { value: !aan };
    teken();
    stuur("/switch/Schema/" + (aan ? "turn_off" : "turn_on"));
  });
  klik("instellen", function () { $("blad").className = "open"; });
  klik("sluit", function () { $("blad").className = ""; });
  $("blad").addEventListener("click", function (e) {
    if (e.target === $("blad")) $("blad").className = "";
  });

  var stapknoppen = document.querySelectorAll("button[data-stap]");
  for (var s = 0; s < stapknoppen.length; s++) {
    stapknoppen[s].addEventListener("click", function () {
      var naam = this.getAttribute("data-stap");
      var d = parseFloat(this.getAttribute("data-d"));
      var huidig = getal(waarde("number/" + naam));
      if (isNaN(huidig)) return;
      var nieuw = Math.min(25, Math.max(10, huidig + d));
      st["number/" + naam] = { value: String(nieuw) };
      teken();
      stuur("/number/" + naam + "/set?value=" + nieuw);
    });
  }

  var tijdvelden = document.querySelectorAll("input[data-tijd]");
  for (var t = 0; t < tijdvelden.length; t++) {
    tijdvelden[t].addEventListener("change", function () {
      if (!/^\d\d:\d\d/.test(this.value)) return;
      var naam = this.getAttribute("data-tijd");
      var v = this.value.substr(0, 5) + ":00";
      st["time/" + naam] = { value: v };
      stuur("/time/" + naam + "/set?value=" + encodeURIComponent(v));
    });
  }

  // ---------------------------------------------------------------------------
  // Scherm uit als er niemand is
  // Zonder beweging en aanraking wordt de pagina helemaal zwart. Een bewegingssensor
  // op de ESP32 (Beweging) zet hem weer aan. Zonder sensor werkt een tik op het scherm.
  // ---------------------------------------------------------------------------

  var laatsteActiviteit = Date.now();

  function rustAan() { return $("rust").className === "aan"; }

  function wordWakker() {
    laatsteActiviteit = Date.now();
    if (rustAan()) $("rust").className = "";
  }

  function getikt(e) {
    if (rustAan()) {
      // De eerste tik maakt alleen wakker en bedient niets.
      if (e && e.cancelable) e.preventDefault();
      if (e) e.stopPropagation();
    }
    wordWakker();
  }
  $("rust").addEventListener("touchstart", getikt);
  $("rust").addEventListener("click", getikt);
  document.addEventListener("touchstart", function () { laatsteActiviteit = Date.now(); }, true);
  document.addEventListener("click", function () { laatsteActiviteit = Date.now(); }, true);

  setInterval(function () {
    var nu = Date.now();
    if (waarde("binary_sensor/Beweging") === true) laatsteActiviteit = nu;
    var heeftSensor = st["binary_sensor/Beweging"] !== undefined;
    var wacht = heeftSensor ? UIT_NA_BEWEGING_MS : UIT_NA_AANRAKING_MS;
    var bladOpen = $("blad").className === "open";
    if (!bladOpen && !rustAan() && nu - laatsteActiviteit > wacht) $("rust").className = "aan";
  }, 1000);

  // ---------------------------------------------------------------------------

  klok();
  setInterval(klok, 10000);
  teken();
  verbind();
})();
