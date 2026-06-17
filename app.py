from flask import Flask, render_template, request, jsonify
import requests, os, time, threading, hashlib, json
from concurrent.futures import ThreadPoolExecutor

app = Flask(__name__)

# ── Clés API ────────────────────────────────────────────────────
RAPIDAPI_KEY  = os.environ.get("RAPIDAPI_KEY",  "86d656bf45msh5e9078844da826cp1f1591jsn6c8215f506fd")
RAPIDAPI_HOST = "skyscanner-flights-travel-api.p.rapidapi.com"
RAPIDAPI_HDR  = {"x-rapidapi-key": RAPIDAPI_KEY, "x-rapidapi-host": RAPIDAPI_HOST}

SERPAPI_KEY   = os.environ.get("SERPAPI_KEY", "2527058a645a93bb7e70a52e6bd277a3292223ee486611e8bcd2a9954560a386")

executor = ThreadPoolExecutor(max_workers=20)

# ── Cache thread-safe TTL 10 min ────────────────────────────────
_cache      = {}
_cache_lock = threading.Lock()
CACHE_TTL   = 600

def cache_get(key):
    with _cache_lock:
        entry = _cache.get(key)
        if entry and (time.time() - entry["ts"]) < CACHE_TTL:
            return entry["data"]
    return None

def cache_set(key, data):
    with _cache_lock:
        _cache[key] = {"data": data, "ts": time.time()}
        if len(_cache) > 500:
            now = time.time()
            for k in [k for k, v in _cache.items() if (now - v["ts"]) > CACHE_TTL]:
                del _cache[k]

def make_key(*args):
    return hashlib.md5(json.dumps(args, sort_keys=True).encode()).hexdigest()

# ── Liens directs compagnies ────────────────────────────────────
AIRLINE_URLS = {
    "air france":         "https://wwws.airfrance.fr/search/offers",
    "royal air maroc":    "https://www.royalairmaroc.com/fr-fr/reservation/recherche-vol",
    "ethiopian airlines": "https://www.ethiopianairlines.com/et/booking/flight-booking",
    "turkish airlines":   "https://www.turkishairlines.com/fr-fr/flights/",
    "klm":                "https://www.klm.com/fr/fr",
    "brussels airlines":  "https://www.brusselsairlines.com/fr/fr/",
    "transavia":          "https://www.transavia.com/fr-FR/accueil/",
    "lufthansa":          "https://www.lufthansa.com/fr/fr/homepage",
    "kenya airways":      "https://www.kenya-airways.com/fr/",
    "tap air portugal":   "https://www.flytap.com/fr-fr/",
    "corsair":            "https://www.corsair.fr/",
    "asky":               "https://flyasky.com/",
    "air senegal":        "https://www.airsenegal.com/",
    "egyptair":           "https://www.egyptair.com/fr/",
    "emirates":           "https://www.emirates.com/fr/french/",
    "qatar airways":      "https://www.qatarairways.com/fr-fr/",
    "swiss":              "https://www.swiss.com/fr/fr/",
    "iberia":             "https://www.iberia.com/fr/",
}

def get_airline_url(carrier_name, legs, adults=1, children=0):
    key = carrier_name.lower()
    if "air france" in key:
        segments = ",".join(
            f"{l['origin']}:{l['destination']}:{l['departure'][:10]}"
            for l in legs
        )
        pax = f"ADT:{adults}"
        if children:
            pax += f"_CHD:{children}"
        return f"https://wwws.airfrance.fr/search/offers?pax={pax}&cabin=ECONOMY&segments={segments}"
    for airline_key, url in AIRLINE_URLS.items():
        if airline_key in key:
            return url
    return "https://www.google.com/travel/flights?hl=fr&curr=EUR"

# ── Parseur résultats SerpAPI Google Flights ────────────────────
def parse_serpapi_results(data, orig_iata, dest_iata, ret_date=None):
    """
    Transforme la réponse SerpAPI en liste de résultats normalisés.
    Combine best_flights + other_flights, gère aller-retour.
    """
    all_items = data.get("best_flights", []) + data.get("other_flights", [])
    results = []

    for it in all_items:
        prix = it.get("price", 0)
        if not prix:
            continue

        segments = it.get("flights", [])
        if not segments:
            continue

        # Séparer segments aller / retour
        # L'aller part de orig_iata, le retour repart de dest_iata
        outbound = []
        inbound  = []
        in_return = False
        for seg in segments:
            dep_id = seg.get("departure_airport", {}).get("id", "")
            if not in_return and dep_id == dest_iata:
                in_return = True
            if in_return:
                inbound.append(seg)
            else:
                outbound.append(seg)

        legs_data = []
        for group, label in [(outbound, "out"), (inbound, "in")]:
            if not group:
                continue
            dep_info = group[0].get("departure_airport", {})
            arr_info = group[-1].get("arrival_airport", {})
            carriers = list(dict.fromkeys(s.get("airline","") for s in group))
            dur_min  = sum(s.get("duration", 0) for s in group)
            # Ajouter temps d'escale si plusieurs segments
            if len(group) > 1:
                dur_min = it.get("total_duration", dur_min)

            legs_data.append({
                "origin":      dep_info.get("id", ""),
                "destination": arr_info.get("id", ""),
                "departure":   dep_info.get("time", "")[:16],
                "arrival":     arr_info.get("time", "")[:16],
                "duration":    f"{dur_min//60}h{dur_min%60:02d}",
                "stops":       len(group) - 1,
                "carriers":    carriers,
            })

        if not legs_data:
            continue

        primary_carrier = segments[0].get("airline", "")
        o  = legs_data[0]["origin"]
        de = legs_data[0]["destination"]

        results.append({
            "prix":            prix,
            "legs":            legs_data,
            "primary_carrier": primary_carrier,
            "airline_url":     None,  # rempli après avec adults/children
            "google_url":      f"https://www.google.com/travel/flights?hl=fr&curr=EUR&q=vols+{o}+{de}",
        })

    return results

# ── Routes ──────────────────────────────────────────────────────
@app.route("/")
def index():
    return render_template("index.html")

@app.route("/api/airport")
def airport():
    q = request.args.get("q", "").strip()
    if not q:
        return jsonify([])
    ck = make_key("airport", q)
    cached = cache_get(ck)
    if cached is not None:
        return jsonify(cached)
    try:
        r = requests.get(
            f"https://{RAPIDAPI_HOST}/flights/searchAirport",
            headers=RAPIDAPI_HDR,
            params={"market": "FR", "query": q, "locale": "fr-FR"},
            timeout=15
        )
        places = r.json().get("places", [])
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    cache_set(ck, places)
    return jsonify(places)

@app.route("/api/search", methods=["POST"])
def search():
    d = request.get_json(force=True)

    adults   = d.get("adults", 1)
    children = d.get("children", 0)
    orig     = d["orig_sky"]   # IATA
    dest     = d["dest_sky"]   # IATA
    dep      = d["dep"]
    ret      = d.get("ret")

    cabin_map    = {"economy": 1, "premium_economy": 2, "business": 3, "first": 4}
    travel_class = cabin_map.get(d.get("cabin", "economy"), 1)

    params = {
        "engine":         "google_flights",
        "departure_id":   orig,
        "arrival_id":     dest,
        "outbound_date":  dep,
        "currency":       "EUR",
        "hl":             "fr",
        "gl":             "fr",
        "adults":         adults,
        "travel_class":   travel_class,
        "api_key":        SERPAPI_KEY,
        "type":           1 if ret else 2,
    }
    if ret:      params["return_date"] = ret
    if children: params["children"]    = children
    if d.get("direct"): params["stops"] = 0

    ck = make_key("serpapi", params)
    cached = cache_get(ck)
    if cached is not None:
        return jsonify(cached)

    try:
        future = executor.submit(
            requests.get, "https://serpapi.com/search",
            params=params, timeout=30
        )
        r    = future.result(timeout=35)
        data = r.json()
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    if "error" in data:
        return jsonify({"error": data["error"]}), 500

    results = parse_serpapi_results(data, orig, dest, ret)

    # Filtre budget
    if d.get("budget"):
        results = [r for r in results if r["prix"] <= d["budget"]]

    results.sort(key=lambda x: x["prix"])

    # Ajouter airline_url avec bons paramètres passagers
    for r in results[:8]:
        r["airline_url"] = get_airline_url(r["primary_carrier"], r["legs"], adults, children)

    final = results[:8]
    cache_set(ck, final)
    return jsonify(final)

if __name__ == "__main__":
    app.run(debug=False, threaded=True, port=5050)
