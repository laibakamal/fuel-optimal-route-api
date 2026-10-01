/* Demonstration map for the fuel-route API.
 *
 * Calls the same public endpoint a reviewer calls from Postman, so this page can
 * never show something the documented API would not return. No framework and no
 * build step on purpose: it is a demonstration surface, not a frontend project.
 */
(function () {
  "use strict";

  var ENDPOINT = "/api/v1/route";

  var map = L.map("map", { scrollWheelZoom: true }).setView([39.5, -98.35], 4);
  L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", {
    maxZoom: 19,
    // Attribution is required by the OpenStreetMap tile usage policy.
    attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors'
  }).addTo(map);

  var layer = L.layerGroup().addTo(map);
  var form = document.getElementById("f");
  var out = document.getElementById("out");
  var button = document.getElementById("go");

  function money(n) {
    return "$" + Number(n).toLocaleString("en-US", {
      minimumFractionDigits: 2, maximumFractionDigits: 2
    });
  }
  function num(n, d) {
    return Number(n).toLocaleString("en-US", {
      minimumFractionDigits: d || 0, maximumFractionDigits: d || 0
    });
  }
  function el(tag, cls, text) {
    var node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text !== undefined) node.textContent = text;
    return node;
  }

  function showProblem(problem) {
    layer.clearLayers();
    out.innerHTML = "";
    var box = el("div", "err");
    box.appendChild(el("h3", null, problem.title || "Request failed"));
    box.appendChild(el("div", null, problem.detail || ""));
    if (problem.infeasibility) {
      var gap = problem.infeasibility;
      box.appendChild(el("pre", null,
        "gap: " + num(gap.gap_miles, 1) + " mi\n" +
        "range: " + num(gap.vehicle_range_miles, 0) + " mi\n" +
        "from: " + gap.from.label + " (mile " + num(gap.from.mile, 1) + ")\n" +
        "to:   " + gap.to.label + " (mile " + num(gap.to.mile, 1) + ")"));
    }
    if (problem.errors) {
      box.appendChild(el("pre", null, JSON.stringify(problem.errors, null, 1)));
    }
    out.appendChild(box);
  }

  function card(parent, label, value) {
    var c = el("div", "card");
    c.appendChild(el("b", null, value));
    c.appendChild(el("span", null, label));
    parent.appendChild(c);
  }

  function render(data) {
    layer.clearLayers();
    out.innerHTML = "";

    var t = data.totals;
    var d = data.diagnostics;

    var cards = el("div", "cards");
    card(cards, "Est. total fuel", money(t.estimated_total_fuel_cost_usd));
    card(cards, "Distance", num(data.route.total_distance_miles, 1) + " mi");
    card(cards, "Fuel stops", String(t.fuel_stop_count));
    card(cards, "Eff. $/gal", "$" + Number(t.effective_price_usd_per_gallon).toFixed(3));
    out.appendChild(cards);

    var meta = el("div", "note");
    meta.innerHTML =
      '<span class="pill">routing calls: ' + d.external_calls.routing_api + "</span> " +
      '<span class="pill">geocoding calls: ' + d.external_calls.geocoding_api + "</span> " +
      '<span class="pill">cache ' + (d.route_cache_hit ? "hit" : "miss") + "</span> " +
      '<span class="pill">local ' + num(d.timings_ms.local_compute_ms, 1) + " ms</span> " +
      '<span class="pill">corridor ' + d.corridor_stops_considered + " of " + d.stops_indexed + "</span>";
    out.appendChild(meta);

    // Be explicit when part of the trip's fuel was not priced. On sparse routes
    // this is a material share of the total and must not be invisible.
    if (t.unpriced_origin_miles > 1) {
      var warn = el("div", "note");
      warn.style.color = "var(--warn)";
      warn.textContent =
        "First usable stop is " + num(t.unpriced_origin_miles, 1) + " mi in, so " +
        num(t.unpriced_origin_gallons, 2) + " gal (" + money(t.origin_leg_estimated_cost_usd) +
        ") is estimated at the first pump's price rather than planned. " +
        "Cost at pumps alone: " + money(t.total_fuel_cost_usd) + ".";
      out.appendChild(warn);
    }

    out.appendChild(el("h2", null, "Fuel stops"));
    var table = el("table");
    var head = el("tr");
    ["#", "Stop", "Mile", "$/gal", "Gal", "Cost"].forEach(function (h, i) {
      var th = el("th", i > 1 ? "num" : null, h);
      head.appendChild(th);
    });
    table.appendChild(head);

    data.fuel_stops.forEach(function (stop) {
      var tr = el("tr");
      tr.appendChild(el("td", "num", String(stop.order)));
      var who = el("td");
      who.appendChild(el("div", null, stop.name));
      who.appendChild(el("div", "note",
        stop.city + ", " + stop.state + " · " + stop.address +
        " · detour " + Number(stop.detour_miles).toFixed(1) + " mi"));
      tr.appendChild(who);
      tr.appendChild(el("td", "num", num(stop.distance_along_route_miles, 0)));
      tr.appendChild(el("td", "num", Number(stop.price_usd_per_gallon).toFixed(3)));
      tr.appendChild(el("td", "num", Number(stop.gallons_purchased).toFixed(1)));
      tr.appendChild(el("td", "num", money(stop.cost_usd)));
      table.appendChild(tr);
      tr.addEventListener("mouseenter", function () {
        map.panTo([stop.latitude, stop.longitude]);
      });
    });
    out.appendChild(table);

    var details = el("details");
    details.appendChild(el("summary", null, "Assumptions and raw response"));
    var pre = el("pre", null, JSON.stringify(
      { assumptions: data.assumptions, diagnostics: d, totals: t }, null, 1));
    details.appendChild(pre);
    out.appendChild(details);

    // --- map -------------------------------------------------------------
    var bounds = null;
    if (data.route.geometry) {
      // GeoJSON is [lon, lat]; Leaflet wants [lat, lon].
      var line = data.route.geometry.coordinates.map(function (c) {
        return [c[1], c[0]];
      });
      var poly = L.polyline(line, { color: "#1b5e9c", weight: 4, opacity: 0.85 }).addTo(layer);
      bounds = poly.getBounds();
    }

    [["origin", data.origin, "#1a7f37"], ["destination", data.destination, "#9c2a1b"]]
      .forEach(function (pair) {
        L.circleMarker([pair[1].latitude, pair[1].longitude], {
          radius: 7, color: "#fff", weight: 2, fillColor: pair[2], fillOpacity: 1
        }).addTo(layer).bindPopup(
          "<b>" + pair[0] + "</b><br>" + (pair[1].resolved_label || pair[1].query) +
          "<br><small>" + pair[1].resolution_source + "</small>");
      });

    data.fuel_stops.forEach(function (stop) {
      L.marker([stop.latitude, stop.longitude], {
        icon: L.divIcon({
          className: "", iconSize: [22, 22], iconAnchor: [11, 11],
          html: '<div class="marker-pin">' + stop.order + "</div>"
        })
      }).addTo(layer).bindPopup(
        "<b>" + stop.name + "</b><br>" + stop.city + ", " + stop.state +
        "<br>" + stop.address +
        "<br>$" + Number(stop.price_usd_per_gallon).toFixed(3) + "/gal" +
        "<br>" + Number(stop.gallons_purchased).toFixed(1) + " gal = " + money(stop.cost_usd) +
        "<br><small>mile " + num(stop.distance_along_route_miles, 0) +
        ", detour " + Number(stop.detour_miles).toFixed(1) + " mi</small>");
    });

    if (bounds) map.fitBounds(bounds, { padding: [28, 28] });
  }

  form.addEventListener("submit", function (event) {
    event.preventDefault();
    button.disabled = true;
    button.textContent = "Planning…";
    var params = new URLSearchParams(new FormData(form)).toString();
    fetch(ENDPOINT + "?" + params, { headers: { Accept: "application/json" } })
      .then(function (response) {
        return response.json().then(function (body) {
          return { ok: response.ok, body: body };
        });
      })
      .then(function (result) {
        if (result.ok) render(result.body);
        else showProblem(result.body);
      })
      .catch(function (error) {
        showProblem({ title: "Network error", detail: String(error) });
      })
      .finally(function () {
        button.disabled = false;
        button.textContent = "Plan route";
      });
  });

  form.dispatchEvent(new Event("submit"));
})();
