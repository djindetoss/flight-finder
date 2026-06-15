from flask import Flask, render_template, request, jsonify
import requests, os, re, time, threading, hashlib, json
from concurrent.futures import ThreadPoolExecutor, as_completed

executor = ThreadPoolExecutor(max_workers=20)  # pool partagé entre tous les users

app = Flask(__name__)

API_KEY  = os.environ.get("RAPIDAPI_KEY", "86d656bf45msh5e9078844da826cp1f1591jsn6c8215f506fd")
API_HOST = "skyscanner-flights-travel-api.p.rapidapi.com"
HEADERS  = {"x-rapidapi-key": API_KEY, "x-rapidapi-host": API_HOST}

# ── Cache thread-safe (TTL 10 min) ─────────────────────────────
_cache      = {}
_cache_lock = threading.Lock()
CACHE_TTL   = 600  # secondes

def cache_get(key):
    with _cache_lock:
        entry = _cache.get(key)
        if entry and (time.time() - entry["ts"]) < CACHE_TTL:
            return entry["data"]
    return None

def cache_set(key, data):
    with _cache_lock:
        _cache[key] = {"data": data, "ts": time.time()}
        # Nettoyage si trop grand
        if len(_cache) > 500:
            now = time.time()
            expired = [k for k, v in _cache.items() if (now - v["ts"]) > CACHE_TTL]
            for k in expired:
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
            f"https://{API_HOST}/flights/searchAirport",
            headers=HEADERS,
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

    params = dict(
        originSkyId         = d["orig_sky"],
        destinationSkyId    = d["dest_sky"],
        originEntityId      = d["orig_entity"],
        destinationEntityId = d["dest_entity"],
        date                = d["dep"],
        cabinClass          = d.get("cabin", "economy"),
        adults              = str(d.get("adults", 1)),
        currency            = "EUR",
        market              = "FR",
        countryCode         = "FR",
        locale              = "fr-FR",
        sortBy              = "cheapest",
    )
    if d.get("ret"):      params["returnDate"]   = d["ret"]
    if d.get("children"): params["children"]     = str(d["children"])
    if d.get("ages"):     params["childrenAges"] = d["ages"]

    # Cache clé = tous les paramètres de recherche
    ck = make_key("search", params, d.get("direct"), d.get("budget"))
    cached = cache_get(ck)
    if cached is not None:
        return jsonify(cached)

    try:
        future = executor.submit(
            requests.get,
            f"https://{API_HOST}/flights/searchFlights",
            headers=HEADERS, params=params, timeout=60
        )
        r    = future.result(timeout=65)
        itin = r.json().get("itineraries", [])
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    if d.get("direct"):
        itin = [i for i in itin if all(l.get("stopCount", 1) == 0 for l in i.get("legs", []))]
    if d.get("budget"):
        itin = [i for i in itin if float(i.get("price", {}).get("amount", 9e9)) <= d["budget"]]

    itin.sort(key=lambda x: float(x.get("price", {}).get("amount", 9e9)))

    adults   = d.get("adults", 1)
    children = d.get("children", 0)
    results  = []

    for it in itin[:8]:
        prix = float(it["price"]["amount"])

        legs_data = []
        for leg in it.get("legs", []):
            dur = leg.get("durationMinutes", 0)
            legs_data.append({
                "origin":      leg.get("origin", ""),
                "destination": leg.get("destination", ""),
                "departure":   leg.get("departure", "")[:16].replace("T", " "),
                "arrival":     leg.get("arrival", "")[:16].replace("T", " "),
                "duration":    f"{dur//60}h{dur%60:02d}",
                "stops":       leg.get("stopCount", 0),
                "carriers":    [c.get("name", "") for c in leg.get("carriers", [])],
            })

        primary_carrier = ""
        if it.get("legs") and it["legs"][0].get("carriers"):
            primary_carrier = it["legs"][0]["carriers"][0].get("name", "")

        o  = legs_data[0]["origin"]
        de = legs_data[0]["destination"]

        results.append({
            "prix":            prix,
            "legs":            legs_data,
            "primary_carrier": primary_carrier,
            "airline_url":     get_airline_url(primary_carrier, legs_data, adults, children),
            "google_url":      f"https://www.google.com/travel/flights?hl=fr&curr=EUR&q=vols+{o}+{de}",
        })

    cache_set(ck, results)
    return jsonify(results)

if __name__ == "__main__":
    app.run(debug=False, threaded=True, port=5050)
