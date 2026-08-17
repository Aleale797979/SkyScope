from flask import Flask, request, jsonify
from flask_cors import CORS
import requests
import time
import os

app = Flask(__name__)
CORS(app)

ASTROMETRY_URL = "https://nova.astrometry.net/api"

@app.route("/")
def home():
    return jsonify({
        "status": "online",
        "service": "SkyScope Backend"
    })

@app.route("/solve", methods=["POST"])
def solve():
    if "image" not in request.files:
        return jsonify({"error": "Nessuna immagine ricevuta"}), 400

    api_key = os.environ.get("ASTROMETRY_API_KEY")

    if not api_key:
        return jsonify({"error": "API key mancante"}), 400

    image = request.files["image"]

    session = requests.Session()

    # Login ad Astrometry.net
    login = session.post(
        ASTROMETRY_URL + "/login",
        data={
            "request-json": '{"apikey":"' + api_key + '"}'
        },
        timeout=30
    )

    data = login.json()

    if data.get("status") != "success":
        return jsonify({"error": "API key Astrometry.net non valida"}), 401

    # Upload della foto
    upload = session.post(
        ASTROMETRY_URL + "/upload",
        data={
            "request-json": '{"publicly_visible":"n"}'
        },
        files={
            "file": (
                image.filename,
                image.stream,
                image.mimetype
            )
        },
        timeout=60
    )

    result = upload.json()

    if result.get("status") != "success":
    return jsonify({
        "error": "Upload fallito",
        "details": result
    }), 500

    submission_id = result["subid"]

    # Aspetta che Astrometry assegni un job
    job_id = None

    for _ in range(60):
        time.sleep(2)

        response = session.get(
            f"{ASTROMETRY_URL}/submissions/{submission_id}",
            timeout=30
        )

        submission = response.json()
        jobs = submission.get("jobs") or []

        if jobs and jobs[0] is not None:
            job_id = jobs[0]
            break

    if job_id is None:
        return jsonify({
            "error": "Astrometry.net non ha trovato un job"
        }), 500

    # Aspetta il plate solving
    for _ in range(90):
        time.sleep(2)

        response = session.get(
            f"{ASTROMETRY_URL}/jobs/{job_id}",
            timeout=30
        )

        status = response.json().get("status")

        if status == "success":
            break

        if status == "failure":
            return jsonify({
                "error": "Astrometry.net non è riuscito a risolvere la foto"
            }), 500

    else:
        return jsonify({
            "error": "Analisi troppo lenta"
        }), 504

    # Recupera i dati astronomici
    info = session.get(
        f"{ASTROMETRY_URL}/jobs/{job_id}/info",
        timeout=30
    ).json()

    calibration = info.get("calibration", {})

    return jsonify({
        "success": True,
        "job_id": job_id,
        "ra": calibration.get("ra"),
        "dec": calibration.get("dec"),
        "pixscale": calibration.get("pixscale"),
        "orientation": calibration.get("orientation")
    })


if __name__ == "__main__":
    app.run(
        host="0.0.0.0",
        port=10000
    )
