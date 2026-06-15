from flask import Flask, render_template, request, jsonify
import requests, os, re

app = Flask(__name__)

API_KEY  = os.environ.get("RAPIDAPI_KEY", "86d656bf45msh5e9078844da826cp1f1591jsn6c8215f506fd")
API_HOST = "skyscanner-flights-travel-api.p.rapidapi.com"
HEADERS  = {"x-rapidapi-key": API_KEY, "x-rapidapi-host": API_HOST}

@app.route("/")
def index():
    return render_template("index.html")

@app.route("/api/airport")
def airport():
    q = request.args.get("q","")
    r = requests.get(f"https://{API_HOST}/flights/searchAirport",
                     headers=HEADERS,
                     params={"market":"FR","query":q,"locale":"fr-FR"}, timeout=15)
    places = r.json().get("places",[])
    return jsonify(places)

@app.route("/api/search", methods=["POST"])
def search():
    d = request.json
    params = dict(
        originSkyId      = d["orig_sky"],
        destinationSkyId = d["dest_sky"],
        originEntityId   = d["orig_entity"],
        destinationEntityId = d["dest_entity"],
        date             = d["dep"],
        cabinClass       = d.get("cabin","economy"),
        adults           = str(d.get("adults",1)),
        currency         = "EUR",
        market           = "FR",
        countryCode      = "FR",
        locale           = "fr-FR",
        sortBy           = "cheapest",
    )
    if d.get("ret"):      params["returnDate"]   = d["ret"]
    if d.get("children"): params["children"]     = str(d["children"])
    if d.get("ages"):     params["childrenAges"] = d["ages"]

    r = requests.get(f"https://{API_HOST}/flights/searchFlights",
                     headers=HEADERS, params=params, timeout=60)
    itin = r.json().get("itineraries",[])

    if d.get("direct"):
        itin = [i for i in itin if all(l.get("stopCount",1)==0 for l in i.get("legs",[]))]
    if d.get("budget"):
        itin = [i for i in itin if float(i.get("price",{}).get("amount",9e9)) <= d["budget"]]

    itin.sort(key=lambda x: float(x.get("price",{}).get("amount",9e9)))

    results = []
    total_pax = d.get("adults",1) + d.get("children",0)

    for it in itin[:8]:
        prix = float(it["price"]["amount"])
        url  = it.get("bookingUrl","")
        url  = re.sub(r'passengers=\d+', f'passengers={total_pax}', url)
        if d.get("children") and "children" not in url:
            url += f"&children={d['children']}&childrenAges={d.get('ages','')}"

        # Lien Air France direct (sans OTA)
        segments = ""
        for leg in it.get("legs",[]):
            o = leg.get("origin","")
            de = leg.get("destination","")
            dt = leg.get("departure","")[:10]
            segments += f"{o}:{de}:{dt},"
        segments = segments.rstrip(",")

        pax_af = f"ADT:{d.get('adults',1)}"
        if d.get("children"): pax_af += f"_CHD:{d['children']}"

        af_url = (f"https://wwws.airfrance.fr/search/offers?"
                  f"pax={pax_af}&cabin=ECONOMY&segments={segments}")

        # Lien Google Flights
        gf_url = (f"https://www.google.com/travel/flights/search?"
                  f"tfs=&hl=fr&curr=EUR&q=vols+{it['legs'][0].get('origin','')}+"
                  f"{it['legs'][0].get('destination','')}")

        legs_data = []
        for leg in it.get("legs",[]):
            dur = leg.get("durationMinutes",0)
            legs_data.append({
                "origin":      leg.get("origin",""),
                "destination": leg.get("destination",""),
                "departure":   leg.get("departure","")[:16].replace("T"," "),
                "arrival":     leg.get("arrival","")[:16].replace("T"," "),
                "duration":    f"{dur//60}h{dur%60:02d}",
                "stops":       leg.get("stopCount",0),
                "carriers":    [c.get("name","") for c in leg.get("carriers",[])],
            })

        results.append({
            "prix":    prix,
            "legs":    legs_data,
            "skyscanner_url": url,
            "airfrance_url":  af_url,
            "google_url":     gf_url,
        })

    return jsonify(results)

if __name__ == "__main__":
    app.run(debug=True, port=5050)
